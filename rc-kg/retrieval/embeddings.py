"""Embedding providers, chosen by EMBEDDING_PROVIDER.

The same embedder is used to index sections and to embed queries; mixing
models between the two silently ruins retrieval, so the model name is stored
on every Section and checked at query time.

`sentence_transformers` runs in-process on CPU or GPU. It is the default
because TicketMate's queries are ticket text, which must not leave the
university -- a hosted embedding API would receive it on every query.
"""

import logging
from functools import lru_cache
from typing import List

from . import http

from config import get_settings

logger = logging.getLogger(__name__)

# BGE models are trained with this prefix on the query side only. Without it
# short questions retrieve noticeably worse; with it on documents, worse again.
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder:
    """Interface every provider implements; `model` and `dim` describe the vectors."""

    model: str
    dim: int

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed passages (sections) for indexing, one vector per text."""
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        """Embed one search query (with the BGE query prefix where it applies)."""
        raise NotImplementedError

    def _query_text(self, text: str) -> str:
        return _BGE_QUERY_PREFIX + text if "bge" in self.model.lower() else text


class SentenceTransformersEmbedder(Embedder):
    """A local sentence-transformers model, run in-process (the default)."""

    def __init__(self, model: str, dim: int) -> None:
        """Load `model` (downloads it on first use); raises if its vector size
        is not `dim`, since the Neo4j vector index is built for that size."""
        from sentence_transformers import SentenceTransformer

        self.model = model
        self._st = SentenceTransformer(model)
        dim_of = getattr(self._st, "get_embedding_dimension", None) \
            or self._st.get_sentence_embedding_dimension
        self.dim = dim_of()
        if self.dim != dim:
            raise RuntimeError(
                "%s produces %d-dim vectors but EMBEDDING_DIM=%d; the vector "
                "index is built for EMBEDDING_DIM" % (model, self.dim, dim)
            )
        logger.info("loaded %s (%d-dim) on %s", model, self.dim, self._st.device)

    def _encode(self, texts: List[str]) -> List[List[float]]:
        """Encode to unit-length vectors, so a dot product is the cosine."""
        vecs = self._st.encode(
            texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vecs]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed passages for indexing."""
        return self._encode(texts)

    def embed_query(self, text: str) -> List[float]:
        """Embed one query, prefixed for BGE models."""
        return self._encode([self._query_text(text)])[0]


class OpenAICompatibleEmbedder(Embedder):
    """Any `/v1/embeddings` endpoint: vLLM, Ollama, TEI, or OpenAI itself."""

    def __init__(self, model: str, dim: int, base_url: str, api_key: str) -> None:
        """Remember the endpoint; nothing is sent until the first embed call."""
        if not base_url:
            raise RuntimeError("EMBEDDING_BASE_URL is required for this provider")
        self.model = model
        self.dim = dim
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {"Authorization": "Bearer %s" % api_key} if api_key else {}

    def _post(self, texts: List[str]) -> List[List[float]]:
        """POST the texts in batches of 64 (network call) and return the
        vectors in input order; raises if their size is not `dim`."""
        out: List[List[float]] = []
        for i in range(0, len(texts), 64):
            r = http.post(
                self._url,
                json={"model": self.model, "input": texts[i : i + 64]},
                headers=self._headers,
                timeout=120,
            )
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
        if out and len(out[0]) != self.dim:
            raise RuntimeError(
                "%s returned %d-dim vectors but EMBEDDING_DIM=%d"
                % (self.model, len(out[0]), self.dim)
            )
        return out

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed passages for indexing via the endpoint."""
        return self._post(texts)

    def embed_query(self, text: str) -> List[float]:
        """Embed one query via the endpoint, prefixed for BGE models."""
        return self._post([self._query_text(text)])[0]


@lru_cache
def get_embedder() -> Embedder:
    """The process-wide embedder for EMBEDDING_PROVIDER, built once and cached."""
    s = get_settings()
    provider = s.embedding_provider.lower()
    if provider == "sentence_transformers":
        return SentenceTransformersEmbedder(s.embedding_model, s.embedding_dim)
    if provider in ("openai", "vllm", "openai_compatible"):
        return OpenAICompatibleEmbedder(
            s.embedding_model, s.embedding_dim,
            s.embedding_base_url or "", s.embedding_api_key or "",
        )
    raise ValueError("unknown EMBEDDING_PROVIDER %r" % s.embedding_provider)
