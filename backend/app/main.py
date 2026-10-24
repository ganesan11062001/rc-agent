"""RC Copilot — FastAPI app serving the drafting API and the static frontend."""

import logging
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, evaluators, kg_client, runlog, tracing
from .config import get_settings
from .llm_client import LLMError, check_health, generate_draft, token_breakdown
from . import startup as startup_progress
from .models import (
    DraftRequest,
    DraftResponse,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    LLMDraft,
    LoginRequest,
    LoginResponse,
    Reference,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _ticket_mate_version() -> dict:
    """Which code produced a draft: this checkout's commit (and whether it had
    local changes), plus the shared OOD release when launched from one."""
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "backend"],
                                    capture_output=True, text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "", False
    return {"commit": commit + ("-dirty" if dirty else ""),
            "release": os.environ.get("RC_COPILOT_RELEASE") or None}


_TM_VERSION = _ticket_mate_version()
_PROMPT_VERSION = tracing.prompt_version()


def _gpu_name() -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5).stdout
        return ", ".join(sorted({l.strip() for l in out.splitlines() if l.strip()}))
    except (OSError, subprocess.SubprocessError):
        return ""


_GPU = _gpu_name()

logger = logging.getLogger("rc_copilot")


def configure_logging() -> None:
    settings = get_settings()
    handlers = [logging.StreamHandler()]
    if settings.log_file:
        log_path = Path(settings.log_file)
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
        except OSError as exc:
            # A read-only or missing log dir shouldn't stop the app booting.
            logging.getLogger(__name__).warning(
                "could not open log file %s (%s); logging to stdout only",
                log_path,
                exc,
            )

    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "RC Copilot starting | vllm_base_url=%s model=%s rc_kg=%s debug=%s",
        settings.vllm_base_url,
        settings.vllm_model_name,
        settings.rc_kg_url or "off",
        settings.debug,
    )
    if settings.debug:
        logger.warning(
            "DEBUG=true — full ticket text WILL be written to the logs. "
            "Do not use this on shared or production hosts."
        )
    # Printed, not logged: the token must not end up in LOG_FILE, which sits on
    # the group-readable /projects filesystem.
    print(auth.startup_banner(), flush=True)
    yield


