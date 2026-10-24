"""Tier 2 evaluation: RAGAS metrics judged by an LLM -- your own vLLM.

Two inputs:

  --questions   eval/questions.yaml, retrieved through the rc-kg API:
                ContextRelevance (are the retrieved contexts relevant to the
                question, as judged by the LLM). Complements Tier 1, whose
                label-based precision undercounts relevant pages nobody
                labelled.
  --runs DIR    TicketMate run logs (one JSON per draft, holding the ticket,
                the prompt's sources and the raw draft):
                Faithfulness and ResponseGroundedness (is the draft supported
                by the sources), ContextUtilization (did the sources it was
                given get used), ContextRelevance.

The judge must be a model inside the university -- ticket text goes to it.
Point --judge-url at a vLLM's /v1 (e.g. the one in a TicketMate session, or
`serve_glm.sh` on a GPU node). ragas talks to it in instructor's JSON mode,
which vLLM supports without tool-call flags.

Runs in .venv-eval (scripts/eval_env.sh), not the serving venv; reaches the
graph through the rc-kg HTTP API, like any other client:

  scripts/ragas_eval.sh --judge-url http://<node>:<port>/v1 --questions
  scripts/ragas_eval.sh --judge-url http://<node>:<port>/v1 --runs /scratch/$USER/rc_copilot_runs
  scripts/ragas_eval.sh ... --dry-run        build the samples, call no LLM

An LLM judge is itself noisy and model-dependent: compare runs made with the
same judge model, and read the per-sample scores before trusting a mean.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval.metrics import bootstrap_ci  # noqa: E402  (pure Python, no serving deps)

HERE = Path(__file__).resolve().parent
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
_MARKERS = re.compile(r"\s*\[S\d+(?:\s*,\s*S\d+)*\]")


def _get(url: str, token: str = "", body=None) -> Dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with _open(req, timeout=120) as r:
        return json.loads(r.read())


def _questions(path: Path) -> List[str]:
    import yaml
    return [q["question"] for q in yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]]


def samples_from_questions(kg_url: str, token: str, k: int, limit: int) -> List[Dict]:
    out = []
    for q in _questions(HERE / "questions.yaml")[: limit or None]:
        r = _get(kg_url.rstrip("/") + "/retrieve", token, {"query": q, "k": k})
        out.append({"id": q, "user_input": q,
                    "retrieved_contexts": ["%s > %s\n%s" % (s["page_title"], s["heading"], s["text"])
                                           for s in r["sources"]]})
    return out


def samples_from_runs(run_dir: Path, only: str, limit: int) -> List[Dict]:
    out = []
    for path in sorted(run_dir.rglob("*.json")):
        if only and not path.parent.name.startswith(only.upper()):
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        user = next((m["content"] for m in run.get("prompt") or [] if m["role"] == "user"), "")
        if "--- RC WEBSITE SOURCES ---" not in user:
            continue                                  # an ungrounded draft
        block = user.split("--- RC WEBSITE SOURCES ---", 1)[1].split("--- END RC WEBSITE SOURCES ---", 1)[0]
        # The first chunk starts with the prompt's instructions; each source
        # begins at its "[S#] title" line, so cut every chunk there.
        contexts = []
        for chunk in re.split(r"\n\n---\n\n", block):
            m = re.search(r"^\[S\d+\] ", chunk, re.M)
            if m:
                contexts.append(chunk[m.start():].strip())
        raw = run.get("raw_response") or ""
        try:
            draft = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except ValueError:
            continue
        response = _MARKERS.sub("", "\n".join(
            [draft.get("draft_response", "")] + list(draft.get("suggested_steps", []))))
        out.append({"id": str(path.relative_to(run_dir)), "model": run.get("model", ""),
                    "user_input": (run.get("input") or {}).get("ticket_text", ""),
                    "response": response, "retrieved_contexts": contexts})
        if limit and len(out) >= limit:
            break
    return out


async def score(samples: List[Dict], judge_url: str, judge_model: str, concurrency: int) -> List[Dict]:
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import (ContextRelevance, ContextUtilization, Faithfulness,
                                           ResponseGroundedness)
    host = urlparse(judge_url).hostname or ""
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(filter(None, [os.environ.get("no_proxy"), host]))
    client = AsyncOpenAI(base_url=judge_url, api_key=os.environ.get("JUDGE_API_KEY", "EMPTY"))
    llm = llm_factory(judge_model, provider="openai", client=client)
    metrics = {"context_relevance": ContextRelevance(llm=llm)}
    if any("response" in s for s in samples):
        metrics.update(faithfulness=Faithfulness(llm=llm), response_groundedness=ResponseGroundedness(llm=llm),
                       context_utilization=ContextUtilization(llm=llm))
    sem = asyncio.Semaphore(concurrency)

    async def one(s: Dict) -> Dict:
        res = {"id": s["id"]}
        for name, metric in metrics.items():
            if name != "context_relevance" and "response" not in s:
                continue
            args = {"context_relevance": dict(user_input=s["user_input"], retrieved_contexts=s["retrieved_contexts"]),
                    "faithfulness": dict(user_input=s["user_input"], response=s.get("response", ""),
                                         retrieved_contexts=s["retrieved_contexts"]),
                    "response_groundedness": dict(response=s.get("response", ""), retrieved_contexts=s["retrieved_contexts"]),
                    "context_utilization": dict(user_input=s["user_input"], response=s.get("response", ""),
                                                retrieved_contexts=s["retrieved_contexts"])}[name]
            async with sem:
                try:
                    res[name] = float((await metric.ascore(**args)).value)
                except Exception as exc:              # noqa: BLE001 -- one bad sample must not stop the run
                    res[name] = float("nan")
                    res.setdefault("errors", []).append("%s: %s" % (name, str(exc)[:160]))
        return res

    return await asyncio.gather(*(one(s) for s in samples))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--questions", action="store_true")
    src.add_argument("--runs", metavar="DIR")
    ap.add_argument("--only", default="", help="--runs: ticket-number prefix, e.g. RCKG-EVAL")
    ap.add_argument("--kg-url", default=os.environ.get("RC_KG_URL", ""))
    ap.add_argument("--kg-token", default=os.environ.get("RC_KG_API_TOKEN", ""))
    ap.add_argument("--judge-url", default=os.environ.get("JUDGE_URL", ""))
    ap.add_argument("--judge-model", default="", help="default: the first model the judge serves")
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="build samples; call no LLM")
    ap.add_argument("--json")
    args = ap.parse_args()

    if args.questions:
        if not args.kg_url:
            ap.error("--questions needs --kg-url (scripts/ragas_eval.sh starts rc-kg for you)")
        samples = samples_from_questions(args.kg_url, args.kg_token, args.k, args.limit)
    else:
        samples = samples_from_runs(Path(args.runs), args.only, args.limit)
    print("%d samples" % len(samples))
    if not samples:
        return 1
    if args.dry_run:
        s = samples[0]
        print(json.dumps({k: (v[:2] if isinstance(v, list) else str(v)[:300]) for k, v in s.items()}, indent=2)[:2000])
        return 0
    if not args.judge_url:
        ap.error("--judge-url is required (a vLLM /v1 endpoint inside the university)")
    model = args.judge_model or _get(args.judge_url.rstrip("/") + "/models")["data"][0]["id"]
    print("judge: %s at %s" % (model, args.judge_url))
    rows = asyncio.run(score(samples, args.judge_url, model, args.concurrency))
    names = sorted({k for r in rows for k in r if k not in ("id", "errors")})
    summary = {"judge_model": model, "samples": len(rows)}
    for n in names:
        point, lo, hi = bootstrap_ci([r[n] for r in rows if n in r])
        summary[n] = {"mean": round(point, 3), "ci95": [round(lo, 3), round(hi, 3)],
                      "scored": sum(1 for r in rows if n in r and r[n] == r[n])}
        print("  %-22s %.3f  95%% CI [%.3f, %.3f]  (%d scored)" % (n, point, lo, hi, summary[n]["scored"]))
    errors = [e for r in rows for e in r.get("errors", [])]
    if errors:
        print("  %d metric calls failed, e.g. %s" % (len(errors), errors[0]))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
