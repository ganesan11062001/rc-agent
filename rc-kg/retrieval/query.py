"""Try retrieval from the shell.

  python -m retrieval.query "my job was killed with OUT_OF_MEMORY"
  python -m retrieval.query --context -k 4 < ticket.txt
"""

import argparse
import logging
import sys

from .graphrag import retrieve


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("query", nargs="?", help="omit to read stdin")
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--context", action="store_true", help="print the LLM context block")
    ap.add_argument("--events", action="store_true", help="include calendar events")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    res = retrieve(args.query or sys.stdin.read(), k=args.k,
                   include_events=True if args.events else None)
    print("topics: %s%s" % (", ".join(res.query_topics) or "-",
                             "   [LOW CONFIDENCE]" if res.low_confidence else ""))
    for src in res.sources:
        print("%-4s %.4f vec=%s %-28s %s > %s\n     %s" % (
            src.ref, src.score, src.vector_score,
            ",".join("%s%d" % (k[0], v) for k, v in src.signals.items()),
            src.page_title, src.heading, src.url))
    if args.context:
        print("\n" + res.context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
