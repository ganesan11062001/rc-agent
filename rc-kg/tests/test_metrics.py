"""Metric formulas. The RAGAS expectations were computed with ragas 0.4.3
itself (IDBasedContextPrecision, IDBasedContextRecall and
LLMContextPrecisionWithReference._calculate_average_precision) on these same
inputs, so a change that drifts from RAGAS fails here."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import metrics as m  # noqa: E402

A, B, C, D = "a", "b", "c", "d"


def test_context_precision_matches_ragas_average_precision():
    # ragas 0.4.3 _calculate_average_precision on these verdict lists:
    assert math.isclose(m.context_precision([1, 0, 1]), 0.8333333333, rel_tol=1e-9)
    assert math.isclose(m.context_precision([0, 1, 0, 1]), 0.5, rel_tol=1e-9)
    assert math.isclose(m.context_precision([1, 1, 1]), 1.0, rel_tol=1e-9)
    assert m.context_precision([0, 0, 0]) == 0.0


def test_id_based_precision_and_recall_match_ragas_set_semantics():
    retrieved = [A, A, B, C]          # duplicates collapse, as in ragas
    reference = [A, D]
    assert m.id_context_precision(retrieved, reference) == 1 / 3
    assert m.id_context_recall(retrieved, reference) == 1 / 2
    assert math.isnan(m.id_context_precision([], reference))


def test_any_of_metrics():
    pages, exp = [B, A, A, C], {A, D}
    v = m.verdicts(pages, exp)
    assert v == [0, 1, 1, 0]
    assert m.hit_at(v, 1) == 0.0 and m.hit_at(v, 2) == 1.0
    assert m.reciprocal_rank(v) == 0.5
    # A counts once at rank 2; ideal has min(|exp|, k) = 2 relevant pages.
    assert math.isclose(m.ndcg_at(pages, exp, 4), (1 / math.log2(3)) / (1 + 1 / math.log2(3)))


def test_bootstrap_interval_brackets_the_mean():
    point, lo, hi = m.bootstrap_ci([0, 1, 1, 0, 1, 1, 1, 0, 1, 1])
    assert lo <= point <= hi and point == 0.7
