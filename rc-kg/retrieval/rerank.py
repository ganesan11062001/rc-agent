"""Cross-encoder scoring of retrieval candidates (RERANK_MODE).

  off      no cross-encoder
  score    score the candidates, keep the hybrid order: the best source's
           score is the confidence signal corrective retrieval uses
  replace  reorder by the cross-encoder (stale posts still demoted)
  fuse     RRF of the hybrid order and the cross-encoder's

Measured 2026-11-01 (eval/rerank_sweep.py, 45 questions, 12 ticket cases,
full graph, k=6): no reordering beat the hybrid order. Baseline hit@1 0.689,
hit@6 0.978, MRR 0.795. MiniLM-L6 replace: identical totals (it moved 45 of
45 results; gains -- "sbatch for a GPU" 4th -> 1st, "add a student to
/projects" missing -> 1st -- cancelled losses -- "gpu partition limits"
1st -> 3rd); fuse w=1 0.778, w=2 0.762. bge-reranker-base: replace 0.744
and one ticket regression, fuse 0.779, ~10 s per query on 2 CPUs. Both are
trained on web search (MS MARCO), not RC's vocabulary. As a CONFIDENCE
signal the cross-encoder is excellent: MiniLM's best source scored median
0.994 (p10 0.48) on the questions and at most 0.087 on twelve off-topic
ones. Hence "score".

Hybrid retrieval (vector + full-text + topics, fused by RRF) is good at
finding the right section somewhere in the top 20-30 and worse at putting it
first: bi-encoder similarity and keyword overlap are each a proxy. A
cross-encoder reads the query and one passage together and scores how well
the passage answers it -- slower, so it only reorders the candidates hybrid
retrieval already found.

Runs in-process on CPU, like the embedder: ticket text must not leave the
university, and the GPU belongs to the drafting model. Loaded once, on first
use; if it cannot be loaded (no network, no cache), reranking is skipped and
retrieval keeps its fused order -- the caller sees it in the trace.

Scores are probabilities (a sigmoid of the model's logit), so a stale-post
penalty can multiply them and a threshold can be set on them.
"""

import logging
import math
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# What the model reads per candidate: the page and heading say what the
# section is about; the text is clipped, since cross-encoders stop at 512
# tokens and the opening of a section carries its point.
PASSAGE_CHARS = 1200


@lru_cache(maxsize=2)
def _model(name: str):
    """The cross-encoder `name` on CPU, or None if it cannot be loaded."""
    try:
        from sentence_transformers import CrossEncoder
        return CrossEncoder(name, device="cpu", max_length=512)
    except Exception as exc:                     # noqa: BLE001 -- reranking is optional
        logger.error("reranker %s unavailable, keeping the fused order: %s", name, exc)
        return None


def available(name: str) -> bool:
    """Whether the cross-encoder `name` can be used."""
    return bool(name) and _model(name) is not None


def passage(page_title: str, heading: str, text: str) -> str:
    """The text the cross-encoder scores for one section."""
    title = page_title if heading in ("", "Overview", page_title) else "%s > %s" % (page_title, heading)
    return "%s\n%s" % (title, (text or "")[:PASSAGE_CHARS])


def score(name: str, query: str, passages: Sequence[str]) -> Optional[List[float]]:
    """Probability that each passage answers `query`, in order; None when the
    model is unavailable or fails."""
    model = _model(name)
    if model is None or not passages:
        return None
    try:
        out = model.predict([(query, p) for p in passages], batch_size=16, show_progress_bar=False)
    except Exception as exc:                     # noqa: BLE001
        logger.error("reranking failed, keeping the fused order: %s", exc)
        return None
    # Some models already end in a sigmoid (bge-reranker: activation Sigmoid),
    # others return logits (ms-marco MiniLM: Identity). A second sigmoid kept
    # the order but squashed bge's scores into 0.50-0.73, useless for a threshold.
    if type(getattr(model, "activation_fn", None)).__name__ == "Sigmoid":
        return [float(x) for x in out]
    return [1.0 / (1.0 + math.exp(-float(x))) for x in out]


def fuse(fused_order: List[str], probs: dict, rrf_k: int, weight: float) -> List[Tuple[str, float]]:
    """RRF of the hybrid order and the cross-encoder order: (sid, score),
    best first. `weight` is the cross-encoder's share relative to the hybrid's 1."""
    ce_order = sorted(fused_order, key=lambda s: probs[s], reverse=True)
    ce_rank = {sid: i for i, sid in enumerate(ce_order, 1)}
    out = [(sid, 1.0 / (rrf_k + i) + weight / (rrf_k + ce_rank[sid])) for i, sid in enumerate(fused_order, 1)]
    return sorted(out, key=lambda x: x[1], reverse=True)
