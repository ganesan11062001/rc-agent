"""Thin wrapper around the vLLM server's OpenAI-compatible API."""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import openai
from openai import AsyncOpenAI
from pydantic import ValidationError

from . import tracing
from .config import Settings, get_settings
from .models import DRAFT_JSON_SCHEMA, LLMDraft
from .prompts import build_messages

logger = logging.getLogger(__name__)

# Structured-output strategies, best first. vLLM (recent versions) and the
# OpenAI API both understand "json_schema"; older vLLM builds only understand
# "json_object"; anything else falls back to prompt-only JSON. We remember the
# first strategy that works so we pay the discovery cost at most once.
_STRATEGIES = ("json_schema", "json_object", "none")
_working_strategy: Optional[str] = None


class LLMError(Exception):
    """An error with a message that is safe (and useful) to show in the UI."""

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


def ensure_proxy_bypass(settings: Optional[Settings] = None) -> None:
    """Add the model server's host to NO_PROXY unless asked not to.

    The openai SDK's HTTP layer picks up HTTP_PROXY/HTTPS_PROXY from the
    environment, which is wrong here: the vLLM server is internal (often
    localhost, via an SSH tunnel) and a site proxy will simply refuse those
    requests. Rather than pinning a particular httpx version so we can pass
    trust_env=False, we extend NO_PROXY — every httpx generation honours it.
    """
    settings = settings or get_settings()
    if settings.use_proxy_env:
        return

    host = urlparse(settings.vllm_base_url).hostname
    if not host:
        return

    current = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    entries = [e.strip() for e in current.split(",") if e.strip()]
    if host in entries:
        return

    entries.append(host)
    joined = ",".join(entries)
    os.environ["no_proxy"] = joined
    os.environ["NO_PROXY"] = joined
    logger.info("added %s to NO_PROXY (USE_PROXY_ENV=false)", host)


def get_client(settings: Optional[Settings] = None) -> AsyncOpenAI:
    """Build a client for one request. The caller must `await client.close()`."""
    settings = settings or get_settings()
    ensure_proxy_bypass(settings)
    return AsyncOpenAI(
        base_url=settings.vllm_base_url,
        api_key=settings.effective_api_key,
        timeout=settings.request_timeout_seconds,
        max_retries=1,
    )


def _response_format(strategy: str) -> Optional[Dict[str, Any]]:
    """The response_format for a structured-output strategy: a strict JSON schema,
    JSON mode, or None for prompt-only ("none").
    """
    if strategy == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "rc_draft_response",
                "strict": True,
                "schema": DRAFT_JSON_SCHEMA,
            },
        }
    if strategy == "json_object":
        return {"type": "json_object"}
    return None


