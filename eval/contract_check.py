"""Check a running rc-kg against API_CONTRACT.md.

  python eval/contract_check.py http://127.0.0.1:PORT [TOKEN]

Standard library only. Every field the contract lists is asserted here, so a
change that would break a v1 client fails this before it ships. Run by
scripts/contract_test.sh against a fresh instance.
"""

import json
import sys
import urllib.error
import urllib.request

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
FAILS = []


def call(base, path, body=None, token=None):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with _open(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def expect(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def has(obj, field, typ):
    return isinstance(obj, dict) and field in obj and isinstance(obj[field], typ)


def main() -> int:
    base = sys.argv[1].rstrip("/")
    token = sys.argv[2] if len(sys.argv) > 2 else None

    print("GET /health")
    code, h = call(base, "/health")
    expect(code == 200, "200")
    expect(has(h, "status", str) and h["status"] == "ok", "status == ok")
    expect(has(h, "api_version", str) and h["api_version"].split(".")[0] == "1",
           "api_version major is 1 (got %r)" % (h or {}).get("api_version"))
    for f in ("sections", "embedded", "retrievable"):
        expect(has(h, f, int), "%s: int" % f)
    expect(h is not None and "snapshot" in h, "snapshot present")

    if token:
        print("auth")
        expect(call(base, "/retrieve", {"query": "x"})[0] == 401, "/retrieve without token -> 401")
        expect(call(base, "/retrieve", {"query": "x"}, "wrong")[0] == 401, "wrong token -> 401")

    print("POST /retrieve")
    expect(call(base, "/retrieve", {"query": "   "}, token)[0] == 400, "empty query -> 400")
    code, r = call(base, "/retrieve", {"query": "my job was killed out of memory", "k": 4}, token)
    expect(code == 200, "200")
    for f, t in (("query", str), ("query_topics", list), ("low_confidence", bool),
                 ("context", str), ("allowed_urls", list), ("sources", list)):
        expect(has(r, f, t), "%s: %s" % (f, t.__name__))
    srcs = (r or {}).get("sources") or []
    expect(1 <= len(srcs) <= 4, "k respected (%d sources)" % len(srcs))
    for f, t in (("ref", str), ("url", str), ("page_url", str), ("page_title", str),
                 ("heading", str), ("text", str), ("published", str), ("score", (int, float)),
                 ("last_modified", str), ("site", str)):
        expect(all(has(s, f, t) for s in srcs), "sources[].%s" % f)
    expect([s.get("ref") for s in srcs] == ["S%d" % i for i in range(1, len(srcs) + 1)],
           "refs are S1..Sn in order")
    expect(all(s.get("url") in (r or {}).get("allowed_urls", []) for s in srcs),
           "every source url is in allowed_urls")
    expect(all("[%s]" % s.get("ref") in (r or {}).get("context", "") for s in srcs),
           "context numbers every source")

    print("POST /cite")
    refs = {s["ref"]: s["url"] for s in srcs}
    allowed = (r or {}).get("allowed_urls", [])
    s1, s2 = (srcs + srcs)[0]["url"], (srcs + srcs)[1]["url"]
    code, c = call(base, "/cite", {"texts": ["Reply [S2] then [S1].", "Step [S1]. See https://rc.northeastern.edu/invented/ [S9]"],
                                   "refs": refs, "allowed_urls": allowed, "style": "numbered",
                                   "check_support": True}, token)
    expect(code == 200, "200")
    for f in ("texts", "cited", "references", "removed_urls", "unknown_refs", "support"):
        expect(c is not None and f in c, "%s present" % f)
    if c:
        expect(c["texts"][0] == "Reply [1] then [2].", "numbered: first use is [1]")
        expect(c["texts"][1].startswith("Step [2]."), "numbering shared across texts")
        expect(c["references"][:2] == [{"n": 1, "ref": "S2", "url": s2}, {"n": 2, "ref": "S1", "url": s1}],
               "references {n, ref, url}")
        expect(c["removed_urls"] == ["https://rc.northeastern.edu/invented/"], "invented URL removed")
        expect("[link removed]" in c["texts"][1], "removed URL marked in text")
        expect(c["unknown_refs"] == ["S9"], "unknown ref reported")
        sup = c["support"] or []
        expect(len(sup) >= 1 and all(set(v) >= {"ref", "claim", "verdict", "score", "best_ref", "best_score"}
                                     and v["verdict"] in ("ok", "better_source", "weak") for v in sup),
               "support verdict shape")
    code, c1 = call(base, "/cite", {"text": "See [S1].", "refs": refs, "allowed_urls": allowed}, token)
    expect(code == 200 and c1 is not None and "text" in c1 and "texts" not in c1, "single text in -> text out")
    expect(call(base, "/cite", {"text": "a", "texts": ["b"], "refs": refs, "allowed_urls": allowed}, token)[0] == 422,
           "text and texts together -> 422")

    print("\n%s: %d failure(s)" % ("PASS" if not FAILS else "FAIL", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
