"""Prompt optimization: A/B a prompt variant against the baseline on the ground truth.

    # put changed copies of any agent prompts (backend/app/agents/prompts/*.md) in a directory:
    mkdir -p variants/orchestrator-v2 && $EDITOR variants/orchestrator-v2/orchestrator.md
    # on a GPU job with vLLM (VLLM_BASE_URL) -- rc_kg is started if RC_KG_URL is unset:
    backend/.venv/bin/python evals/prompt_ab.py variants/orchestrator-v2 [--suite e2e --suite routing]

Runs evals/run.py twice -- baseline prompts, then with RC_AGENT_PROMPTS
pointing at the variant (files there replace same-named prompts, nothing
else) -- and writes the comparison to traces/$USER/evals/<time>-prompt-ab/:
every metric for both, the change, and which ground-truth cases flipped
pass/fail. Adopt a variant only if it wins on the metric it targets without
losing elsewhere; then copy its files into prompts/ (their new versions show
in every trace from then on).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))
from app import ops_store  # noqa: E402

PY = sys.executable


def run(suites, env) -> Path:
    before = set((ops_store.root() / "evals").glob("*")) if (ops_store.root() / "evals").exists() else set()
    cmd = [PY, str(HERE / "run.py")] + sum((["--suite", s] for s in suites), [])
    subprocess.run(cmd, env=env, check=False)
    new = sorted(set((ops_store.root() / "evals").glob("*")) - before)
    if not new:
        raise SystemExit("evals/run.py wrote no report")
    return new[-1] / "report.json"


def flat(report):
    return {"%s.%s" % (s, k): v for s, b in report["suites"].items() for k, v in b["metrics"].items()}


def passed(report):
    out = {}
    for s, b in report["suites"].items():
        for r in b["rows"]:
            bad = (r.get("hit") is False or r.get("route_ok") is False or r.get("skills_ok") is False
                   or r.get("forbidden") or r.get("reply_failed") or r.get("asks_missing") or r.get("error"))
            out["%s/%s" % (s, r["id"])] = not bad
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("variant", help="a directory of prompt files (same names as backend/app/agents/prompts)")
    ap.add_argument("--suite", action="append", help="default: e2e")
    args = ap.parse_args()
    variant = Path(args.variant).resolve()
    if not list(variant.glob("*.md")):
        raise SystemExit("no .md prompt files in %s" % variant)
    suites = args.suite or ["e2e"]
    base_env = {k: v for k, v in os.environ.items() if k != "RC_AGENT_PROMPTS"}
    a = json.loads(run(suites, base_env).read_text())
    b = json.loads(run(suites, dict(base_env, RC_AGENT_PROMPTS=str(variant))).read_text())
    fa, fb, pa, pb = flat(a), flat(b), passed(a), passed(b)
    lines = ["# Prompt A/B: %s" % variant.name, "", "Variant files: %s" % ", ".join(p.name for p in variant.glob("*.md")),
             "", "| metric | baseline | variant | change |", "|---|---|---|---|"]
    for k in fa:
        va, vb = fa.get(k), fb.get(k)
        d = round(vb - va, 3) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else ""
        lines.append("| %s | %s | %s | %s |" % (k, va, vb, d))
    flips = [(k, pa[k], pb.get(k)) for k in pa if pb.get(k) is not None and pb[k] != pa[k]]
    lines += ["", "Cases that flipped: %d" % len(flips)] + ["- %s: %s -> %s" % (k, "pass" if x else "fail",
                                                                                   "pass" if y else "fail")
                                                            for k, x, y in flips]
    out = ops_store.new_eval_dir("prompt-ab-" + variant.name)
    (out / "comparison.md").write_text("\n".join(lines) + "\n")
    (out / "baseline.json").write_text(json.dumps(a, indent=1))
    (out / "variant.json").write_text(json.dumps(b, indent=1))
    print("\n".join(lines))
    print("\nreport: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
