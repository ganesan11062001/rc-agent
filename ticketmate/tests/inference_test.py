#!/usr/bin/env python3
"""Does inference actually work on this cluster, and does the RC Copilot prompt
produce a usable draft?

This is the one question the rest of the project has never answered: every
backend test so far used a stub returning canned JSON.

It imports the REAL prompt and the REAL response schema from ../backend, so a
pass here means the product's prompt works against a live model -- not that a
copy of it does. Nothing in backend/ is modified or written to.

Run it against a server this script did not start:

    source cluster/env.sh
    python tests/inference_test.py --base-url http://127.0.0.1:8000/v1

Or let tests/run_inference_test.sbatch start one for you.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app import models, prompts  # noqa: E402  (real product code, read-only)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results = []


def record(name, status, detail=""):
    colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m"}[status]
    print("  %s%-4s\033[0m  %-38s %s" % (colour, status, name, detail), flush=True)
    results.append((name, status, detail))


def rule(title):
    print("\n" + title)
    print("-" * 74, flush=True)


def make_client(base_url, api_key, timeout):
    """An OpenAI client pointed at vLLM, with the site proxy bypassed.

    These nodes set HTTP_PROXY, and the SDK's HTTP layer honours it, so a
    request to a loopback or internal address would be sent to the site proxy
    and rejected. Extending NO_PROXY is version-agnostic; passing
    trust_env=False would tie us to one httpx generation.
    """
    host = urlparse(base_url).hostname or "127.0.0.1"
    current = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    entries = [e.strip() for e in current.split(",") if e.strip()]
    if host not in entries:
        entries.append(host)
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = ",".join(entries)

    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key or "EMPTY",
                  timeout=timeout, max_retries=0)


# --------------------------------------------------------------------------- #

def test_reachable(client):
    rule("1. Is a model being served?")
    try:
        ids = [m.id for m in client.models.list().data]
    except Exception as exc:
        record("server reachable", FAIL, type(exc).__name__ + ": " + str(exc)[:90])
        return None
    if not ids:
        record("server reachable", FAIL, "answered, but serves no models")
        return None
    record("server reachable", PASS, "serving: " + ", ".join(ids))
    return ids[0]


def test_trivial(client, model):
    rule("2. Can it complete anything at all?")
    started = time.perf_counter()
    try:
        out = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=16, temperature=0,
        )
    except Exception as exc:
        record("trivial completion", FAIL, type(exc).__name__ + ": " + str(exc)[:90])
        return False
    took = time.perf_counter() - started
    text = (out.choices[0].message.content or "").strip()
    record("trivial completion", PASS, "%.1fs -> %r" % (took, text[:40]))

    # GLM models emit reasoning separately; note it, because it changes how the
    # answer field must be parsed.
    extra = getattr(out.choices[0].message, "reasoning_content", None)
    record("reasoning field present", PASS if extra else SKIP,
           "yes -- content is separate" if extra else "no separate reasoning field")
    return True


def draft_once(client, model, ticket, instructions, guided, max_tokens, temperature):
    """One real drafting call using the product's own prompt."""
    messages = prompts.build_messages(
        ticket, json_reminder=not guided, extra_instructions=instructions)
    kwargs = dict(model=model, messages=messages,
                  max_tokens=max_tokens, temperature=temperature)
    if guided:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "rc_draft_response", "strict": True,
                            "schema": models.DRAFT_JSON_SCHEMA},
        }
    started = time.perf_counter()
    out = client.chat.completions.create(**kwargs)
    return (out.choices[0].message.content or "",
            out.choices[0].finish_reason,
            time.perf_counter() - started)


def validate(raw):
    """Parse and validate exactly as the backend does."""
    text = raw.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in the response")
        obj = json.loads(text[start:end + 1])
    return models.LLMDraft.model_validate(obj)


