"""ads.txt / app-ads.txt fetching and parsing per IAB Tech Lab ads.txt 1.1.

Spec points implemented:
* records: ``<ad system domain>, <publisher account id>, <DIRECT|RESELLER>[, <cert authority id>]``
* ``#`` starts a comment; whitespace is ignored; relationship is case-insensitive
* variables: CONTACT, SUBDOMAIN, INVENTORYPARTNERDOMAIN, OWNERDOMAIN, MANAGERDOMAIN (value[, country])
* file lives at the root domain; HTTPS preferred with HTTP fallback
* redirects are followed only within the original root domain, plus at most one hop outside it;
  a redirect *from* the outside location is an error
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from ..core.domains import registrable_domain
from ..core.http import Fetcher, FetchError, FetchResult
from ..core.privacy import email_domain, safe_email

_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


@dataclass
class AdsRecord:
    ad_system: str
    account_id: str
    relationship: str
    cert_id: str | None = None


@dataclass
class AdsTxt:
    url: str
    records: list[AdsRecord] = field(default_factory=list)
    variables: dict[str, list[str]] = field(default_factory=dict)
    invalid_lines: int = 0

    @property
    def direct(self) -> list[AdsRecord]:
        return [r for r in self.records if r.relationship == "DIRECT"]

    def owner_domains(self) -> list[str]:
        return [v.lower() for v in self.variables.get("OWNERDOMAIN", []) if _DOMAIN_RE.match(v.lower())]

    def manager_domains(self) -> list[dict[str, str]]:
        out = []
        for v in self.variables.get("MANAGERDOMAIN", []):
            parts = [p.strip() for p in v.split(",")]
            if parts and _DOMAIN_RE.match(parts[0].lower()):
                item = {"domain": parts[0].lower()}
                if len(parts) > 1 and re.fullmatch(r"[A-Za-z]{2}", parts[1]):
                    item["country"] = parts[1].upper()
                out.append(item)
        return out

    def contacts(self) -> list[str]:
        out = []
        for v in self.variables.get("CONTACT", []):
            if "@" in v:
                out.append(safe_email(v) or "")
            elif v.lower().startswith(("http://", "https://")):
                out.append(v)
        return [c for c in out if c]

    def contact_domains(self) -> list[str]:
        doms = []
        for v in self.variables.get("CONTACT", []):
            d = email_domain(v) if "@" in v else None
            if d is None and v.lower().startswith(("http://", "https://")):
                d = httpx.URL(v).host
            if d:
                root = registrable_domain(d)
                if root:
                    doms.append(root)
        return list(dict.fromkeys(doms))


def parse_ads_txt(text: str, url: str = "") -> AdsTxt:
    result = AdsTxt(url=url)
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().strip("﻿")
        if not line:
            continue
        if "=" in line and "," not in line.split("=", 1)[0]:
            key, value = line.split("=", 1)
            key = key.strip().upper()
            if key in {"CONTACT", "SUBDOMAIN", "INVENTORYPARTNERDOMAIN", "OWNERDOMAIN", "MANAGERDOMAIN"}:
                result.variables.setdefault(key, []).append(value.strip())
                continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 3:
            result.invalid_lines += 1
            continue
        system = fields[0].lower().rstrip(".")
        rel = fields[2].upper()
        if not _DOMAIN_RE.match(system) or rel not in {"DIRECT", "RESELLER"} or not fields[1]:
            result.invalid_lines += 1
            continue
        result.records.append(AdsRecord(system, fields[1], rel, fields[3] if len(fields) > 3 and fields[3] else None))
    return result


def _looks_like_html(res: FetchResult) -> bool:
    head = res.content[:512].lstrip().lower()
    return "text/html" in res.content_type.lower() or head.startswith(b"<!doctype") or head.startswith(b"<html")


async def fetch_ads_txt(fetcher: Fetcher, root: str, filename: str = "ads.txt") -> tuple[AdsTxt | None, FetchResult | None, str | None]:
    """Fetch /ads.txt for a root domain. Returns (parsed, raw_result, error)."""
    last_error: str | None = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{root}/{filename}"
        outside_hops = 0
        current = url
        for _ in range(6):
            try:
                res = await fetcher.get(current, follow_redirects=False, max_bytes=4 * 1024 * 1024)
            except FetchError as exc:
                last_error = str(exc)
                break
            if res.status in (301, 302, 303, 307, 308) and "location" in res.headers:
                nxt = str(httpx.URL(current).join(res.headers["location"]))
                nxt_root = registrable_domain(httpx.URL(nxt).host or "")
                if nxt_root != root:
                    if outside_hops >= 1:
                        last_error = "ads.txt redirected more than once outside the root domain (spec: error)"
                        break
                    outside_hops += 1
                elif outside_hops:
                    last_error = "third-party ads.txt location redirected again (spec: error)"
                    break
                current = nxt
                continue
            if res.status != 200:
                last_error = f"HTTP {res.status}"
                break
            if _looks_like_html(res):
                last_error = "response is an HTML page, not an ads.txt file"
                break
            parsed = parse_ads_txt(res.text(), res.url)
            if not parsed.records and not parsed.variables:
                last_error = "file contains no valid records"
                break
            return parsed, res, None
    return None, None, last_error
