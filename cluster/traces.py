#!/usr/bin/env python3
"""Read RC Copilot's per-draft traces (the run logs, one JSON per draft).

  traces.py list                       newest drafts, one line each
  traces.py list --since 2026-10-24 --ticket INC
  traces.py show <request_id>          everything about one draft, with its run tree
  traces.py show <request_id> --io     ...plus every run's inputs and outputs
  traces.py show <request_id> --prompt ...plus the exact prompt of every model call
  traces.py runs --type llm --status error        runs across all drafts, filtered
  traces.py runs --name rc_kg.retrieve --min-ms 1000
  traces.py runs --meta strategy=json_object --tag kg:ok
  traces.py stats [--by model|prompt_version|project|day|thread]
                                       per-step latency p50/p95, tokens, errors, feedback
  traces.py feedback <request_id> --key thumbs --score 1 [--comment ...] [--correction ...]
  traces.py export drafts.csv          one row per draft (no ticket text)

The model is LangSmith's: each draft is a trace, a tree of runs (chain,
retriever, embedding, llm, parser, tool), each with id, parent, start/end,
latency, status, error, inputs, outputs, metadata and tags; traces carry a
project (the graph variant), session (Slurm job) and thread (the ticket);
feedback -- automatic at draft time, or a person's via /api/feedback or this
script -- is attached by run id.

Run logs live in /scratch/$USER/rc_copilot_runs (or $RUN_LOG_DIR), mode 700:
only you can read your own. Each holds what went into the model (ticket,
engineer instructions, retrieved RC sources, the exact messages of every
call), what came out (raw output, the parsed draft, the final reply after
citation rendering), every model call's token usage, timings per stage, and
the versions of TicketMate, rc_kg and the graph that produced it. Logs from
before trace_version 2 show what they have.

Note: /scratch is purged monthly. Copy logs you need to keep somewhere
private (mode 700) before then.
"""

import argparse
import csv
import json
import os
import statistics
import sys
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("RUN_LOG_DIR") or "/scratch/%s/rc_copilot_runs" % os.environ.get("USER", ""))


def load(run_dir: Path, since: str = "", ticket: str = ""):
    runs = []
    for p in run_dir.rglob("*.json"):
        if ticket and not p.parent.name.startswith(ticket.upper()):
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if since and (r.get("at") or "") < since:
            continue
        r["_path"] = str(p)
        fb_path = p.with_suffix(".feedback.jsonl")
        r["_feedback"] = list(((r.get("trace") or {}).get("run_tree") or {}).get("feedback") or [])
        if fb_path.exists():
            for line in fb_path.read_text(encoding="utf-8").splitlines():
                try:
                    r["_feedback"].append(json.loads(line))
                except ValueError:
                    pass
        runs.append(r)
    return sorted(runs, key=lambda r: r.get("at") or "")


def tree(r):
    return (((r.get("trace") or {}).get("run_tree")) or {}).get("runs") or []


def root_meta(r):
    t = tree(r)
    return (t[0].get("metadata") or {}) if t else {}


