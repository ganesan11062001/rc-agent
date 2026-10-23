"""Retrieval metrics, as pure functions of ranked results and labels.

A query's result is the ranked list of retrieved *contexts* (sections); a
context is relevant when its page is one of the question's expected pages.
Labels in eval/questions.yaml are "any of these pages answers it", so two
families of metric mean different things here:

  any-of (what the labels mean)     hit@k, mrr, ndcg@k
  RAGAS                              context_precision          rank-aware AP
                                     id_context_precision/recall set-based

The RAGAS definitions are copied from ragas 0.4.3 so the numbers are the ones
RAGAS would report given the same relevance verdicts (checked against the
library: see tests/test_metrics.py and the README):

  context_precision  = sum_i precision@i * v_i / (sum_i v_i + 1e-10)
                       (_calculate_average_precision; RAGAS gets v_i from an
                       LLM judge, here it comes from the labels)
  id_context_precision = |retrieved ids & reference ids| / |retrieved ids|
  id_context_recall    = |retrieved ids & reference ids| / |reference ids|
                       (IDBasedContextPrecision / IDBasedContextRecall, ids =
                       page URLs, compared as sets)

id_context_recall asks for *every* listed page, so on questions listing
alternatives it is stricter than the labels intend -- report it, but read
hit@k for "did we find an answer".
"""

import math
import random
from typing import Callable, Dict, List, Sequence, Tuple


def verdicts(retrieved_pages: Sequence[str], expected: set) -> List[int]:
    return [1 if p in expected else 0 for p in retrieved_pages]


def hit_at(v: Sequence[int], k: int) -> float:
    return 1.0 if any(v[:k]) else 0.0


def reciprocal_rank(v: Sequence[int]) -> float:
    for i, x in enumerate(v, 1):
        if x:
            return 1.0 / i
    return 0.0


def ndcg_at(retrieved_pages: Sequence[str], expected: set, k: int) -> float:
    """Binary-gain nDCG over pages. A page counts once (its first rank), so a
    page cited by three sections is not rewarded three times."""
    seen, dcg = set(), 0.0
    for i, p in enumerate(retrieved_pages[:k], 1):
        if p in expected and p not in seen:
            dcg += 1.0 / math.log2(i + 1)
            seen.add(p)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(expected), k) + 1))
    return dcg / ideal if ideal else 0.0


def context_precision(v: Sequence[int]) -> float:
    """RAGAS context precision (average precision), ragas 0.4.3 formula."""
    num = sum((sum(v[: i + 1]) / (i + 1)) * v[i] for i in range(len(v)))
    return num / (sum(v) + 1e-10)


def id_context_precision(retrieved_ids: Sequence[str], reference_ids: Sequence[str]) -> float:
    r, ref = {str(x) for x in retrieved_ids}, {str(x) for x in reference_ids}
    return len(r & ref) / len(r) if r else float("nan")


def id_context_recall(retrieved_ids: Sequence[str], reference_ids: Sequence[str]) -> float:
    r, ref = {str(x) for x in retrieved_ids}, {str(x) for x in reference_ids}
    return len(r & ref) / len(ref) if ref else float("nan")


def bootstrap_ci(values: Sequence[float], stat: Callable[[Sequence[float]], float] = None,
                 n: int = 2000, alpha: float = 0.05, seed: int = 0) -> Tuple[float, float, float]:
    """(point, lo, hi) percentile-bootstrap interval over questions. With ~45
    questions an interval is the honest way to report a mean: a 0.02 change
    in MRR is usually inside it."""
    vals = [x for x in values if not math.isnan(x)]
    if not vals:
        return float("nan"), float("nan"), float("nan")
    stat = stat or (lambda xs: sum(xs) / len(xs))
    rng = random.Random(seed)
    boots = sorted(stat([vals[rng.randrange(len(vals))] for _ in vals]) for _ in range(n))
    lo, hi = boots[int(n * alpha / 2)], boots[int(n * (1 - alpha / 2)) - 1]
    return stat(vals), lo, hi


def per_query(retrieved_pages: Sequence[str], expected: Sequence[str], k: int) -> Dict[str, float]:
    exp = set(expected)
    v = verdicts(retrieved_pages[:k], exp)
    return {
        "hit@1": hit_at(v, 1),
        "hit@%d" % k: hit_at(v, k),
        "mrr": reciprocal_rank(v),
        "ndcg@%d" % k: ndcg_at(retrieved_pages, exp, k),
        "context_precision": context_precision(v),
        "id_context_precision": id_context_precision(retrieved_pages[:k], expected),
        "id_context_recall": id_context_recall(retrieved_pages[:k], expected),
    }
