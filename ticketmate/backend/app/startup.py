"""What the session is doing while it starts, for the UI's progress display.

Two things start in parallel, and both take a while:

  vLLM   read from its log ($VLLM_LOG_FILE): the stage it has reached, and
         progress *within* the slow stages from its own counters -- weight
         shards ("Loading safetensors checkpoint shards: 1/2") and CUDA graph
         capture ("Capturing CUDA graphs (PIECEWISE): 1/51"). Markers match
         vLLM 0.29's log (cluster/logs/vllm-*.out); unknown lines are ignored,
         so another version degrades to coarser stages, never to an error.
  rc_kg  the per-step checklist rc_kg writes ($RC_KG_PROGRESS_DIR, one
         <step>.json each: pending/running/done/failed).

Percentages are estimates for a progress bar, weighted by where startup time
actually goes (weights dominate for large models); the stage names and the
counters are exact.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STARTED = time.time()                  # the backend starts just before vLLM
# vLLM captures CUDA graphs in several passes (4 in the 0.29 log: 51, 2, 51,
# 35 graphs) and the count of passes depends on its config, so the bar is
# kept from ever moving backwards; the detail shows which pass is running.
_high_water = 0.0
_GRAPH_PASSES = 4

# (key, label, marker regex, % of the bar when this stage begins)
VLLM_STAGES = [
    ("starting", "Starting vLLM", None, 0),
    ("engine", "Initializing the engine", r"Initializing (?:a V1|an) LLM engine", 5),
    ("weights", "Loading model weights", r"Loading model from scratch|Starting to load model|Checkpoint size:", 10),
    ("compile", "Compiling the model", r"torch\.compile|Directly load AOT compilation|Dynamo bytecode transform|Compiling a graph", 60),
    ("profile", "Profiling memory / KV cache", r"Initial profiling/warmup run|Memory profiling|Available KV cache memory|GPU KV cache size", 70),
    ("graphs", "Capturing CUDA graphs", r"Capturing CUDA graphs", 75),
    ("server", "Starting the API server", r"init engine .* took|Starting vLLM (?:API )?server|Uvicorn running on", 95),
]
_SHARDS = re.compile(r"Loading safetensors checkpoint shards:\s*(\d+)%[^|]*\|\s*(\d+)/(\d+)")
_GRAPHS = re.compile(r"Capturing CUDA graphs[^:]*:\s*(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")
_ERROR = re.compile(r"(CUDA out of memory|OutOfMemoryError|Traceback \(most recent call last\)|EngineCore failed|"
                    r"Engine core initialization failed|RuntimeError: .*|ValueError: .*|No CUDA GPUs are available|"
                    r"rc-copilot: vLLM exited with status \d+)")
_MODEL = re.compile(r"model='([^']+)'")
_SIZE = re.compile(r"Checkpoint size: ([\d.]+ ?[GM]iB)")

KG_STEPS = [
    ("prepare", "Creating a private Neo4j"),
    ("runtime", "Preparing the Python runtime"),
    ("neo4j_load", "Loading the graph snapshot"),
    ("neo4j_start", "Starting Neo4j"),
    ("api_start", "Starting the graph API"),
    ("embedder", "Loading the embedding model"),
    ("graph", "Connecting to the graph"),
    ("warmup", "Warming up queries"),
]


def _read(path: str, limit: int = 4_000_000) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def vllm_progress(log_path: Optional[str], reachable: bool) -> Dict[str, Any]:
    global _high_water
    if reachable:
        return {"stage": "ready", "label": "Ready", "pct": 100, "detail": None, "failed": False}
    text = _read(log_path) if log_path else ""
    if not text:
        return {"stage": "starting", "label": "Starting vLLM", "pct": 0, "failed": False,
                "detail": "waiting for the model server to start" if log_path else
                          "waiting for the model server (no log to read)"}
    reached = 0
    for i, (_k, _l, rx, _p) in enumerate(VLLM_STAGES):
        if rx and re.search(rx, text):
            reached = i
    key, label, _rx, start = VLLM_STAGES[reached]
    end = VLLM_STAGES[reached + 1][3] if reached + 1 < len(VLLM_STAGES) else 99
    pct, detail = float(start), None
    model = (_MODEL.findall(text) or [None])[-1]
    if key == "weights":
        m = _SHARDS.findall(text)
        size = (_SIZE.findall(text) or [None])[-1]
        if m:
            _p, done, total = m[-1]
            pct = start + (end - start) * int(done) / max(1, int(total))
            detail = "%s/%s weight shards%s" % (done, total, " of %s" % size if size else "")
        elif size:
            detail = "%s checkpoint" % size
    elif key == "graphs":
        m = _GRAPHS.findall(text)
        if m:
            _p, done, total = m[-1]
            passes = max(1, sum(1 for x in m if x[1] == "0"))
            # Spread the stage over the passes expected, so an early short pass
            # does not fill the bar; extra passes just hold near the end.
            expect = max(_GRAPH_PASSES, passes)
            pct = start + (end - start) * ((passes - 1) + int(done) / max(1, int(total))) / expect
            detail = "pass %d · %s/%s graphs" % (passes, done, total)
    # Fatal only: vLLM logs harmless "Failed to ..." warnings during normal starts.
    fatal = re.search(r"Traceback \(most recent call last\)|CUDA out of memory|OutOfMemoryError|"
                      r"Engine core initialization failed|EngineCore failed|No CUDA GPUs are available|"
                      r"rc-copilot: vLLM exited", text)
    err = _ERROR.findall(text)
    failed = bool(fatal)
    _high_water = max(_high_water, min(pct, 99))
    out = {"stage": "failed" if failed else key, "label": "Failed" if failed else label,
           "pct": round(_high_water, 1), "detail": (err[-1][:200] if failed else detail), "failed": failed}
    if model:
        out["model"] = model
    return out


def kg_progress(progress_dir: Optional[str], kg_state: str, kg_detail: Optional[str]) -> Dict[str, Any]:
    if kg_state == "ok":
        return {"stage": "ready", "label": "Ready", "pct": 100, "detail": kg_detail, "failed": False, "steps": []}
    if kg_state == "disabled":
        return {"stage": "disabled", "label": "Not configured", "pct": 100, "detail": None, "failed": False, "steps": []}
    steps: List[Dict[str, Any]] = []
    if progress_dir and Path(progress_dir).is_dir():
        for key, label in KG_STEPS:
            try:
                d = json.loads((Path(progress_dir) / (key + ".json")).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                d = {"status": "pending"}
            steps.append({"step": key, "label": label, "status": d.get("status", "pending"),
                          "detail": d.get("detail") or None})
    if not steps:
        return {"stage": kg_state, "label": "Starting" if kg_state in ("unreachable", "loading") else kg_state,
                "pct": 0, "detail": kg_detail, "failed": kg_state == "incompatible", "steps": []}
    done = sum(1 for s in steps if s["status"] == "done")
    failed = next((s for s in steps if s["status"] == "failed"), None)
    running = [s for s in steps if s["status"] == "running"]
    current = failed or (running[-1] if running else next((s for s in steps if s["status"] == "pending"), steps[-1]))
    return {"stage": "failed" if failed else current["step"], "label": "Failed: " + current["label"] if failed else current["label"],
            "pct": round(100 * done / len(steps)), "detail": current.get("detail"), "failed": bool(failed),
            "running": [s["label"] for s in running], "steps": steps}


def startup(vllm_reachable: bool, kg_state: str, kg_detail: Optional[str]) -> Dict[str, Any]:
    v = vllm_progress(os.environ.get("VLLM_LOG_FILE"), vllm_reachable)
    k = kg_progress(os.environ.get("RC_KG_PROGRESS_DIR"), kg_state, kg_detail)
    return {
        "ready": v["stage"] == "ready" and k["stage"] in ("ready", "disabled"),
        "model_ready": v["stage"] == "ready",
        "elapsed_s": int(time.time() - STARTED),
        "vllm": v,
        "knowledge_graph": k,
    }
