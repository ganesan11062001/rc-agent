"""Access-token sign-in, and short-lived signed sessions.

The model: one access token per running backend, pasted once by the person who
started it. In exchange they get a session token that the server itself will
refuse once it is older than SESSION_HOURS.

Sessions are HMAC-signed rather than stored, so there is no session table to
sweep and nothing to persist. The signing key lives only in memory, which means
restarting the backend invalidates every outstanding session -- correct
behaviour, not a limitation.
"""

import hashlib
import hmac
import logging
import secrets
import time
from typing import Optional, Tuple

from fastapi import Header, HTTPException

from .config import get_settings

logger = logging.getLogger(__name__)

# Generated once per process, on first use.
_access_token: Optional[str] = None
_signing_key: Optional[str] = None


def access_token() -> str:
    """The token a user must paste to sign in."""
    global _access_token
    if _access_token is None:
        configured = get_settings().auth_token
        _access_token = configured or secrets.token_urlsafe(24)
    return _access_token


def _key() -> str:
    global _signing_key
    if _signing_key is None:
        _signing_key = secrets.token_urlsafe(32)
    return _signing_key


def _sign(payload: str) -> str:
    return hmac.new(_key().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def check_access_token(candidate: str) -> bool:
    """Constant-time comparison, so this can't be probed a character at a time."""
    return hmac.compare_digest(candidate.strip(), access_token())


def mint_session() -> Tuple[str, int]:
    """Return (session_token, unix_expiry)."""
    expires_at = int(time.time() + get_settings().session_hours * 3600)
    return "%d.%s" % (expires_at, _sign(str(expires_at))), expires_at


def verify_session(token: str) -> bool:
    try:
        expiry_str, signature = token.split(".", 1)
        expires_at = int(expiry_str)
    except (ValueError, AttributeError):
        return False

    # Check the signature before trusting the expiry it carries.
    if not hmac.compare_digest(signature, _sign(expiry_str)):
        return False
    return expires_at > time.time()


async def require_session(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency guarding the endpoints that do real work."""
    if get_settings().auth_disabled:
        return

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to draft responses.")

    if not verify_session(authorization.split(" ", 1)[1].strip()):
        raise HTTPException(
            status_code=401, detail="Your session has expired. Sign in again."
        )


def startup_banner() -> str:
    """The block printed at startup so the operator can copy their token."""
    settings = get_settings()
    if settings.auth_disabled:
        return (
            "\n"
            "  ============================================================\n"
            "   AUTH IS DISABLED (AUTH_DISABLED=true)\n"
            "   Anyone who can reach this port can use it and read drafts.\n"
            "   Only acceptable when bound to 127.0.0.1 on your own machine.\n"
            "  ============================================================\n"
        )

    if settings.auth_token:
        # Supplied by whoever started us, so they already have it. Do NOT echo
        # it: under Slurm this stdout becomes a job log on the group-readable
        # /projects filesystem, which would hand the token to the whole group.
        return (
            "\n"
            "  ============================================================\n"
            "   RC Copilot: using the access token from AUTH_TOKEN.\n"
            "   (Not printed here on purpose — job logs are group-readable.)\n"
            "   Sessions last %.1f hours.\n"
            "  ============================================================\n"
            % settings.session_hours
        )

    return (
        "\n"
        "  ============================================================\n"
        "   RC Copilot access token (generated for this run):\n"
        "\n"
        "       %s\n"
        "\n"
        "   Paste it into the app to sign in. Sessions last %.1f hours.\n"
        "   Restarting the backend invalidates all sessions.\n"
        "  ============================================================\n"
        % (access_token(), settings.session_hours)
    )