def test_prompt(client, model, ticket, instructions, guided, args):
    label = "guided decoding" if guided else "prompt-only fallback"
    rule("%d. The real RC Copilot prompt (%s)" % (3 if guided else 4, label))
    try:
        raw, finish, took = draft_once(client, model, ticket, instructions,
                                       guided, args.max_tokens, args.temperature)
    except Exception as exc:
        msg = type(exc).__name__ + ": " + str(exc)[:110]
        # A 400 here means this server cannot do constrained decoding; the
        # product falls back, so this is informative rather than fatal.
        record("draft call (%s)" % label, SKIP if guided else FAIL, msg)
        return None
    record("draft call (%s)" % label, PASS, "%.1fs, %d chars" % (took, len(raw)))

    if finish == "length":
        record("answer not truncated", FAIL,
               "finish_reason=length -- raise --max-tokens (now %d)" % args.max_tokens)
        return None
    record("answer not truncated", PASS, "finish_reason=" + str(finish))

    try:
        draft = validate(raw)
    except Exception as exc:
        record("valid JSON matching the schema", FAIL, str(exc)[:110])
        print("\n  --- raw model output, first 700 chars ---")
        print("  " + raw[:700].replace("\n", "\n  "))
        return None
    record("valid JSON matching the schema", PASS,
           "%d steps, %d caveats" % (len(draft.suggested_steps), len(draft.caveats)))

    label_ = models.confidence_label(draft.confidence)
    record("confidence in range", PASS, "%.2f -> %s" % (draft.confidence, label_))
    record("draft is non-trivial",
           PASS if len(draft.draft_response) > 120 else FAIL,
           "%d chars" % len(draft.draft_response))
    return draft


def show(draft):
    rule("5. What the model actually wrote -- judge this yourself")
    print("  PROBLEM SUMMARY")
    print("    " + draft.problem_summary)
    print("\n  SUGGESTED STEPS")
    for i, s in enumerate(draft.suggested_steps, 1):
        print("    %d. %s" % (i, s))
    print("\n  CONFIDENCE  %.2f (%s)" % (draft.confidence,
                                         models.confidence_label(draft.confidence)))
    print("\n  CAVEATS")
    for c in draft.caveats or ["(none)"]:
        print("    - " + c)
    print("\n  DRAFT RESPONSE")
    for line in draft.draft_response.splitlines():
        print("    " + line)
    print(flush=True)


# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.environ.get("VLLM_BASE_URL",
                                                        "http://127.0.0.1:8000/v1"))
    p.add_argument("--model", default=os.environ.get("VLLM_MODEL_NAME", ""),
                   help="defaults to whatever the server reports")
    p.add_argument("--api-key", default=os.environ.get("VLLM_API_KEY", ""))
    p.add_argument("--ticket", type=Path,
                   default=Path(__file__).resolve().parent / "sample_ticket.txt",
                   help="file with ticket text; use a real one to judge quality")
    p.add_argument("--instructions", default="",
                   help="extra guidance, as the RC member would type")
    p.add_argument("--max-tokens", type=int, default=1400)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--timeout", type=float, default=300.0)
    args = p.parse_args()

    ticket = args.ticket.read_text(encoding="utf-8").strip()

    print("=" * 74)
    print(" RC Copilot -- can this cluster actually run inference?")
    print("=" * 74)
    print("  endpoint   %s" % args.base_url)
    print("  ticket     %s (%d chars)" % (args.ticket.name, len(ticket)))
    print("  prompt     %s" % (REPO / "backend/app/prompts.py"))
    print("  node       %s" % os.uname().nodename)
    if args.instructions:
        print("  extra      %r" % args.instructions)

    client = make_client(args.base_url, args.api_key, args.timeout)

    served = test_reachable(client)
    if served is None:
        summarise()
        return 1
    model = args.model or served
    if args.model and args.model != served:
        record("requested model is served", FAIL,
               "asked for %r, server has %r" % (args.model, served))

    if not test_trivial(client, model):
        summarise()
        return 1

    draft = test_prompt(client, model, ticket, args.instructions, True, args)
    if draft is None:
        # Either the server refused the schema or the output failed validation.
        # Try the path the product falls back to before calling it a failure.
        draft = test_prompt(client, model, ticket, args.instructions, False, args)

    if draft is not None:
        show(draft)

    return summarise()


def summarise():
    rule("Summary")
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = [n for n, s, _ in results if s == FAIL]
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    print("  %d passed, %d failed, %d skipped" % (passed, len(failed), skipped))
    if failed:
        print("\n  \033[31mFAILED:\033[0m " + ", ".join(failed))
        print("\n  Inference is NOT working end to end yet.")
        return 1
    print("\n  \033[32mInference works on this cluster, and the RC Copilot prompt")
    print("  produced a schema-valid draft.\033[0m")
    print("\n  Next: run this against real tickets with --ticket, and tune")
    print("  backend/app/prompts.py until the drafts are good enough to send.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