def extract_json(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of a model response.

    Handles the three things models actually do when they ignore us: fenced
    code blocks, a preamble sentence, and trailing commentary.
    """
    text = text.strip()
    if not text:
        raise ValueError("model returned an empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Scan for the first balanced {...}, ignoring braces inside strings.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])

    raise ValueError("no JSON object found in the model response")


def _is_unsupported_param_error(exc: openai.APIStatusError) -> bool:
    """Did the server reject us because it doesn't know response_format?"""
    if exc.status_code not in (400, 404, 422, 500):
        return False
    blob = str(getattr(exc, "body", "") or "") + " " + str(exc)
    blob = blob.lower()
    return any(
        needle in blob
        for needle in (
            "response_format",
            "json_schema",
            "guided",
            "unsupported",
            "unrecognized",
            "unexpected keyword",
            "extra inputs are not permitted",
        )
    )


async def generate_draft(ticket_text: str,
                         extra_instructions: str = "",
                         trace: Optional[Dict[str, Any]] = None,
                         kg_context: str = "",
                         kg_low_confidence: bool = False,
                         lookups_context: str = "",
                         playbook_context: str = "",
                         playbook_missing: Tuple[str, ...] = (),
                         ) -> Tuple[LLMDraft, str]:
    """Send one ticket to the model and return (validated draft, model name).

    `kg_context` is the numbered RC website sources from rc_kg, if any.

    Raises LLMError with a UI-friendly message on any failure.
    """
    global _working_strategy

    settings = get_settings()
    client = get_client(settings)

    # Strategies still worth trying, in order.
    start_at = _STRATEGIES.index(_working_strategy) if _working_strategy else 0
    strategies = _STRATEGIES[start_at:]

    raw = ""
    last_parse_error: Optional[str] = None
    # Every call to the model is recorded -- including rejected strategies
    # and unparseable output -- with its exact token usage, so a draft's cost
    # and history can be reconstructed from its run log.
    attempts: List[Dict[str, Any]] = trace.setdefault("attempts", []) if trace is not None else []
    if trace is not None:
        trace["params"] = {"model": settings.vllm_model_name, "temperature": settings.temperature,
                           "max_tokens": settings.max_tokens,
                           "timeout_seconds": settings.request_timeout_seconds}

    try:
        for strategy in strategies:
            messages = build_messages(
                ticket_text,
                json_reminder=(strategy == "none"),
                extra_instructions=extra_instructions,
                kg_context=kg_context,
                kg_low_confidence=kg_low_confidence,
                lookups_context=lookups_context,
                playbook_context=playbook_context,
                playbook_missing=playbook_missing,
            )
            kwargs: Dict[str, Any] = {
                "model": settings.vllm_model_name,
                "messages": messages,
                "temperature": settings.temperature,
                "max_tokens": settings.max_tokens,
            }
            response_format = _response_format(strategy)
            if response_format is not None:
                kwargs["response_format"] = response_format

            att: Dict[str, Any] = {
                "n": len(attempts) + 1, "strategy": strategy,
                "response_format": (response_format or {}).get("type"),
                "json_reminder": strategy == "none",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "outcome": "error", "usage": None,
            }
            attempts.append(att)
            invocation = {k: v for k, v in kwargs.items() if k != "messages"}
            with tracing.run("llm", "llm", inputs={"messages": messages},
                             metadata={"invocation_params": invocation, "strategy": strategy,
                                       "attempt": att["n"], "ls_provider": "vllm",
                                       "ls_model_name": settings.vllm_model_name},
                             tags=["strategy:" + strategy]) as lrun:
                if lrun is not None:
                    att["run_id"] = lrun.id
                t0 = time.perf_counter()
                try:
                    completion = await client.chat.completions.create(**kwargs)
                except openai.APIStatusError as exc:
                    att.update(latency_ms=int((time.perf_counter() - t0) * 1000),
                               http_status=exc.status_code, error=_truncate(str(exc)))
                    if strategy != "none" and _is_unsupported_param_error(exc):
                        att["outcome"] = "unsupported"
                        if lrun is not None:
                            lrun.end({"http_status": exc.status_code},
                                     error="unsupported response_format: %s" % _truncate(str(exc), 200))
                        logger.warning(
                            "server rejected response_format=%s (HTTP %s); "
                            "falling back to the next structured-output strategy",
                            strategy,
                            exc.status_code,
                        )
                        continue
                    raise
                except Exception as exc:
                    att.update(latency_ms=int((time.perf_counter() - t0) * 1000), error=_truncate(str(exc)))
                    raise
                att["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                att["served_model"] = completion.model
                att["usage"] = _usage(completion)

                if not completion.choices:
                    raise LLMError("The model returned no completions.")

                choice = completion.choices[0]
                raw = choice.message.content or ""
                att.update(finish_reason=choice.finish_reason, raw_response=raw)
                if lrun is not None:
                    lrun.usage = att["usage"]
                    lrun.end({"completion": raw, "finish_reason": choice.finish_reason,
                              "served_model": completion.model},
                             error="output truncated (max_tokens)" if choice.finish_reason == "length" else None)

                if choice.finish_reason == "length":
                    att["outcome"] = "truncated"
                    raise LLMError(
                        "The model's answer was cut off before it finished.",
                        "Increase MAX_TOKENS (currently %d) and try again."
                        % settings.max_tokens,
                    )

            with tracing.run("parse_draft", "parser", inputs={"completion_chars": len(raw)},
                             metadata={"strategy": strategy}) as prun:
                try:
                    draft = LLMDraft.model_validate(extract_json(raw))
                except (ValueError, ValidationError) as exc:
                    last_parse_error = str(exc)
                    att.update(outcome="parse_error", error=_truncate(last_parse_error))
                    if prun is not None:
                        prun.end(error=_truncate(last_parse_error, 300))
                    logger.warning(
                        "could not parse model output with strategy=%s: %s",
                        strategy,
                        last_parse_error,
                    )
                    # Constrained decoding failing to parse means the constraint
                    # isn't really being applied — drop to the next strategy.
                    if strategy != "none":
                        continue
                    if trace is not None:
                        trace["messages"] = messages
                        trace["raw_response"] = raw
                        trace["strategy"] = strategy
                    raise LLMError(
                        "The model did not return valid JSON in the expected format.",
                        _truncate(last_parse_error),
                    )
                if prun is not None:
                    prun.end({"draft": draft.model_dump()})

            att["outcome"] = "ok"
            _working_strategy = strategy
            if trace is not None:
                # Enough to reproduce and argue with this draft later.
                trace["messages"] = messages
                trace["raw_response"] = raw
                trace["strategy"] = strategy
            return draft, completion.model or settings.vllm_model_name

        # Every strategy was rejected as unsupported.
        raise LLMError(
            "The model did not return valid JSON in the expected format.",
            _truncate(last_parse_error) if last_parse_error else None,
        )

    except openai.APITimeoutError as exc:
        raise LLMError(
            "The model server timed out (after %.0fs)."
            % settings.request_timeout_seconds,
            "The request may be queued behind other work on the cluster, or the "
            "ticket may be very long. Try again or raise REQUEST_TIMEOUT_SECONDS.",
        ) from exc
    except openai.APIConnectionError as exc:
        raise LLMError(
            "Could not reach the model server at %s." % settings.vllm_base_url,
            "Check that vLLM is running on the cluster and that your SSH tunnel "
            "or network route to it is up.",
        ) from exc
    except openai.AuthenticationError as exc:
        raise LLMError(
            "The model server rejected our credentials.",
            "Set VLLM_API_KEY to the key the server was started with.",
        ) from exc
    except openai.NotFoundError as exc:
        raise LLMError(
            "The model '%s' is not served at %s."
            % (settings.vllm_model_name, settings.vllm_base_url),
            "Check GET /v1/models on the server and set VLLM_MODEL_NAME to match.",
        ) from exc
    except openai.APIStatusError as exc:
        raise LLMError(
            "The model server returned an error (HTTP %d)." % exc.status_code,
            _truncate(str(getattr(exc, "body", "") or exc)),
        ) from exc
    finally:
        await client.close()


async def check_health() -> Tuple[bool, List[str], Optional[str]]:
    """Ping the server's /models endpoint.

    Returns (reachable, model_ids, detail_message).
    """
    settings = get_settings()
    client = get_client(settings)
    try:
        listing = await client.models.list()
        return True, [m.id for m in listing.data], None
    except openai.APIConnectionError:
        return False, [], "Could not connect to %s" % settings.vllm_base_url
    except openai.APITimeoutError:
        return False, [], "Timed out connecting to %s" % settings.vllm_base_url
    except openai.APIStatusError as exc:
        return False, [], "Server returned HTTP %d" % exc.status_code
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return False, [], _truncate(str(exc))
    finally:
        await client.close()


def _count_sync(root: str, model: str, text: str, timeout: float) -> int:
    """Blocking POST to vLLM's /tokenize under `root`; the token count of `text`."""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        root + "/tokenize", headers={"Content-Type": "application/json"},
        data=json.dumps({"model": model, "prompt": text, "add_special_tokens": False}).encode())
    with opener.open(req, timeout=timeout) as r:
        return int(json.loads(r.read())["count"])


_context_window: Dict[str, int] = {}


async def context_window() -> Optional[int]:
    """The served model's context length (vLLM reports max_model_len in
    /v1/models), cached per model; None if the server does not say."""
    settings = get_settings()
    name = settings.vllm_model_name
    if name in _context_window:
        return _context_window[name]
    client = get_client(settings)
    try:
        listing = await client.models.list()
    except Exception:          # the draft call reports real errors
        return None
    finally:
        await client.close()
    for m in listing.data:
        n = getattr(m, "max_model_len", None) or (getattr(m, "model_extra", None) or {}).get("max_model_len")
        if m.id == name and n:
            _context_window[name] = int(n)
            return int(n)
    return None


async def token_breakdown(parts: Dict[str, str]) -> Optional[Dict[str, int]]:
    """Tokens per prompt part, counted by the serving model's own tokenizer
    (vLLM's /tokenize, beside /v1). Best-effort: None if the server has no
    /tokenize. The exact totals come from the completion's usage either way;
    this only says where they went (ticket vs. RC sources vs. instructions)."""
    import asyncio
    settings = get_settings()
    root = settings.vllm_base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    async def count(text: str) -> int:
        """Tokens in one prompt part (0 for an empty one), counted on a worker thread."""
        if not text:
            return 0                     # nothing to send; an empty part costs nothing
        return await asyncio.to_thread(_count_sync, root, settings.vllm_model_name, text, 3.0)
    try:
        counts = await asyncio.gather(*(count(text) for text in parts.values()))
    except Exception as exc:                       # noqa: BLE001
        logger.info("token breakdown unavailable: %s", _truncate(str(exc), 120))
        return None
    return dict(zip(parts.keys(), counts))


def _usage(completion: Any) -> Optional[Dict[str, int]]:
    """Token counts as the server reported them (vLLM returns them on every
    completion); None if it did not."""
    u = getattr(completion, "usage", None)
    if u is None:
        return None
    return {"prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(u, "total_tokens", 0) or 0)}


def _truncate(text: Optional[str], limit: int = 400) -> Optional[str]:
    """`text` with whitespace collapsed, cut to `limit` characters with an ellipsis."""
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