app = FastAPI(
    title="RC Copilot",
    description=(
        "Drafts responses to Research Computing ServiceNow tickets using a "
        "local LLM served by vLLM. Manual copy-paste only — no ServiceNow "
        "integration."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "That request wasn't valid.",
            "detail": "Paste some ticket text before drafting a response.",
        },
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request, exc: HTTPException):
    """Keep every error the frontend sees in the same {error, detail} shape."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "detail": None},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def _unhandled_handler(request, exc: Exception):
    """Never leak a stack trace to the browser."""
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Something went wrong on the RC Copilot server.",
            "detail": "Check the server logs for details.",
        },
    )


@app.post("/api/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    settings = get_settings()

    if not auth.check_access_token(request.access_token):
        logger.warning("failed sign-in attempt")
        raise HTTPException(status_code=401, detail="That access token isn't right.")

    session_token, expires_at = auth.mint_session()
    logger.info("sign-in ok | session expires at %d", expires_at)
    return LoginResponse(
        session_token=session_token,
        expires_at=expires_at,
        session_hours=settings.session_hours,
    )


@app.post(
    "/api/draft",
    response_model=DraftResponse,
    dependencies=[Depends(auth.require_session)],
    responses={
        401: {"description": "Not signed in, or the session expired"},
        502: {"description": "The model server could not be used"},
    },
)
async def draft(request: DraftRequest):
    settings = get_settings()
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    logger.info(
        "draft request | id=%s ticket=%s input_chars=%d instruction_chars=%d",
        request_id,
        request.ticket_number or "-",
        len(request.ticket_text),
        len(request.extra_instructions),
    )
    if settings.debug:
        logger.debug("draft request | id=%s ticket_text=%r", request_id, request.ticket_text)

    # Filled by generate_draft: every model call (strategy, exact messages,
    # raw output, token usage, timing, outcome) and the final prompt/output.
    trace: dict = {}
    timings: dict = {}

    # The run tree (LangSmith's model): this draft is the root `chain` run;
    # retrieval, each model call, parsing and citation are child runs.
    thread = runlog.ticket_folder(request.ticket_number, request.ticket_text)
    tr = tracing.Trace(
        "draft",
        inputs={"ticket_text": request.ticket_text, "extra_instructions": request.extra_instructions,
                "ticket_number": request.ticket_number},
        metadata={"request_id": request_id, "thread_id": thread,
                  "session_id": os.environ.get("SLURM_JOB_ID") or "local",
                  "project": "rc-copilot",
                  "node": os.uname().nodename, "gpu": _GPU, "model": settings.vllm_model_name,
                  "prompt_version": _PROMPT_VERSION, "versions": {"ticket_mate": _TM_VERSION}},
        tags=["rc-copilot"],
    )

    # Ground the draft in the RC website. Never fatal: without it the draft is
    # produced exactly as before, with a caveat saying so.
    t = time.perf_counter()
    kg = await kg_client.retrieve(request.ticket_text, request_id=request_id)
    timings["retrieve"] = int((time.perf_counter() - t) * 1000)
    tr.root.metadata["versions"]["rc_kg"] = kg.trace.get("rc_kg")
    # The project is the deployment: which graph this draft was grounded in.
    variant = (((kg.trace.get("rc_kg") or {}).get("snapshot")) or {}).get("variant")
    tr.root.metadata["project"] = "rc-copilot/" + (variant or "no-kg")
    tr.root.tags.append("kg:" + kg.status)

    def record(draft_out, error, model, before_citations=None, cite_trace=None, tokens=None):
        timings["total"] = int((time.perf_counter() - started) * 1000)
        tokens = tokens or _token_summary(trace.get("attempts", []), None)
        tr.root.usage = {k: tokens.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        tr.root.tags.append("model:" + (model or "?"))
        tr.finish({"draft": draft_out} if draft_out else None, error=error)
        return runlog.record(
            request_id=request_id,
            ticket_number=request.ticket_number,
            ticket_text=request.ticket_text,
            extra_instructions=request.extra_instructions,
            messages=trace.get("messages", []),
            raw_response=trace.get("raw_response", ""),
            strategy=trace.get("strategy", "n/a"),
            draft=draft_out,
            error=error,
            model=model,
            latency_ms=timings["total"],
            trace={
                "request_id": request_id,
                "started_at": started_at,
                "versions": {"ticket_mate": _TM_VERSION, "rc_kg": kg.trace.get("rc_kg")},
                "retrieval": kg.trace,
                "llm": {"params": trace.get("params"), "final_strategy": trace.get("strategy"),
                        "attempts": trace.get("attempts", [])},
                "tokens": tokens,
                "citations": cite_trace,
                "output": {"model_draft": before_citations, "response": draft_out},
                "timings_ms": timings,
                "error": error,
                # LangSmith-style run tree: every step with id, parent, type,
                # timing, status, inputs, outputs, metadata, tags; plus feedback.
                "run_tree": tr.to_dict(),
            },
        )

    t = time.perf_counter()
    try:
        with tracing.run("generate_draft", "chain",
                         inputs={"ticket_chars": len(request.ticket_text), "kg_context_chars": len(kg.context),
                                 "kg_low_confidence": kg.low_confidence},
                         metadata={"prompt_version": _PROMPT_VERSION["version"]}) as grun:
            llm_draft, model_name = await generate_draft(
                request.ticket_text, request.extra_instructions, trace,
                kg_context=kg.context, kg_low_confidence=kg.low_confidence,
            )
            if grun is not None:
                grun.end({"draft": llm_draft.model_dump(), "strategy": trace.get("strategy")})
    except LLMError as exc:
        timings["llm"] = int((time.perf_counter() - t) * 1000)
        logger.error(
            "draft failed | id=%s input_chars=%d latency_ms=%d error=%s detail=%s",
            request_id,
            len(request.ticket_text),
            int((time.perf_counter() - started) * 1000),
            exc.message,
            exc.detail,
        )
        # Record failures too: a draft that never arrived is worth explaining.
        record(None, exc.message, settings.vllm_model_name)
        return JSONResponse(
            status_code=502,
            content={"error": exc.message, "detail": exc.detail, "request_id": request_id},
        )
    timings["llm"] = int((time.perf_counter() - t) * 1000)
    before_citations = llm_draft.model_dump()

    # Where the prompt's tokens went, by the model's own tokenizer.
    breakdown = None
    if settings.trace_token_breakdown and trace.get("messages"):
        with tracing.run("tokenize.prompt_breakdown", "tool") as krun:
            breakdown = await token_breakdown({
                "system_prompt": trace["messages"][0]["content"],
                "ticket_text": request.ticket_text,
                "rc_sources": kg.context,
                "engineer_instructions": request.extra_instructions,
            })
            if krun is not None:
                krun.end({"counts": breakdown}, error=None if breakdown else "model server has no /tokenize")
    tokens = _token_summary(trace.get("attempts", []), breakdown)

    references = []
    cite_trace = None
    t = time.perf_counter()
    if kg.status == "ok":
        fields, refs, cite_trace = await kg_client.apply_citations(
            llm_draft.model_dump(), kg, request_id=request_id)
        llm_draft = LLMDraft(**fields)
        references = [Reference(**r) for r in refs]
        if kg.low_confidence and not references:
            llm_draft.caveats.append(
                "No RC website page closely matched this ticket; the reply is "
                "not backed by RC documentation.")
    else:
        llm_draft = LLMDraft(**kg_client.strip_citations(llm_draft.model_dump()))
    if kg.status == "unavailable":
        llm_draft.caveats.append(
            "The RC knowledge graph was unavailable (%s), so this draft was not "
            "checked against the RC website and has no links." % kg.detail)
    timings["citations"] = int((time.perf_counter() - t) * 1000)

    response = DraftResponse.from_llm_draft(llm_draft, model_name, 0)
    response.references = references

    # Evaluators that need no LLM, run now so the UI can show failures:
    # assertions (the system prompt's rules, as code) and metrics.
    with tracing.run("evaluate", "chain", metadata={"evaluators": "assertions, metrics, answer_relevance"}) as erun:
        checks = evaluators.assertions(response.model_dump(), request.ticket_text, kg.status,
                                       kg.low_confidence, cite_trace)
        measures = evaluators.metrics(response.model_dump(), cite_trace)
        relevance = await evaluators.answer_relevance(request.ticket_text, response.draft_response, request_id)
        if relevance is not None:
            measures["answer_relevance_embedding"] = relevance
        if erun is not None:
            erun.end({"assertions": checks, "metrics": measures})
    for key, ok in checks.items():
        tr.add_feedback(key, 1 if ok else 0, source="assertion")
    for key, value in measures.items():
        tr.add_feedback(key, value, source="metric")
    passed = sum(checks.values())
    tr.add_feedback("assertions_passed", round(passed / len(checks), 3) if checks else 1.0, source="assertion")
    failed = sorted(k for k, ok in checks.items() if not ok)
    response.evaluations = {"assertions": checks, "assertions_failed": failed, "metrics": measures}
    if failed:
        tr.root.tags.append("assertion_failed")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response.latency_ms = elapsed_ms
    response.kg_status = kg.status
    response.request_id = request_id
    response.usage = {k: tokens[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}

    logger.info(
        "draft ok | id=%s input_chars=%d confidence=%.2f label=%s steps=%d "
        "caveats=%d references=%d kg=%s tokens=%d (prompt %d, completion %d, calls %d) "
        "latency_ms=%d model=%s",
        request_id,
        len(request.ticket_text),
        response.confidence,
        response.confidence_label,
        len(response.suggested_steps),
        len(response.caveats),
        len(response.references),
        response.kg_status,
        tokens["total_tokens"], tokens["prompt_tokens"], tokens["completion_tokens"],
        tokens["model_calls"],
        elapsed_ms,
        model_name,
    )
    # Automatic feedback, LangSmith-style: scores attached to this trace that
    # can be filtered and compared across models and prompt versions.
    tr.add_feedback("grounded", 1 if kg.status == "ok" else 0)
    tr.add_feedback("references", len(references))
    tr.add_feedback("citations_flagged", sum(1 for c in response.caveats if c.startswith("Check citation")))
    tr.add_feedback("urls_removed", len((cite_trace or {}).get("removed_urls") or []))
    tr.add_feedback("retrieval_low_confidence", 1 if kg.low_confidence else 0)
    tr.add_feedback("needed_fallback", 1 if tokens["model_calls"] > 1 else 0)
    saved = record(response.model_dump(), None, model_name, before_citations, cite_trace, tokens)
    if saved:
        logger.info("run recorded | id=%s -> %s", request_id, saved)
        # LLM-as-judge in the background; it appends its grades beside the run.
        evaluators.schedule_judge(request_id, request.ticket_text, kg.context, response.model_dump())

    if settings.debug:
        logger.debug("draft response | id=%s %s", request_id, response.model_dump())

    return response


def _token_summary(attempts, breakdown):
    """Totals over every model call this draft made (fallback strategies
    included -- they cost tokens too), the final call alone, and where the
    prompt tokens went."""
    used = [a["usage"] for a in attempts if a.get("usage")]
    total = {k: sum(u[k] for u in used) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    final = next((a["usage"] for a in reversed(attempts) if a.get("outcome") == "ok"), None)
    out = {**total, "model_calls": len(attempts), "calls_with_usage": len(used), "final_call": final}
    if breakdown is not None:
        out["prompt_breakdown"] = dict(breakdown)
        if final:
            # Chat template, the JSON reminder and the fixed prompt scaffolding.
            out["prompt_breakdown"]["template_and_scaffolding"] = final["prompt_tokens"] - sum(breakdown.values())
    return out


@app.post("/api/feedback", response_model=FeedbackResponse,
          dependencies=[Depends(auth.require_session)])
async def feedback(request: FeedbackRequest):
    """Attach a person's judgement to a draft (LangSmith-style feedback):
    a score under a key, optionally a comment and the corrected reply. Stored
    beside the draft's run log, so it travels with the trace."""
    saved = runlog.add_feedback(request.request_id, {
        "key": request.key, "score": request.score, "value": request.value,
        "comment": request.comment, "correction": request.correction, "source": "user",
    })
    if saved is None:
        raise HTTPException(status_code=404, detail="No recorded draft with that request id.")
    if request.correction.strip():
        # What the engineer actually sent vs what was drafted: an implicit
        # correctness signal (1 = sent unchanged).
        sim = runlog.edit_similarity(request.request_id, request.correction)
        if sim is not None:
            runlog.add_feedback(request.request_id, {"key": "edit_similarity", "score": sim,
                                                     "source": "derived", "comment": "draft vs reply sent"})
    logger.info("feedback | id=%s key=%s score=%s", request.request_id, request.key, request.score)
    return FeedbackResponse(feedback_id=saved, request_id=request.request_id)


