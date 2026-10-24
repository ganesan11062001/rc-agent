"""Application settings, loaded from environment variables / .env file."""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for RC Copilot.

    Every field can be overridden with an environment variable of the same
    name in upper case (e.g. VLLM_BASE_URL), or via a .env file in the
    directory you launch uvicorn from.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- vLLM / OpenAI-compatible endpoint -------------------------------
    # Point this at the vLLM server running on the HPC cluster, including
    # the trailing /v1 (e.g. http://d1234:8000/v1).
    vllm_base_url: str = "http://localhost:8000/v1"

    # Model id as the vLLM server advertises it (see GET /v1/models).
    vllm_model_name: str = "meta-llama/Llama-3.1-8B-Instruct"

    # vLLM usually needs no auth. Set this if the server was started with
    # --api-key, or if you are pointing at a hosted OpenAI-compatible API.
    vllm_api_key: Optional[str] = None

    # --- Auth --------------------------------------------------------------
    # Token a user pastes once to sign in. Leave this unset: the app then mints
    # a random one at startup and prints it to the console. That matters on the
    # cluster, where /projects is group-readable -- a token written to .env is
    # readable by everyone in the group, a token held in memory is not.
    auth_token: Optional[str] = None

    # How long a sign-in lasts before the user must paste the token again.
    # Sized to match how long an RC SSH session/tunnel typically survives.
    session_hours: float = 3.0

    # Only for a loopback-only deployment on your own machine. Logs a warning
    # on every startup, because on a shared node this leaves the app wide open.
    auth_disabled: bool = False

    # --- RC knowledge graph (rc_kg) ---------------------------------------
    # Base URL of the rc_kg API (e.g. http://127.0.0.1:8090). Unset = drafts
    # are not grounded in the RC website, exactly as before rc_kg existed.
    # script.sh.erb starts rc_kg in the same job and sets this.
    rc_kg_url: Optional[str] = None
    # Bearer token for rc_kg; the job generates it and gives it to both sides.
    rc_kg_api_token: Optional[str] = None
    # Website sections put in the prompt per ticket.
    rc_kg_k: int = 6
    # Retrieval is well under a second; the timeout only matters when rc_kg
    # is wedged.
    rc_kg_timeout_seconds: float = 15.0

    # --- App -------------------------------------------------------------
    app_port: int = 8080

    # Generation knobs. Low temperature keeps the JSON stable.
    temperature: float = 0.2
    max_tokens: int = 1400
    request_timeout_seconds: float = 120.0

    # By default we ignore HTTP_PROXY/HTTPS_PROXY when talking to the model
    # server: it lives inside the network (or on localhost via an SSH tunnel),
    # and a site proxy will happily 403 those requests. Set this to true if you
    # genuinely need a proxy to reach the endpoint — e.g. when pointing at a
    # hosted API from behind one during local development.
    use_proxy_env: bool = False

    # Count each prompt part's tokens with the serving model's tokenizer
    # (vLLM /tokenize) for the run log's token breakdown. One extra local
    # request per part; best-effort.
    trace_token_breakdown: bool = True

    # Online evaluation: after each draft is returned, the serving model
    # grades it (hallucination, correctness, conciseness, answer relevance)
    # in the background; results land as feedback beside the run. Costs one
    # extra model call per draft; sample to reduce it.
    online_eval: bool = True
    online_eval_sample_rate: float = 1.0

    # Ticket text may contain sensitive researcher data, so it is NOT logged
    # unless this is explicitly turned on for local debugging.
    debug: bool = False

    # Set to "" to log to stdout only.
    log_file: str = "logs/rc_copilot.log"

    @property
    def effective_api_key(self) -> str:
        """openai's client requires a non-empty key; vLLM ignores the value."""
        return self.vllm_api_key or "EMPTY"


@lru_cache
def get_settings() -> Settings:
    return Settings()
