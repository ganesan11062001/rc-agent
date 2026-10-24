"""HTTP API for TicketMate (RC Copilot) and anything else that needs grounded
RC context.

  POST /retrieve   ticket or question -> numbered sources + prompt context.
                   No LLM. This is what TicketMate's backend calls: it puts
                   `context` into its own prompt and runs its own model.
  POST /cite       model output with [S#] markers -> text with exact links,
                   any invented URL removed. Stateless: send back the
                   `sources`/`allowed_urls` /retrieve gave you. Accepts one
                   `text` or several `texts` sharing one citation numbering.
  POST /answer     retrieve + LLM + cite in one call, if LLM_BASE_URL is set.
  GET  /health     never needs the token; "loading" until the graph and the
                   embedding model are both ready.

Run:  scripts/serve_kg.sh            (per-job Neo4j from the snapshot + this)
      uvicorn api.app:app --host 127.0.0.1 --port 8090   (dev, uses .env)
"""

import hmac
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from config import get_settings
from graph.connection import driver
from retrieval import citations, http, support
from retrieval.embeddings import get_embedder
from retrieval.graphrag import retrieve

logger = logging.getLogger(__name__)

# One line per request -- request id, timings, counts -- and never the query:
# ticket text stays in the caller's private run log, not in rc-kg's log.
reqlog = logging.getLogger("rc_kg.requests")
if not reqlog.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    reqlog.addHandler(_h)
    reqlog.setLevel(logging.INFO)
    reqlog.propagate = False

_ready = threading.Event()
_warm_error: List[str] = []


# serve_kg.sh starts the API while Neo4j is still loading the snapshot (or,
# with no snapshot, while the graph is still being built), so warm-up waits
# for a graph that answers and has embeddings rather than failing at once.
_WARM_TIMEOUT_S = float(os.environ.get("RC_KG_WARM_TIMEOUT", "900"))
_WARMUP_QUERIES = [
    "job killed out of memory on the gpu partition",
    "how do I transfer data with globus to scratch",
    "conda environment and modules for python on explorer",
    "slurm job pending in the queue, sbatch srun",
    "storage quota for projects and home directory",
]


def _progress(step: str, status: str, detail: str = "") -> None:
    """One startup step's state, for TicketMate's progress display (same
    files and format as kg_progress in scripts/_kg_instance.sh)."""
    d = os.environ.get("RC_KG_PROGRESS_DIR")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, step + ".json")
        tmp = "%s.tmp.%d" % (target, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"step": step, "status": status, "detail": detail[:300],
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, fh)
        os.replace(tmp, target)
    except OSError:
        pass                                       # progress is best-effort


def _graph_ready() -> bool:
    try:
        d = driver()
        d.verify_connectivity()
        with d.session(database=get_settings().neo4j_database) as sess:
            return sess.run(
                "MATCH (s:Section) WHERE s.embedding IS NOT NULL RETURN count(s) > 0 AS ok"
            ).single()["ok"]
    except Exception:                              # noqa: BLE001
        return False


def _warm() -> None:
    """Load the embedding model and wait for the graph before the first query,
    so /health turning "ok" means a query will actually be answered."""
    step = "embedder"
    try:
        _progress("embedder", "running", "loading the embedding model")
        get_embedder()
        _progress("embedder", "done")
        step = "graph"
        _progress("graph", "running", "waiting for Neo4j to answer with an embedded graph")
        deadline = time.monotonic() + _WARM_TIMEOUT_S
        while not _graph_ready():
            if time.monotonic() > deadline:
                raise RuntimeError("graph not ready after %.0f s" % _WARM_TIMEOUT_S)
            time.sleep(1)
        _progress("graph", "done")
        step = "warmup"
        # A few varied throwaway queries before reporting ready. Neo4j's JVM
        # compiles and optimizes each query over its first executions; traces
        # showed every session's first real ticket paying for it (retrieval
        # 3.1 s cold vs 0.17 s warm; topic boost and vector search the worst).
        # One warm-up query was not enough (first ticket still 0.96 s); five
        # varied ones bring it to ~0.13 s, measured 2026-10-24.
        for i, q in enumerate(_WARMUP_QUERIES, 1):
            _progress("warmup", "running", "warm-up query %d/%d" % (i, len(_WARMUP_QUERIES)))
            try:
                retrieve(q, k=6)
            except Exception:                      # noqa: BLE001 -- warm-up is best-effort
                logger.warning("warm-up query failed", exc_info=True)
                break
        _progress("warmup", "done", "%d queries" % len(_WARMUP_QUERIES))
        _ready.set()
        logger.info("rc-kg ready")
    except Exception as exc:                       # noqa: BLE001
        _warm_error.append(str(exc).split("\n")[0])
        _progress(step, "failed", _warm_error[0])
        logger.exception("rc-kg warm-up failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In a thread: the port opens immediately and /health reports "loading",
    # rather than the client seeing connection refused for ~20 s.
    _progress("api_start", "done", "API listening")
    threading.Thread(target=_warm, daemon=True).start()
    yield


# The HTTP contract clients depend on (see API_CONTRACT.md). MAJOR changes
# when a client written for the old one could break: a field removed or
# renamed, a meaning changed. MINOR for additions. Clients check MAJOR.
API_VERSION = "1.4"

app = FastAPI(title="RC-KG", version=API_VERSION, lifespan=lifespan)


def require_token(authorization: Optional[str] = Header(None)) -> None:
    token = get_settings().rc_kg_api_token
    if not token:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, token):
        raise HTTPException(401, "missing or wrong bearer token")