@app.get("/api/health", response_model=HealthResponse)
async def health():
    settings = get_settings()
    reachable, model_ids, detail = await check_health()
    kg_state, kg_detail = await kg_client.health()

    if not reachable:
        logger.warning("health check failed | %s", detail)
        # Deliberately 200, not 503. This endpoint reports health; it is not
        # itself failing. A 503 here is indistinguishable from OOD's own 503
        # ("nothing is listening on that port"), so the extension would tell
        # the user the job was dead while the backend was answering fine and
        # simply waiting for the model to load. The body carries the status.
        return HealthResponse(
            status="unreachable",
            auth_required=not settings.auth_disabled,
            vllm_base_url=settings.vllm_base_url,
            configured_model=settings.vllm_model_name,
            detail=detail,
            knowledge_graph=kg_state,
            knowledge_graph_detail=kg_detail,
            startup=startup_progress.startup(False, kg_state, kg_detail),
        )

    model_available = settings.vllm_model_name in model_ids if model_ids else None
    return HealthResponse(
        status="ok",
        auth_required=not settings.auth_disabled,
        vllm_base_url=settings.vllm_base_url,
        configured_model=settings.vllm_model_name,
        model_available=model_available,
        available_models=model_ids,
        detail=(
            None
            if model_available is not False
            else "Server is up, but it is not serving '%s'."
            % settings.vllm_model_name
        ),
        knowledge_graph=kg_state,
        knowledge_graph_detail=kg_detail,
        startup=startup_progress.startup(True, kg_state, kg_detail),
    )


# Mounted last so the /api/* routes above win. html=True serves index.html at /.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="frontend")
else:  # pragma: no cover - only hit if the repo layout is broken
    logger.error("frontend directory not found at %s", WEB_DIR)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=get_settings().app_port,
        reload=True,
    )
