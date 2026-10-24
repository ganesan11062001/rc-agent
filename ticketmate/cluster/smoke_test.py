#!/usr/bin/env python
"""Smoke-test a running vLLM server.

    source env.sh
    python smoke_test.py                  # against localhost:8000
    python smoke_test.py --model glm-4.5-air
"""

import argparse
import json
import urllib.error
import urllib.request


# These nodes set http_proxy, and urllib would otherwise send even localhost
# traffic to the site proxy, which rejects it with a 403. An empty ProxyHandler
# disables proxying for this client entirely.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000/v1")
    p.add_argument("--model", default="glm-4.7-flash")
    args = p.parse_args()

    # Which models the server actually has loaded.
    try:
        with _opener.open(f"{args.base_url}/models", timeout=30) as r:
            served = [m["id"] for m in json.load(r)["data"]]
    except urllib.error.URLError as e:
        print(f"cannot reach {args.base_url}: {e.reason}")
        print("is the server up? check: squeue -u $USER  and  logs/glm-vllm-*.out")
        return 1
    print(f"served models: {served}")

    if args.model not in served:
        print(f"warning: '{args.model}' not in served models, using '{served[0]}'")
        args.model = served[0]

    body = json.dumps(
        {
            "model": args.model,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 64,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        f"{args.base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with _opener.open(req, timeout=300) as r:
        msg = json.load(r)["choices"][0]["message"]

    # GLM models always emit a reasoning block; the parser splits it out.
    if msg.get("reasoning_content") or msg.get("reasoning"):
        print("reasoning parser: active")
    print(f"content: {msg.get('content')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