def require_ready() -> None:
    if not _ready.is_set():
        raise HTTPException(503, "rc-kg is still loading" if not _warm_error
                            else "rc-kg failed to start: %s" % _warm_error[0])


class RetrieveRequest(BaseModel):
    query: str = Field(..., max_length=50_000, description="Ticket text or a question")
    k: int = Field(6, ge=1, le=20)
    include_events: Optional[bool] = None


_STYLE = "^(markdown|plain|numbered)$"


class CiteRequest(BaseModel):
    text: Optional[str] = None
    texts: Optional[List[str]] = None
    refs: Dict[str, str] = Field(..., description='{"S1": url_with_anchor, ...}')
    allowed_urls: List[str]
    style: str = Field("markdown", pattern=_STYLE)
    # Also judge whether each [S#] fits its sentence better than the other
    # sources do (retrieval/support.py). Needs the graph and model loaded.
    check_support: bool = False


class SimilarityRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=50_000)
    texts: List[str] = Field(..., min_length=1, max_length=20)


class AnswerRequest(RetrieveRequest):
    style: str = Field("markdown", pattern=_STYLE)


ANSWER_SYSTEM = """\
You answer questions from Northeastern Research Computing staff using ONLY the \
numbered sources provided. Rules:
- Every factual claim must be followed by the source it came from, as [S1] or \
[S1, S3]. Cite by marker only.
- Never write a URL. Links are added automatically from the markers.
- If the sources do not contain the answer, say so plainly and say what is \
missing. Do not fill gaps from general knowledge about other HPC sites; \
Northeastern's partitions, paths and limits are only what the sources say.
- Be concise and practical. Quote commands exactly as the sources give them.
"""


def _snapshot() -> Optional[Dict]:
    path = get_settings().rc_kg_manifest
    if not path:
        return None
    try:
        m = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    out = {k: m.get(k) for k in ("built_at", "git_commit", "eval")}
    # Snapshots from before variants existed are the full graph.
    out["variant"] = m.get("variant", "full")
    out["sites"] = m.get("sites") or ["rc.northeastern.edu", "rc-docs.northeastern.edu"]
    return out


@app.get("/health")
def health() -> Dict:
    s = get_settings()
    if not _ready.is_set():
        return {"status": "failed" if _warm_error else "loading",
                "api_version": API_VERSION,
                "detail": _warm_error[0] if _warm_error else None}
    with driver().session(database=s.neo4j_database) as sess:
        rec = sess.run(
            "MATCH (s:Section) RETURN count(s) AS sections, "
            "count(s.embedding) AS embedded, "
            "sum(CASE WHEN s.retrievable THEN 1 ELSE 0 END) AS retrievable"
        ).single()
    return {"status": "ok", "api_version": API_VERSION, **dict(rec),
            "embedding_model": s.embedding_model,
            "snapshot": _snapshot(), "llm_configured": bool(s.llm_base_url)}


