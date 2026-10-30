"""A run log as a person reads it: one Markdown page per draft.

Every draft has two records, side by side (app/runlog.py):

    <time>-<id>.json   the full trace -- every run, prompt, token, timing and
                       score, machine-readable, for tools and audits
    <time>-<id>.md     this rendering: what the draft was, how it was made and
                       how good it is, in reading order, for a person

The JSON is the source of truth; this page is derived from it (and from the
feedback beside it) and can be re-rendered at any time:
`cluster/traces.py md <id>`. Standard library only, so the cluster tools can
import it without the backend's environment. Missing fields (older logs) are
skipped, never an error.
"""

import json
from typing import Any, Dict, List, Optional

_OK, _FAIL = "PASS", "FAIL"


def _g(d: Any, *keys: str, default: Any = None) -> Any:
    """d[k1][k2]... or `default` when any step is missing."""
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def _cell(v: Any, limit: int = 120) -> str:
    """A value fit for one table cell: one line, pipes escaped, clipped."""
    s = "" if v is None else str(v)
    s = " ".join(s.split()).replace("|", "\\|")
    return s if len(s) <= limit else s[:limit - 1] + "..."


def _table(head: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _fence(text: str, lang: str = "") -> str:
    text = (text or "").rstrip()
    fence = "````" if "```" in text else "```"
    return "%s%s\n%s\n%s\n" % (fence, lang, text, fence)


def _details(summary: str, body: str) -> str:
    return "<details><summary>%s</summary>\n\n%s\n</details>\n" % (summary, body)


def _score(v: Any) -> str:
    if isinstance(v, bool):
        return _OK if v else _FAIL
    if isinstance(v, float):
        return "%.2f" % v
    return "" if v is None else str(v)


def _run_tree(runs: List[Dict[str, Any]]) -> str:
    """The run tree as an indented outline: name (type) -- ms, status."""
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for r in runs:
        children.setdefault(r.get("parent_id"), []).append(r)
    lines: List[str] = []

    def walk(parent: Optional[str], depth: int) -> None:
        for r in children.get(parent, []):
            status = r.get("status") or ""
            err = " -- **%s**" % _cell(r.get("error"), 80) if r.get("error") else ""
            lines.append("%s- `%s` (%s) %s ms%s%s" % (
                "  " * depth, r.get("name"), r.get("run_type"), _score(r.get("latency_ms")),
                "" if status in ("", "success") else ", " + status, err))
            walk(r.get("id"), depth + 1)
    walk(None, 0)
    return "\n".join(lines) + "\n" if lines else ""


def render(run: Dict[str, Any], feedback: Optional[List[Dict[str, Any]]] = None) -> str:
    """The Markdown page for one run log (the parsed .json) and its feedback rows."""
    tr = run.get("trace") or {}
    draft = run.get("draft") or {}
    ev = draft.get("evaluations") or {}
    judge = draft.get("judge") or {}
    tokens = tr.get("tokens") or {}
    rid = run.get("request_id", "?")
    out: List[str] = []
    add = out.append

    add("# Draft %s -- %s\n" % (rid, run.get("ticket_number", "?")))
    verdict = "error: " + str(run.get("error")) if run.get("error") else (
        "%d assertion(s) failed" % len(ev.get("assertions_failed") or []) if ev.get("assertions_failed")
        else "all assertions passed")
    if judge.get("verdict"):
        verdict += "; LLM judge: %s%s" % (judge["verdict"], " (rewritten once)" if judge.get("rewritten") else "")
    add(_table(["", ""], [
        ["when", run.get("at")],
        ["outcome", verdict],
        ["model", "%s (%s)" % (run.get("model"), run.get("strategy"))],
        ["confidence", "%s (%s)" % (draft.get("confidence"), draft.get("confidence_label"))],
        ["latency", "%s ms" % run.get("latency_ms")],
        ["tokens", "%s total (%s prompt, %s completion) over %s model call(s)" % (
            tokens.get("total_tokens"), tokens.get("prompt_tokens"), tokens.get("completion_tokens"),
            tokens.get("model_calls"))],
        ["where", "node %s, Slurm job %s" % (run.get("node"), run.get("slurm_job_id"))],
        ["versions", "TicketMate %s; rc-kg API %s, graph %s" % (
            _g(tr, "versions", "ticket_mate", "commit"), _g(tr, "versions", "rc_kg", "api_version"),
            _g(tr, "versions", "rc_kg", "snapshot", "built_at"))],
        ["full trace", "the .json beside this file (request id %s)" % rid],
    ]))

    if run.get("error"):
        add("## Error\n\n%s\n" % run["error"])

    if draft:
        add("## The reply\n")
        add(_fence(draft.get("draft_response") or ""))
        add("## For the engineer\n")
        add("**Summary.** %s\n" % (draft.get("problem_summary") or ""))
        if draft.get("suggested_steps"):
            add("**Steps**\n\n" + "\n".join("%d. %s" % (i, s) for i, s in
                                           enumerate(draft["suggested_steps"], 1)) + "\n")
        if draft.get("caveats"):
            add("**Check before sending**\n\n" + "\n".join("- " + c for c in draft["caveats"]) + "\n")
        if draft.get("references"):
            add("**Cited pages**\n\n" + "\n".join("%s. [%s](%s)" % (r.get("n"), r.get("title"), r.get("url"))
                                                for r in draft["references"]) + "\n")

    # Quality: code checks, measures, the judge, people.
    add("## Quality\n")
    checks = ev.get("assertions") or {}
    if checks:
        add("**Assertions** (the prompt's rules, checked in code)\n")
        add(_table(["check", "result"], [[k.replace("assert_", ""), _score(v)] for k, v in checks.items()]))
    if ev.get("metrics"):
        add("**Metrics**\n")
        add(_table(["metric", "value"], [[k, _score(v)] for k, v in ev["metrics"].items()]))
    if judge:
        add("**LLM judge** (%s mode, %s)\n" % (judge.get("mode"), judge.get("verdict")))
        add(_table(["criterion", "score", "why"], [
            [k, _score(v.get("score")), v.get("comment")] for k, v in (judge.get("scores") or {}).items()]))
        if judge.get("rubric"):
            add("Request-type rubric:\n")
            add(_table(["requirement", "met", "why"], [
                [r.get("item"), {True: "yes", False: "NO", None: "n/a"}.get(r.get("met"), r.get("met")),
                 r.get("reason")] for r in judge["rubric"]]))
        if judge.get("reasons"):
            add("Why it was flagged: " + "; ".join(judge["reasons"]) + "\n")
        if judge.get("first"):
            add("The first draft was judged %s (%s); rewrite: %s.\n" % (
                _g(judge, "first", "verdict"), "; ".join(_g(judge, "first", "reasons", default=[]) or []),
                judge.get("rewrite") or ("kept" if judge.get("rewritten") else "not used")))
    if feedback:
        add("**Feedback** (people, and the background judge)\n")
        add(_table(["at", "source", "by", "key", "score", "comment"], [
            [f.get("created_at"), f.get("source"), f.get("by"), f.get("key"), _score(f.get("score")),
             f.get("comment") or f.get("value")] for f in feedback]))

    # How it was made, in pipeline order.
    add("## How it was made\n")
    timings = tr.get("timings_ms") or {}
    if timings:
        add(_table(["stage", "ms"], [[k, v] for k, v in timings.items()]))
    ctx = tr.get("ticket_context") or {}
    if ctx:
        add("**Ticket cleaning.** %s -> %s characters; %s addresses, %s notifications, %s signatures/quotes "
            "removed; %s entries kept, %s omitted to fit; budget %s tokens of a %s-token window.\n" % (
                ctx.get("raw_chars"), ctx.get("chars"), ctx.get("addresses_removed", 0),
                ctx.get("notifications_removed", 0),
                (ctx.get("signatures_removed") or 0) + (ctx.get("quoted_chains_removed") or 0),
                ctx.get("entries_kept"), ctx.get("entries_omitted", 0), ctx.get("budget_tokens"),
                ctx.get("context_window")))
    inst = _g(run, "input", "extra_instructions")
    if inst:
        add("**Engineer's instructions**\n\n" + _fence(inst))
    if tr.get("cluster_lookups"):
        add("**Cluster lookups** (read-only)\n")
        add(_table(["command", "ok", "ms", "summary"], [
            [lk.get("command"), _score(lk.get("ok")), lk.get("ms"), lk.get("summary")] for lk in tr["cluster_lookups"]]))
    if tr.get("playbooks"):
        add("**Playbooks**\n")
        for pb in tr["playbooks"]:
            src = pb.get("value_sources") or {}
            vals = "; ".join("%s = %s (%s)" % (k, ", ".join(v), src.get(k, "text"))
                             for k, v in (pb.get("values") or {}).items()) or "none"
            add("- **%s** -- values: %s%s\n" % (pb.get("title") or pb.get("name"), vals,
                                                 "; missing: " + "; ".join(pb["missing"]) if pb.get("missing") else ""))
    ret = tr.get("retrieval") or {}
    if ret:
        add("**Retrieval** -- %s, %s ms%s\n" % (ret.get("status"), ret.get("latency_ms"),
                                                 ", LOW CONFIDENCE" if ret.get("low_confidence") else ""))
        add(_table(["ref", "page", "section", "score", "vector"], [
            [s.get("ref"), "[%s](%s)" % (_cell(s.get("page_title"), 60), s.get("url")), s.get("heading"),
             _score(s.get("score")), _score(s.get("vector_score"))] for s in ret.get("sources") or []]))
    cit = tr.get("citations") or {}
    if cit:
        bits = ["cited %s" % (", ".join(str(c) for c in cit.get("cited") or []) or "none")]
        if cit.get("removed_urls"):
            bits.append("URLs removed: " + ", ".join(cit["removed_urls"]))
        if cit.get("unknown_refs"):
            bits.append("unknown refs: " + ", ".join(str(r) for r in cit["unknown_refs"]))
        flagged = [s for s in cit.get("support") or [] if s.get("verdict") != "ok"]
        if flagged:
            bits.append("%d citation(s) flagged by the support check" % len(flagged))
        add("**Citations** -- " + "; ".join(bits) + "\n")
    attempts = _g(tr, "llm", "attempts", default=[]) or []
    if attempts:
        add("**Model calls**\n")
        add(_table(["#", "phase", "strategy", "outcome", "prompt tok", "completion tok", "ms", "finish"], [
            [a.get("n"), a.get("phase", "draft"), a.get("strategy"), a.get("outcome"),
             _g(a, "usage", "prompt_tokens"), _g(a, "usage", "completion_tokens"), a.get("latency_ms"),
             a.get("finish_reason")] for a in attempts]))
    for j in tr.get("judge_calls") or []:
        add("- judge call (%s): %s ms, %s tokens%s\n" % (j.get("phase"), j.get("latency_ms"),
                                                       _g(j, "usage", "total_tokens"),
                                                       ", error: " + j["error"] if j.get("error") else ""))
    runs = _g(tr, "run_tree", "runs", default=[]) or []
    if runs:
        add("**Run tree**\n\n" + _run_tree(runs))

    # The exact prompt and output, folded: long, but what reproduces the draft.
    prompt = run.get("prompt") or []
    if prompt:
        add("## Exact prompt and output\n")
        for m in prompt:
            add(_details("%s message (%d chars)" % (m.get("role"), len(m.get("content") or "")),
                         _fence(m.get("content") or "")))
        if run.get("raw_response"):
            add(_details("model output (raw)", _fence(run["raw_response"], "json")))
    return "\n".join(out)


def render_file(json_path: str) -> str:
    """Render the run log at `json_path` with the feedback beside it."""
    with open(json_path, encoding="utf-8") as fh:
        run = json.load(fh)
    feedback: List[Dict[str, Any]] = []
    try:
        with open(json_path[:-len(".json")] + ".feedback.jsonl", encoding="utf-8") as fh:
            feedback = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        pass
    return render(run, feedback)
