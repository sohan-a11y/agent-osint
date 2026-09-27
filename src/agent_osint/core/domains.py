"""Domain normalization helpers using the bundled Public Suffix List snapshot."""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


class InvalidDomain(ValueError):
    pass


def host_from_input(value: str) -> str:
    """Accept 'example.com', 'https://www.example.com/path', 'Example.COM.' -> 'www.example.com'."""
    raw = (value or "").strip()
    if not raw:
        raise InvalidDomain("empty domain")
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlsplit(raw).hostname or "").strip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidDomain(f"invalid domain: {value!r}") from exc
    if not _HOST_RE.match(host):
        raise InvalidDomain(f"invalid domain: {value!r}")
    return host


@lru_cache(maxsize=65536)
def registrable_domain(host: str) -> str | None:
    """'news.bbc.co.uk' -> 'bbc.co.uk'. Returns None for bare suffixes or IPs."""
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}".lower()


def root_domain(value: str) -> str:
    host = host_from_input(value)
    root = registrable_domain(host)
    if not root:
        raise InvalidDomain(f"{value!r} has no registrable domain")
    return root