@app.post("/retrieve", dependencies=[Depends(require_token), Depends(require_ready)])
def retrieve_endpoint(req: RetrieveRequest,
                      x_request_id: Optional[str] = Header(None)) -> Dict:
    try:
        res = retrieve(req.query, k=req.k, include_events=req.include_events)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    body = asdict(res)
    body["trace"].update(request_id=x_request_id, api_version=API_VERSION, snapshot=_snapshot())
    reqlog.info("retrieve id=%s k=%d sources=%d best_vec=%.3f low=%s total_ms=%s",
                x_request_id or "-", req.k, len(res.sources), res.trace["best_vector_score"],
                res.low_confidence, res.trace["timings_ms"]["total"])
    return body


@app.post("/cite", dependencies=[Depends(require_token)])
def cite_endpoint(req: CiteRequest, x_request_id: Optional[str] = Header(None)) -> Dict:
    t0 = time.perf_counter()
    if (req.text is None) == (req.texts is None):
        raise HTTPException(422, "send exactly one of `text` or `texts`")
    texts = [req.text] if req.text is not None else req.texts
    out = citations.render_many(texts, req.refs, req.allowed_urls, req.style)
    body = asdict(out)
    # Judged on the model's original text, where [S#] still marks each claim.
    body["support"] = None
    if req.check_support and _ready.is_set():
        body["support"] = [asdict(v) for v in support.check(texts, req.refs)]
    body["trace"] = {"request_id": x_request_id, "api_version": API_VERSION,
                     "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    reqlog.info("cite id=%s texts=%d cited=%d removed=%d unknown=%d flagged=%d ms=%s",
                x_request_id or "-", len(texts), len(out.cited), len(out.removed_urls),
                len(out.unknown_refs),
                sum(1 for v in body["support"] or [] if v["verdict"] != "ok"),
                body["trace"]["latency_ms"])
    if req.text is not None:
        body["text"] = out.text
        del body["texts"]
    return body


@app.post("/similarity", dependencies=[Depends(require_token), Depends(require_ready)])
def similarity_endpoint(req: SimilarityRequest, x_request_id: Optional[str] = Header(None)) -> Dict:
    """Cosine similarity of `query` (embedded as a query) to each text
    (embedded as a passage), with the same local model retrieval uses. For
    clients' answer-relevance checks; nothing leaves the node."""
    t0 = time.perf_counter()
    emb = get_embedder()
    q = emb.embed_query(req.query)
    docs = emb.embed_documents([t[:20_000] for t in req.texts])
    scores = [round(sum(a * b for a, b in zip(q, d)), 4) for d in docs]
    ms = round((time.perf_counter() - t0) * 1000, 1)
    reqlog.info("similarity id=%s texts=%d ms=%s", x_request_id or "-", len(req.texts), ms)
    return {"scores": scores, "model": emb.model,
            "trace": {"request_id": x_request_id, "api_version": API_VERSION, "latency_ms": ms}}


def _chat(messages: List[Dict]) -> str:
    s = get_settings()
    if not s.llm_base_url:
        raise HTTPException(503, "LLM_BASE_URL is not set; use /retrieve and your own model")
    headers = {"Authorization": "Bearer %s" % s.llm_api_key} if s.llm_api_key else {}
    r = http.post(
        s.llm_base_url.rstrip("/") + "/chat/completions",
        json={"model": s.llm_model, "messages": messages,
              "temperature": s.llm_temperature, "max_tokens": s.llm_max_tokens},
        headers=headers, timeout=300,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


@app.post("/answer", dependencies=[Depends(require_token), Depends(require_ready)])
def answer_endpoint(req: AnswerRequest) -> Dict:
    res = retrieve(req.query, k=req.k, include_events=req.include_events)
    if not res.sources:
        return {"answer": "No RC website content matched this question.", "sources": []}
    raw = _chat([
        {"role": "system", "content": ANSWER_SYSTEM},
        {"role": "user", "content": "SOURCES\n\n%s\n\nQUESTION\n%s" % (res.context, res.query)},
    ])
    refs = {src.ref: src.url for src in res.sources}
    out = citations.render(raw, refs, res.allowed_urls, req.style)
    return {
        "answer": out.text,
        "cited": out.cited,
        "references": out.references,
        "removed_urls": out.removed_urls,
        "unknown_refs": out.unknown_refs,
        "low_confidence": res.low_confidence,
        "sources": [
            {"ref": src.ref, "title": src.page_title, "heading": src.heading,
             "url": src.url, "score": src.score}
            for src in res.sources
        ],
    }