def print_tree(r, io=False):
    runs = tree(r)
    if not runs:
        print("(no run tree: trace_version %s)" % r.get("trace_version", 1)); return
    kids = {}
    for x in runs:
        kids.setdefault(x.get("parent_id"), []).append(x)

    def walk(x, depth):
        u = x.get("usage") or {}
        tok = (" tokens %s+%s=%s" % (u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"))
               if u.get("total_tokens") else "")
        flag = "" if x.get("status") == "success" else "  [%s] %s" % (x.get("status"), (x.get("error") or "")[:90])
        derived = " (derived)" if (x.get("metadata") or {}).get("derived") else ""
        print("  %s%-*s %-10s %8s ms%s%s%s" % ("  " * depth, 34 - 2 * depth, x["name"], x["run_type"],
                                           x.get("latency_ms"), tok, derived, flag))
        if io:
            for label, d in (("in", x.get("inputs")), ("out", x.get("outputs"))):
                if d:
                    txt = json.dumps(d, ensure_ascii=False)
                    print("  %s    %s: %s" % ("  " * depth, label, txt[:300] + ("…" if len(txt) > 300 else "")))
        for c in kids.get(x["id"], []):
            walk(c, depth + 1)
    for root in kids.get(None, []):
        walk(root, 0)


def tokens(r):
    t = (r.get("trace") or {}).get("tokens") or {}
    return t.get("prompt_tokens"), t.get("completion_tokens"), t.get("total_tokens"), t.get("model_calls")


def kg(r):
    ret = (r.get("trace") or {}).get("retrieval") or {}
    return ret.get("status") or ("-" if "trace" not in r else "?")


def refs(r):
    return len(((r.get("draft") or {}).get("references")) or [])


def flagged(r):
    return sum(1 for c in ((r.get("draft") or {}).get("caveats") or []) if c.startswith("Check citation"))


def cmd_list(runs, _args):
    print("%-25s %-9s %-16s %-26s %-11s %4s %4s %7s %7s %7s %5s %7s  %s" % (
        "at", "id", "ticket", "model", "kg", "src", "refs", "prompt", "compl", "total", "calls", "ms", "error"))
    for r in runs[-_args.n:]:
        p, c, t, n = tokens(r)
        src = len(((r.get("trace") or {}).get("retrieval") or {}).get("sources") or [])
        print("%-25s %-9s %-16s %-26s %-11s %4s %4s %7s %7s %7s %5s %7s  %s" % (
            (r.get("at") or "")[:25], r.get("request_id", ""), r.get("ticket_number", "")[:16],
            (r.get("model") or "")[:26], kg(r), src, refs(r), p or "-", c or "-", t or "-", n or "-",
            r.get("latency_ms", "-"), (r.get("error") or "")[:40]))


def _find(runs, key):
    hits = [r for r in runs if r.get("request_id") == key or r["_path"].endswith(key)]
    if not hits:
        sys.exit("no run with id %r" % key)
    return hits[-1]


def cmd_show(runs, args):
    r = _find(runs, args.id)
    tr = r.get("trace") or {}
    print("draft %s  %s  ticket %s  model %s" % (r.get("request_id"), r.get("at"), r.get("ticket_number"), r.get("model")))
    print("log: %s   (trace_version %s)" % (r["_path"], r.get("trace_version", 1)))
    v = tr.get("versions") or {}
    snap = ((v.get("rc_kg") or {}).get("snapshot")) or {}
    print("versions: ticket_mate %s | rc_kg API %s | graph %s built %s (commit %s)" % (
        (v.get("ticket_mate") or {}).get("commit"), (v.get("rc_kg") or {}).get("api_version"),
        snap.get("variant"), snap.get("built_at"), snap.get("git_commit")))
    inp = r.get("input") or {}
    m = root_meta(r)
    if m:
        print("project %s | session %s | thread %s | node %s | gpu %s | prompt %s" % (
            m.get("project"), m.get("session_id"), m.get("thread_id"), m.get("node"), m.get("gpu") or "-",
            (m.get("prompt_version") or {}).get("version")))
        rt = tree(r)
        print("tags: %s" % (rt[0].get("tags") if rt else []))
    print("\n── RUN TREE ──")
    print_tree(r, args.io)
    print("\n── IN ──\nticket (%d chars):\n%s" % (inp.get("chars", 0), inp.get("ticket_text", "")))
    if inp.get("extra_instructions"):
        print("engineer instructions:\n%s" % inp["extra_instructions"])
    ret = tr.get("retrieval") or {}
    if ret:
        rk = ((ret.get("rc_kg") or {}).get("retrieval")) or {}
        print("\nretrieval: %s in %s ms (rc_kg %s ms)  topics %s  low_confidence %s  best vector %s" % (
            ret.get("status"), ret.get("latency_ms"), (rk.get("timings_ms") or {}).get("total"),
            ret.get("query_topics"), ret.get("low_confidence"), rk.get("best_vector_score")))
        for s in ret.get("sources") or []:
            print("  %-4s %.4f vec=%s %-22s %s > %s\n       %s" % (
                s["ref"], s.get("score") or 0, s.get("vector_score"),
                ",".join("%s%s" % (k[0], n) for k, n in (s.get("signals") or {}).items()),
                s.get("page_title"), s.get("heading"), s.get("url")))
    llm = tr.get("llm") or {}
    if llm:
        print("\nmodel calls (params %s):" % llm.get("params"))
        for a in llm.get("attempts") or []:
            u = a.get("usage") or {}
            print("  #%s %-12s %-12s %6s ms  finish=%-6s tokens prompt %s + completion %s = %s%s" % (
                a.get("n"), a.get("strategy"), a.get("outcome"), a.get("latency_ms"), a.get("finish_reason"),
                u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
                ("   error: " + a["error"]) if a.get("error") else ""))
            if args.prompt:
                for m in a.get("messages") or []:
                    print("     ── %s ──\n%s" % (m["role"], m["content"]))
    t = tr.get("tokens") or {}
    if t:
        print("\ntokens: prompt %s + completion %s = %s over %s call(s)" % (
            t.get("prompt_tokens"), t.get("completion_tokens"), t.get("total_tokens"), t.get("model_calls")))
        if t.get("prompt_breakdown"):
            print("  prompt by part: %s" % ", ".join("%s %s" % kv for kv in t["prompt_breakdown"].items()))
    print("\n── OUT ──\nraw model output:\n%s" % (r.get("raw_response") or ""))
    cit = tr.get("citations") or {}
    if cit:
        print("\ncitations: %s in %s ms  references %d  removed URLs %s  unknown refs %s" % (
            cit.get("status"), cit.get("latency_ms"), len(cit.get("references") or []),
            cit.get("removed_urls"), cit.get("unknown_refs")))
        for v_ in cit.get("support") or []:
            if v_["verdict"] != "ok":
                print("  flagged %s (%s): %s" % (v_["ref"], v_["verdict"], v_["claim"][:100]))
    d = r.get("draft") or {}
    if d:
        print("\nfinal reply:\n%s" % d.get("draft_response", ""))
        print("caveats: %s" % d.get("caveats"))
    print("\ntimings ms: %s" % (tr.get("timings_ms") or {"total": r.get("latency_ms")}))
    if r["_feedback"]:
        print("\nfeedback:")
        for f in r["_feedback"]:
            print("  %-26s %-6s %-6s %s %s" % (f["key"], f["score"], f.get("source"), f.get("created_at", ""),
                                             ("— " + f["comment"]) if f.get("comment") else ""))
    if r.get("error"):
        print("ERROR: %s" % r["error"])


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def _all_runs(runs):
    for r in runs:
        for x in tree(r):
            yield r, x


def cmd_runs(runs, args):
    want_meta = dict(kv.split("=", 1) for kv in args.meta)
    rows = []
    for r, x in _all_runs(runs):
        if args.type and x["run_type"] != args.type: continue
        if args.name and x["name"] != args.name: continue
        if args.status and x.get("status") != args.status: continue
        if args.min_ms and (x.get("latency_ms") or 0) < args.min_ms: continue
        tags = set(x.get("tags") or []) | set(tree(r)[0].get("tags") or [])
        if args.tag and args.tag not in tags: continue
        meta = {**root_meta(r), **(x.get("metadata") or {})}
        if any(str(meta.get(k)) != v for k, v in want_meta.items()): continue
        rows.append((r, x))
    print("%-25s %-9s %-26s %-10s %-8s %9s %7s  %s" % ("start", "draft", "run", "type", "status", "ms", "tokens", "error"))
    for r, x in rows[-args.n:]:
        print("%-25s %-9s %-26s %-10s %-8s %9s %7s  %s" % (
            (x.get("start_time") or "")[:25], r.get("request_id"), x["name"][:26], x["run_type"], x.get("status"),
            x.get("latency_ms"), (x.get("usage") or {}).get("total_tokens") or "", (x.get("error") or "")[:60]))
    print("%d run(s)" % len(rows))


def _group_key(r, by):
    m = root_meta(r)
    if by == "prompt_version":
        return (m.get("prompt_version") or {}).get("version") or "?"
    if by == "day":
        return (r.get("at") or "")[:10]
    if by in ("project", "thread"):
        return m.get("project" if by == "project" else "thread_id") or "?"
    return r.get("model") or "?"


def cmd_stats(runs, _args):
    if _args.by != "model":
        return cmd_stats_by(runs, _args)
    by_model = {}
    for r in runs:
        by_model.setdefault(r.get("model") or "?", []).append(r)
    print("%d drafts in %s" % (len(runs), _args.dir))
    for model, rs in sorted(by_model.items()):
        traced = [r for r in rs if r.get("trace")]
        tot = [tokens(r)[2] for r in traced if tokens(r)[2]]
        pro = [tokens(r)[0] for r in traced if tokens(r)[0]]
        com = [tokens(r)[1] for r in traced if tokens(r)[1]]
        lat = [r.get("latency_ms") for r in rs]
        print("\n%s: %d drafts (%d traced), %d failed" % (model, len(rs), len(traced), sum(1 for r in rs if r.get("error"))))
        if tot:
            print("  tokens total %d | per draft mean %.0f p95 %s | prompt mean %.0f | completion mean %.0f" % (
                sum(tot), statistics.mean(tot), _pct(tot, 0.95), statistics.mean(pro), statistics.mean(com)))
            extra = sum(1 for r in traced if (tokens(r)[3] or 1) > 1)
            print("  drafts needing more than one model call (strategy fallback): %d" % extra)
        print("  latency ms p50 %s p95 %s" % (_pct(lat, 0.5), _pct(lat, 0.95)))
        for stage in ("retrieve", "llm", "citations"):
            xs = [((r.get("trace") or {}).get("timings_ms") or {}).get(stage) for r in traced]
            if any(x is not None for x in xs):
                print("    %-10s p50 %s p95 %s" % (stage, _pct(xs, 0.5), _pct(xs, 0.95)))
        if traced:
            grounded = sum(1 for r in traced if kg(r) == "ok")
            print("  grounded in rc_kg: %d/%d, with references: %d, with a flagged citation: %d" % (
                grounded, len(traced), sum(1 for r in traced if refs(r)), sum(1 for r in traced if flagged(r))))


def cmd_stats_by(runs, args):
    groups = {}
    for r in runs:
        groups.setdefault(_group_key(r, args.by), []).append(r)
    print("%d drafts in %s, by %s" % (len(runs), args.dir, args.by))
    for key, rs in sorted(groups.items()):
        print("\n%s = %s: %d drafts, %d failed" % (args.by, key, len(rs), sum(1 for r in rs if r.get("error"))))
        tot = [tokens(r)[2] for r in rs if tokens(r)[2]]
        if tot:
            print("  tokens per draft: mean %.0f p95 %s (total %d)" % (statistics.mean(tot), _pct(tot, 0.95), sum(tot)))
        by_name = {}
        for r in rs:
            for x in tree(r):
                by_name.setdefault((x["run_type"], x["name"]), []).append(x)
        for (rtype, name), xs in sorted(by_name.items(), key=lambda kv: kv[0][1]):
            lat = [x.get("latency_ms") for x in xs]
            err = sum(1 for x in xs if x.get("status") == "error")
            print("    %-28s %-10s n=%-4d p50 %8s ms  p95 %8s ms  errors %d" % (name, rtype, len(xs), _pct(lat, 0.5), _pct(lat, 0.95), err))
        fb = {}
        for r in rs:
            for f in r["_feedback"]:
                if isinstance(f.get("score"), (int, float)):
                    fb.setdefault((f["key"], f.get("source")), []).append(f["score"])
        if fb:
            print("  feedback (mean score, n):")
            for (k, src), xs in sorted(fb.items()):
                print("    %-28s %-6s %.3f  n=%d" % (k, src, statistics.mean(xs), len(xs)))


def cmd_feedback(runs, args):
    r = _find(runs, args.id)
    path = Path(r["_path"]).with_suffix(".feedback.jsonl")
    import uuid
    from datetime import datetime
    rec = {"id": uuid.uuid4().hex, "request_id": r.get("request_id"),
           "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "by": os.environ.get("USER", ""), "key": args.key, "score": args.score,
           "comment": args.comment, "correction": args.correction, "source": "user"}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("feedback %s recorded on draft %s (%s)" % (rec["id"][:8], r.get("request_id"), path))


def cmd_export(runs, args):
    cols = ["at", "request_id", "ticket_number", "model", "kg_status", "sources", "references", "flagged_citations",
            "prompt_tokens", "completion_tokens", "total_tokens", "model_calls", "rc_source_tokens",
            "latency_ms", "retrieve_ms", "llm_ms", "citations_ms", "graph_variant", "graph_built_at",
            "ticket_mate_commit", "error"]
    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in runs:
            tr = r.get("trace") or {}
            p, c, t, n = tokens(r)
            tm = tr.get("timings_ms") or {}
            snap = (((tr.get("versions") or {}).get("rc_kg") or {}).get("snapshot")) or {}
            w.writerow([r.get("at"), r.get("request_id"), r.get("ticket_number"), r.get("model"), kg(r),
                        len(((tr.get("retrieval") or {}).get("sources")) or []), refs(r), flagged(r),
                        p, c, t, n, ((tr.get("tokens") or {}).get("prompt_breakdown") or {}).get("rc_sources"),
                        r.get("latency_ms"), tm.get("retrieve"), tm.get("llm"), tm.get("citations"),
                        snap.get("variant"), snap.get("built_at"),
                        ((tr.get("versions") or {}).get("ticket_mate") or {}).get("commit"), r.get("error")])
    os.chmod(args.csv, 0o600)
    print("wrote %d rows to %s (mode 600)" % (len(runs), args.csv))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--since", default="", help="ISO date/time, e.g. 2026-10-24")
    ap.add_argument("--ticket", default="", help="ticket-number prefix")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list"); pl.add_argument("-n", type=int, default=30)
    ps = sub.add_parser("show"); ps.add_argument("id"); ps.add_argument("--prompt", action="store_true")
    ps.add_argument("--io", action="store_true", help="print every run's inputs and outputs")
    pr = sub.add_parser("runs"); pr.add_argument("--type"); pr.add_argument("--name"); pr.add_argument("--status")
    pr.add_argument("--tag"); pr.add_argument("--min-ms", type=float, default=0)
    pr.add_argument("--meta", action="append", default=[], help="key=value, on the run or its trace's root")
    pr.add_argument("-n", type=int, default=50)
    pst = sub.add_parser("stats"); pst.add_argument("--by", default="model",
                                                    choices=["model", "prompt_version", "project", "day", "thread"])
    pf = sub.add_parser("feedback"); pf.add_argument("id"); pf.add_argument("--key", required=True)
    pf.add_argument("--score", type=float, required=True); pf.add_argument("--comment", default="")
    pf.add_argument("--correction", default="")
    pe = sub.add_parser("export"); pe.add_argument("csv")
    args = ap.parse_args()
    if not args.dir.is_dir():
        sys.exit("no run logs at %s" % args.dir)
    runs = load(args.dir, args.since, args.ticket)
    {"list": cmd_list, "show": cmd_show, "runs": cmd_runs, "stats": cmd_stats,
     "feedback": cmd_feedback, "export": cmd_export}[args.cmd](runs, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
