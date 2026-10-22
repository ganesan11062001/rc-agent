"""RC Copilot — FastAPI app serving the drafting API and the static frontend."""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, kg_client, runlog
from .config import get_settings
from .llm_client import LLMError, check_health, generate_draft
from .models import (
    DraftRequest,
    DraftResponse,
    HealthResponse,
    LLMDraft,
    LoginRequest,
    LoginResponse,
    Reference,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

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

    logger.info(
        "draft request | id=%s ticket=%s input_chars=%d instruction_chars=%d",
        request_id,
        request.ticket_number or "-",
        len(request.ticket_text),
        len(request.extra_instructions),
    )
    if settings.debug:
        logger.debug("draft request | id=%s ticket_text=%r", request_id, request.ticket_text)

    # Collects the exact prompt, the raw model output and which structured
    # output strategy worked, so this draft can be explained afterwards.
    trace: dict = {}

    # Ground the draft in the RC website. Never fatal: without it the draft is
    # produced exactly as before, with a caveat saying so.
    kg = await kg_client.retrieve(request.ticket_text)

    try:
        llm_draft, model_name = await generate_draft(
            request.ticket_text, request.extra_instructions, trace,
            kg_context=kg.context, kg_low_confidence=kg.low_confidence,
        )
    except LLMError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.error(
            "draft failed | id=%s input_chars=%d latency_ms=%d error=%s detail=%s",
            request_id,
            len(request.ticket_text),
            elapsed_ms,
            exc.message,
            exc.detail,
        )
        # Record failures too: a draft that never arrived is worth explaining.
        runlog.record(
            request_id=request_id,
            ticket_number=request.ticket_number,
            ticket_text=request.ticket_text,
            extra_instructions=request.extra_instructions,
            messages=trace.get("messages", []),
            raw_response=trace.get("raw_response", ""),
            strategy=trace.get("strategy", "n/a"),
            draft=None,
            error=exc.message,
            model=settings.vllm_model_name,
            latency_ms=elapsed_ms,
        )
        return JSONResponse(
            status_code=502,
            content={"error": exc.message, "detail": exc.detail},
        )

    references = []
    if kg.status == "ok":
        fields, refs = await kg_client.apply_citations(llm_draft.model_dump(), kg)
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

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response = DraftResponse.from_llm_draft(llm_draft, model_name, elapsed_ms)
    response.references = references
    response.kg_status = kg.status

    logger.info(
        "draft ok | id=%s input_chars=%d confidence=%.2f label=%s steps=%d "
        "caveats=%d references=%d kg=%s latency_ms=%d model=%s",
        request_id,
        len(request.ticket_text),
        response.confidence,
        response.confidence_label,
        len(response.suggested_steps),
        len(response.caveats),
        len(response.references),
        response.kg_status,
        elapsed_ms,
        model_name,
    )
    saved = runlog.record(
        request_id=request_id,
        ticket_number=request.ticket_number,
        ticket_text=request.ticket_text,
        extra_instructions=request.extra_instructions,
        messages=trace.get("messages", []),
        raw_response=trace.get("raw_response", ""),
        strategy=trace.get("strategy", "unknown"),
        draft=response.model_dump(),
        error=None,
        model=model_name,
        latency_ms=elapsed_ms,
    )
    if saved:
        logger.info("run recorded | id=%s -> %s", request_id, saved)

    if settings.debug:
        logger.debug("draft response | id=%s %s", request_id, response.model_dump())

    return response


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
