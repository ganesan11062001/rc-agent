"""The vLLM startup parser against a real vLLM 0.29 startup log, replayed one
line at a time as it would be read while the model loads.

  cd backend && .venv/bin/python ../tests/startup/test_vllm_progress.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "backend"))
import app.startup as st  # noqa: E402

lines = open(os.path.join(HERE, "vllm-0.29-startup.log"), encoding="utf-8").read().splitlines(keepends=True)
tmp = tempfile.NamedTemporaryFile("w", delete=False)
stages, pcts, details = [], [], []
for line in lines:
    tmp.write(line); tmp.flush()
    p = st.vllm_progress(tmp.name, False)
    stages.append(p["stage"]); pcts.append(p["pct"]); details.append(p["detail"] or "")
os.unlink(tmp.name)

order = [k for k, *_ in st.VLLM_STAGES]
seen = list(dict.fromkeys(stages))
fails = []
def check(c, what):
    print(("  ok    " if c else "  FAIL  ") + what)
    if not c: fails.append(what)
check(seen == ["starting", "engine", "weights", "compile", "profile", "graphs", "server"], "stages in order: %s" % seen)
check(all(a <= b for a, b in zip(pcts, pcts[1:])), "the bar never moves backwards")
check(any("1/2 weight shards of 5.75 GiB" in d for d in details), "weight shards counted, with checkpoint size")
check(any(d.startswith("pass 4 ·") for d in details), "all four CUDA-graph passes seen")
check("failed" not in stages, "a healthy log is never reported as failed")
check(st.vllm_progress(None, True)["stage"] == "ready", "reachable means ready")
bad = tempfile.NamedTemporaryFile("w", delete=False)
bad.write("".join(lines[:45]) + "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB\n"); bad.close()
p = st.vllm_progress(bad.name, False); os.unlink(bad.name)
check(p["failed"] and "out of memory" in (p["detail"] or "").lower(), "an OOM is reported as failed, with the message")
print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
