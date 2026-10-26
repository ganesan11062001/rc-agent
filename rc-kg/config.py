"""Settings, from environment variables or .env (see .env.example).

Nothing here hardcodes an embedding or LLM vendor: `embedding_provider`
selects the embedder (retrieval/embeddings.py), and the LLM is any
OpenAI-compatible endpoint (vLLM, llama.cpp, Ollama, the OpenAI API) at
`llm_base_url`, so swapping one for another is a .env change.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Source site ------------------------------------------------------
    # The RC website's WordPress API and sitemap. Which hosts count as internal
    # (ingested) versus ExternalLink (never fetched) is ingest/urls.py's call.
    wp_api: str = "https://rc.northeastern.edu/wp-json/wp/v2"
    sitemap_index: str = "https://rc.northeastern.edu/wp-sitemap.xml"

    # Identifies us in the site's access logs, with a contact address. A shared
    # WordPress host is entitled to know who is crawling it.
    user_agent: str = (
        "RC-KG/0.1 (+Northeastern Research Computing; murugan.g@northeastern.edu)"
    )
    # Seconds between requests to the site. 1.0 = the plan's "be polite".
    request_delay: float = 1.0
    request_timeout: float = 30.0

    # Read the Docs site, ingested alongside (ingest/rtd.py). `latest` is
    # built from `master`; explorer-main lagged it by 17 commits on 2026-10-22.
    docs_enabled: bool = True
    docs_base: str = "https://rc-docs.northeastern.edu/en/latest/"
    docs_repo: str = "https://github.com/northeastern-rc/rc-public-documentation"
    docs_branch: str = "master"
    docs_source_dir: str = "docs/source"

    # --- Neo4j ------------------------------------------------------------
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    # --- Embeddings -------------------------------------------------------
    # "sentence_transformers" | "openai" | "vllm"
    embedding_provider: str = "sentence_transformers"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    embedding_base_url: Optional[str] = None
    embedding_api_key: Optional[str] = None

    # --- LLM --------------------------------------------------------------
    # Any OpenAI-compatible chat endpoint: vLLM, llama.cpp, Ollama, OpenAI.
    llm_model: str = "Qwen/Qwen2.5-7B-Instruct"
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_temperature: float = 0.1
    llm_max_tokens: int = 1200

    # --- Behaviour --------------------------------------------------------
    # Past events go stale, so whether they are retrievable is a policy choice,
    # not a fact about the data. Phase 1 ingests them either way.
    include_events_in_retrieval: bool = False

    # Resolve 301/302 on internal links before storing them. Measured on this
    # site: a third of internal hrefs are redirects, so leaving this off drops
    # ~30 of 90 LINKS_TO targets on the floor.
    resolve_redirects: bool = True

    data_dir: str = "data"

    # --- Serving (set by scripts/serve_kg.sh) -------------------------------
    # When set, every endpoint except /health needs "Authorization: Bearer".
    # Loopback is shared by every user on a compute node, so a job that starts
    # the API also generates a token and hands it to its client.
    rc_kg_api_token: Optional[str] = None
    # manifest.json of the snapshot this instance was loaded from, shown by
    # /health so a client can tell how old the graph is.
    rc_kg_manifest: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
