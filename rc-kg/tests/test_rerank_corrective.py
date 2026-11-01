"""Reranking and corrective retrieval, without a graph or a model download:
the fusion arithmetic, what counts as weak, the query rewrite, and that a
missing reranker leaves retrieval as it was."""

from retrieval import corrective, rerank
from retrieval.graphrag import RetrievalResult


def _res(low=False, top=None):
    return RetrievalResult(query="q", query_topics=[], sources=[], context="", allowed_urls=[],
                           low_confidence=low, trace={"rerank_top": top, "timings_ms": {}})


def test_fuse_moves_a_strong_cross_encoder_pick_up():
    fused = ["a", "b", "c", "d"]
    probs = {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.99}
    out = [s for s, _ in rerank.fuse(fused, probs, 60, 2.0)]
    assert out[0] == "d" and set(out) == set(fused)
    # weight 0: the hybrid order is unchanged
    assert [s for s, _ in rerank.fuse(fused, probs, 60, 0.0)] == fused


def test_passage_names_page_and_heading_and_clips():
    p = rerank.passage("Slurm", "Memory", "x" * 5000)
    assert p.startswith("Slurm > Memory\n") and len(p) == len("Slurm > Memory\n") + rerank.PASSAGE_CHARS
    assert rerank.passage("Slurm", "Overview", "t").startswith("Slurm\n")


def test_unavailable_model_scores_nothing():
    assert rerank.score("no-such/model-xyz", "q", ["p"]) is None
    assert not rerank.available("") and not rerank.available("no-such/model-xyz")


def test_weak():
    assert corrective.is_weak(_res(low=True), 0.3) == "low_confidence"
    assert corrective.is_weak(_res(top=0.1), 0.3).startswith("rerank_top 0.100")
    assert corrective.is_weak(_res(top=0.9), 0.3) is None
    assert corrective.is_weak(_res(top=None), 0.3) is None      # no reranker: only low_confidence counts


def test_rewrite_uses_the_docs_words():
    q = "My jobs have been pending for three days and never start"
    r = corrective.rewrite(q)
    assert r.startswith(q) and "queue priority fairshare" in r
    oom = corrective.rewrite("job killed, sacct says OUT_OF_MEMORY")
    assert "--mem" in oom
    access = corrective.rewrite("please add my student to our lab group")
    assert "PI approval" in access
    assert corrective.rewrite("hello there") == ""               # nothing known: no second search


def test_rewrite_keeps_only_the_start_of_a_long_query():
    q = "pending " + "word " * 400
    head = corrective.rewrite(q).split("\n")[0]
    assert len(head) <= 300


def _fake(top, low=False, page="https://rc.example/p"):
    from retrieval.graphrag import Source
    src = Source(ref="S1", section_id="x", page_title="P", heading="H", url=page, page_url=page,
                 content_type="page", site="rc", published="", last_modified="", anchor_kind="", text="t", score=1.0)
    r = _res(low=low, top=top)
    r.sources = [src]
    r.trace["timings_ms"] = {"total": 5.0}
    return r


def _run(monkeypatch, first, second, gain=0.2, weak=0.3):
    from config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "corrective_rag", True)
    monkeypatch.setattr(s, "rerank_mode", "score")
    monkeypatch.setattr(s, "rerank_weak_below", weak)
    monkeypatch.setattr(s, "corrective_min_gain", gain)
    calls = iter([first, second])
    monkeypatch.setattr(corrective, "retrieve", lambda *a, **k: next(calls))
    monkeypatch.setattr(corrective.rerank, "available", lambda name: True)
    return corrective.retrieve_corrective("my jobs are pending for days")


def test_corrective_keeps_a_clearly_better_rewrite(monkeypatch):
    out = _run(monkeypatch, _fake(0.05), _fake(0.9, page="https://rc.example/queue"))
    c = out.trace["corrective"]
    assert c["ran"] and c["kept"] == "rewrite" and out.query == "my jobs are pending for days"
    assert c["sources_changed"] == ["https://rc.example/queue"] and not out.low_confidence


def test_corrective_ignores_a_small_gain(monkeypatch):
    out = _run(monkeypatch, _fake(0.22), _fake(0.38))
    assert out.trace["corrective"]["kept"] == "first" and out.trace["rerank_top"] == 0.22


def test_corrective_does_not_run_on_a_strong_result(monkeypatch):
    out = _run(monkeypatch, _fake(0.95), _fake(0.99))
    assert out.trace["corrective"]["ran"] is False and out.trace["corrective"]["weak"] is None
