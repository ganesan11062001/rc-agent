"""One polite HTTP session, shared by every ingest module.

Rate limiting lives here rather than at each call site so it cannot be
forgotten: the delay is enforced between requests on the session itself.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple

import requests

from config import get_settings

logger = logging.getLogger(__name__)


class PoliteSession:
    """A requests.Session that never issues two requests closer than `delay`."""

    def __init__(self, delay: float, user_agent: str, timeout: float) -> None:
        self._delay = delay
        self._timeout = timeout
        self._last = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self.request_count = 0

    def _wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._delay:
                time.sleep(self._delay - gap)
            self._last = time.monotonic()
            self.request_count += 1

    def get(self, url: str, **kwargs) -> requests.Response:
        self._wait()
        kwargs.setdefault("timeout", self._timeout)
        logger.debug("GET %s", url)
        return self._session.get(url, **kwargs)

    def head_resolved(self, url: str) -> Tuple[int, str]:
        """Follow redirects and report (final_status, final_url).

        Uses GET, not HEAD: WordPress and many CDNs answer HEAD differently from
        GET, and a redirect chain that only appears under GET is exactly the case
        this is meant to catch.
        """
        self._wait()
        try:
            r = self._session.get(
                url, timeout=self._timeout, allow_redirects=True, stream=True
            )
            r.close()
            return r.status_code, r.url
        except requests.RequestException as exc:
            logger.warning("could not resolve %s: %s", url, exc)
            return 0, url

    def get_json(self, url: str, params: Optional[Dict] = None):
        """GET returning (json, headers). Raises on a non-2xx: a silent empty
        page here would look identical to "that content type is empty"."""
        r = self.get(url, params=params)
        r.raise_for_status()
        return r.json(), r.headers


_session: Optional[PoliteSession] = None


def session() -> PoliteSession:
    global _session
    if _session is None:
        s = get_settings()
        _session = PoliteSession(s.request_delay, s.user_agent, s.request_timeout)
    return _session
