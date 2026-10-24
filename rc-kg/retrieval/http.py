"""POST to model servers without sending local traffic through the site proxy.

Explorer sets http_proxy/https_proxy to a Squid with no no_proxy, and that
Squid answers 403 for 127.0.0.1 and compute-node addresses -- exactly where a
vLLM in a Slurm job lives. So loopback, private and cluster-internal hosts go
direct, and anything public (a hosted API, if someone configures one) still
uses the proxy.
"""

import ipaddress
import socket
from urllib.parse import urlparse

import requests

_DIRECT = {"http": None, "https": None}


def _is_internal(host: str) -> bool:
    if host in ("localhost",) or "." not in host:
        return True                                   # bare node names: d0110
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def post(url: str, **kwargs) -> requests.Response:
    if _is_internal(urlparse(url).hostname or ""):
        kwargs["proxies"] = _DIRECT
    return requests.post(url, **kwargs)
