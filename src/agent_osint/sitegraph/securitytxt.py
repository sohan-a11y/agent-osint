"""security.txt (RFC 9116) fetching and parsing."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..core.domains import registrable_domain
from ..core.http import Fetcher, FetchError, FetchResult
from ..core.privacy import email_domain, safe_email

# Bug-bounty / disclosure platforms and free mail: shared by thousands of unrelated sites.
PLATFORM_DOMAINS = {
    "hackerone.com", "bugcrowd.com", "intigriti.com", "yeswehack.com", "openbugbounty.org", "zerocopter.com",
    "hackenproof.com", "federacy.com", "huntr.dev", "huntr.com", "synack.com", "cobalt.io", "immunefi.com",
    "responsibledisclosure.com", "disclose.io", "securitytxt.org", "github.com", "gitlab.com", "google.com",
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "proton.me", "protonmail.com", "wordpress.com",
    "wpengine.com", "cloudflare.com", "shopify.com", "squarespace.com", "wix.com", "automattic.com",
}


@dataclass
class SecurityTxt:
    url: str
    fields: dict[str, list[str]] = field(default_factory=dict)
    signed: bool = False

    def contacts(self) -> list[str]:
        out = []
        for c in self.fields.get("contact", []):
            if c.lower().startswith("mailto:") or ("@" in c and "://" not in c):
                out.append("mailto:" + (safe_email(c) or ""))
            elif c.lower().startswith("tel:"):
                continue  # phone numbers are not returned
            else:
                out.append(c)
        return out

    def linking_domains(self) -> list[str]:
        """Registrable domains from Contact/Canonical/Policy that can link sites (platforms excluded)."""
        doms: list[str] = []
        for key in ("contact", "canonical", "policy"):
            for value in self.fields.get(key, []):
                d = None
                if value.lower().startswith("mailto:") or ("@" in value and "://" not in value):
                    d = email_domain(value)
                elif value.lower().startswith(("http://", "https://")):
                    try:
                        d = httpx.URL(value).host
                    except httpx.InvalidURL:
                        d = None
                if d:
                    root = registrable_domain(d)
                    if root and root not in PLATFORM_DOMAINS:
                        doms.append(root)
        return list(dict.fromkeys(doms))


def parse_security_txt(text: str, url: str = "") -> SecurityTxt:
    st = SecurityTxt(url=url)
    lines = text.splitlines()
    if lines and lines[0].startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        st.signed = True
    in_sig = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("-----BEGIN PGP SIGNATURE-----"):
            in_sig = True
        if in_sig or line.startswith("-----") or line.lower().startswith("hash:") or not line or line.startswith("#"):
            if line.startswith("-----END PGP SIGNATURE-----"):
                in_sig = False
            continue
        if line.startswith("- "):
            line = line[2:]  # dash-escaped lines in cleartext signatures
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in {"contact", "expires", "encryption", "acknowledgments", "preferred-languages", "canonical",
                   "policy", "hiring", "csaf"}:
            st.fields.setdefault(key, []).append(value.strip())
    return st


async def fetch_security_txt(fetcher: Fetcher, host: str) -> tuple[SecurityTxt | None, FetchResult | None, str | None]:
    last = None
    for path in ("/.well-known/security.txt", "/security.txt"):
        url = f"https://{host}{path}"
        try:
            res = await fetcher.get(url, max_bytes=256 * 1024)
        except FetchError as exc:
            last = str(exc)
            continue
        if res.status != 200:
            last = f"HTTP {res.status}"
            continue
        head = res.content[:256].lstrip().lower()
        if head.startswith(b"<!doctype") or head.startswith(b"<html"):
            last = "HTML page instead of security.txt"
            continue
        st = parse_security_txt(res.text(), res.url)
        if "contact" not in st.fields:
            last = "no Contact field (not a valid security.txt)"
            continue
        return st, res, None
    return None, None, last
