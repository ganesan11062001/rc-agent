"""Measure retrieval/support.py: false flags on correct citations, catches on
wrong ones. Two sets:

  hand-labelled  eval/citation_cases.yaml -- realistic draft sentences
  held-out       a sentence from each retrieved section, lightly reworded by
                 dropping every third word, cited correctly and then against
                 a section on another page. Larger but easier than real drafts.

Run:  scripts/with_kg.sh -m eval.citation_check
"""

import json
import logging
import random
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

from retrieval.graphrag import retrieve
from retrieval.support import check

CASES = Path(__file__).resolve().parent / "citation_cases.yaml"
SITE = "https://rc.northeastern.edu"


def _path(url: str) -> str:
    p = url.split("#", 1)[0].replace(SITE, "").rstrip("/")
    return p or "/"


def _verdict(claim: str, ref: str, refs) -> str:
    v = check(["%s [%s]." % (claim, ref)], refs)
    return v[0].verdict if v else "none"


def hand_labelled() -> dict:
    fp, pos, caught, neg, missing = 0, 0, 0, 0, []
    details = []
    for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]:
        res = retrieve(c["query"], k=6)
        refs = {s.ref: s.url for s in res.sources}
        for want in c["supported"]:
            hit = next((s for s in res.sources if _path(s.page_url) == want["page"]
                        and want["heading"].lower() in s.heading.lower()), None)
            if not hit:
                missing.append("%s | %s" % (want["page"], want["heading"]))
                continue
            pos += 1
            v = _verdict(c["claim"], hit.ref, refs)
            if v != "ok":
                fp += 1
                details.append("FALSE FLAG (%s): %r cited %s > %s"
                               % (v, c["claim"], hit.page_title, hit.heading))
        for page in c["unsupported"]:
            for s in (s for s in res.sources if _path(s.page_url) == page):
                neg += 1
                v = _verdict(c["claim"], s.ref, refs)
                if v != "ok":
                    caught += 1
                else:
                    details.append("MISSED: %r cited %s > %s" % (c["claim"], s.page_title, s.heading))
    return {"correct_citations": pos, "false_flags": fp,
            "wrong_citations": neg, "caught": caught,
            "supported_section_not_retrieved": missing, "details": details}


def held_out(seed: int = 7) -> dict:
    qs = [q["question"] for q in yaml.safe_load(
        (CASES.parent / "questions.yaml").read_text(encoding="utf-8"))["questions"]]
    rng = random.Random(seed)
    right, wrong = Counter(), Counter()
    for q in qs:
        res = retrieve(q, k=6)
        refs = {s.ref: s.url for s in res.sources}
        for s in res.sources:
            sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", s.text)
                     if 40 < len(x.strip()) < 250 and re.search(r"[A-Za-z]{3}", x)]
            if not sents:
                continue
            words = rng.choice(sents).rstrip(".").split()
            claim = " ".join(w for i, w in enumerate(words) if i % 3 != 1)
            right[_verdict(claim, s.ref, refs)] += 1
            others = [o.ref for o in res.sources if o.page_url != s.page_url]
            if others:
                wrong[_verdict(claim, rng.choice(others), refs)] += 1
    n, m = sum(right.values()), sum(wrong.values())
    return {"correct_citations": n, "false_flags": n - right["ok"],
            "wrong_citations": m, "caught": m - wrong["ok"]}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    out = {"hand_labelled": hand_labelled(), "held_out": held_out()}
    for name, r in out.items():
        print("%-14s false flags %d/%d (%.1f%%)   caught %d/%d (%.1f%%)" % (
            name, r["false_flags"], r["correct_citations"],
            100.0 * r["false_flags"] / max(1, r["correct_citations"]),
            r["caught"], r["wrong_citations"],
            100.0 * r["caught"] / max(1, r["wrong_citations"])))
    for line in out["hand_labelled"]["details"]:
        print("   ", line)
    for miss in out["hand_labelled"]["supported_section_not_retrieved"]:
        print("    (supported section not retrieved, case skipped: %s)" % miss)
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
