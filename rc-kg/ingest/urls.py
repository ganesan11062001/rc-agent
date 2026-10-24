"""URL normalization and internal/external classification.

Normalization has to be *stable* rather than clever: it is the join key between
a body link and a Page node, so the same page reached two ways must produce one
string. Rules, in order:

  lowercase scheme and host, drop "www.", force https
  drop the query string and the fragment
  collapse duplicate slashes in the path
  strip the trailing slash (but never reduce the path to "")

The fragment is dropped deliberately: a link to /policies/#scratch and one to
/policies/ point at the same Page, and the section is modelled by the Section
node, not by the Page URL.
"""

import re
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

_MULTISLASH = re.compile(r"/{2,}")
# Links that are not web resources and must never become nodes.
_NON_HTTP = ("mailto:", "tel:", "javascript:", "data:", "sms:", "ftp:")


def normalize(url: str, base_host: str = "rc.northeastern.edu") -> Optional[str]:
    """Return a canonical absolute URL, or None if this is not a web link."""
    if not url:
        return None
    raw = url.strip()
    if raw.startswith("#") or raw.lower().startswith(_NON_HTTP):
        return None

    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = "https://%s%s" % (base_host, raw)
    elif not raw.lower().startswith(("http://", "https://")):
        # A bare relative link ("getting-help/"). Resolving it needs the page it
        # came from, which the caller has and we do not, so refuse rather than
        # guess a wrong absolute URL.
        return None

    p = urlparse(raw)
    host = p.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = _MULTISLASH.sub("/", p.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def host_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def classify(url: str, base_host: str = "rc.northeastern.edu") -> Tuple[str, str]:
    """Return ("internal"|"external", host) for an already-normalized URL."""
    host = host_of(url)
    return ("internal" if host == base_host else "external", host)


def is_wp_api_url(url: str) -> bool:
    """wp-json links appear in body text on this site and are not content."""
    return "/wp-json/" in url
