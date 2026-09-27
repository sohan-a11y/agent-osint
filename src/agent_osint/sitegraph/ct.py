"""Certificate Transparency: which registrable domains share certificates.

Primary source: SSLMate Cert Spotter API (``/v1/issuances`` with
``expand=dns_names``), which returns the full SAN list of each certificate.
Certificates covering many unrelated domains (CDN/shared-hosting certs) are
flagged as ``shared`` and do not link sites.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..core.domains import registrable_domain
from ..core.http import Fetcher, FetchError, FetchResult

CERTSPOTTER_URL = "https://api.certspotter.com/v1/issuances"
SHARED_CERT_THRESHOLD = 12  # more distinct registrable domains than this => shared/CDN cert


@dataclass
class CertInfo:
    cert_id: str
    roots: list[str]
    not_before: str | None
    not_after: str | None
    issuer: str | None
    shared: bool


@dataclass
class CTResult:
    certs: list[CertInfo] = field(default_factory=list)
    raw: FetchResult | None = None
    error: str | None = None

    def co_domains(self, own_root: str) -> dict[str, list[str]]:
        """Other registrable domains -> list of (non-shared) cert ids they co-occur in."""
        out: dict[str, list[str]] = {}
        for cert in self.certs:
            if cert.shared:
                continue
            for root in cert.roots:
                if root != own_root:
                    out.setdefault(root, []).append(cert.cert_id)
        return out


def parse_certspotter(payload: list[dict]) -> list[CertInfo]:
    certs = []
    for item in payload:
        names = item.get("dns_names") or []
        roots = sorted({r for r in (registrable_domain(n.lstrip("*.").lower()) for n in names) if r})
        cid = item.get("cert_sha256") or item.get("tbs_sha256") or hashlib.sha256(
            ",".join(sorted(names)).encode()).hexdigest()
        issuer = None
        if isinstance(item.get("issuer"), dict):
            issuer = item["issuer"].get("friendly_name") or item["issuer"].get("name")
        certs.append(CertInfo(cert_id=str(cid), roots=roots, not_before=item.get("not_before"),
                              not_after=item.get("not_after"), issuer=issuer,
                              shared=len(roots) > SHARED_CERT_THRESHOLD))
    return certs


async def fetch_ct(fetcher: Fetcher, root: str, *, max_pages: int = 2, api_key: str | None = None) -> CTResult:
    result = CTResult()
    after = None
    for _ in range(max_pages):
        params = {"domain": root, "include_subdomains": "true", "expand": ["dns_names", "issuer"]}
        if after:
            params["after"] = after
        try:
            res = await fetcher.get(CERTSPOTTER_URL, params=params, max_bytes=16 * 1024 * 1024, cache_ttl=7 * 86400,
                                    headers={"Authorization": f"Bearer {api_key}"} if api_key else None)
        except FetchError as exc:
            result.error = str(exc)
            return result
        if res.status != 200:
            result.error = f"Cert Spotter HTTP {res.status}"
            return result
        result.raw = result.raw or res
        try:
            payload = res.json()
        except ValueError:
            result.error = "Cert Spotter returned invalid JSON"
            return result
        if not isinstance(payload, list) or not payload:
            break
        result.certs.extend(parse_certspotter(payload))
        after = payload[-1].get("id")
        if len(payload) < 100 or not after:
            break
    return result
