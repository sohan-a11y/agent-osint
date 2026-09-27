"""Extract operator fingerprints from a web page.

Identifiers are only searched for inside <script> code/src, <noscript>, <ins>,
<meta> and <link> elements — not visible text — which removes most false
positives (e.g. a blog post that *mentions* a GA ID).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.privacy import classify_name, safe_name

# (kind, compiled regex, group index, normaliser)
_ID_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("ga_ua", re.compile(r"\b(UA-\d{4,10})-\d{1,4}\b"), 1),
    ("ga4", re.compile(r"\b(G-[A-Z0-9]{8,12})\b"), 1),
    ("gtm", re.compile(r"\b(GTM-[A-Z0-9]{4,9})\b"), 1),
    ("adsense", re.compile(r"\b(?:ca-)?(pub-\d{16})\b"), 1),
    ("gads", re.compile(r"\b(AW-\d{9,11})\b"), 1),
    ("fb_pixel", re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{15,16})['\"]"), 1),
    ("fb_pixel", re.compile(r"facebook\.com/tr\?id=(\d{15,16})"), 1),
    ("yandex_metrica", re.compile(r"\bym\(\s*(\d{6,10})\s*,\s*['\"]init['\"]"), 1),
    ("yandex_metrica", re.compile(r"mc\.yandex\.ru/watch/(\d{6,10})"), 1),
    ("hotjar", re.compile(r"\bhjid\s*:\s*(\d{6,8})\b"), 1),
    ("clarity", re.compile(r"clarity\.ms/tag/([a-z0-9]{8,12})\b"), 1),
    ("clarity", re.compile(r"['\"]clarity['\"]\s*,\s*['\"]script['\"]\s*,\s*['\"]([a-z0-9]{8,12})['\"]"), 1),
    ("tiktok_pixel", re.compile(r"ttq\.load\(\s*['\"]([A-Z0-9]{20})['\"]"), 1),
    ("linkedin_insight", re.compile(r"_linkedin_partner_id\s*=\s*['\"](\d{5,9})['\"]"), 1),
]

# Documentation / template placeholders that must never link sites.
PLACEHOLDER_VALUES = {
    "UA-0000", "UA-00000", "UA-000000", "UA-XXXXX", "G-XXXXXXXXXX", "G-XXXXXXXX", "GTM-XXXX", "GTM-XXXXXX",
    "GTM-XXXXXXX", "pub-0000000000000000", "pub-1234567890123456", "pub-xxxxxxxxxxxxxxxx", "AW-000000000",
    "000000000000000", "123456789012345", "1234567890123456",
}

IMPRINT_LINK_RE = re.compile(
    r"impressum|imprint|legal[\s_-]?notice|legal[\s_-]?info|mentions[\s_-]?l[ée]gales|aviso[\s_-]?legal|"
    r"note[\s_-]?legali|colofon|colophon|informacje[\s_-]?prawne|impresszum|tir[aá][zž]|"
    r"about[\s_-]?us|who[\s_-]?we[\s_-]?are|chi[\s_-]?siamo|qui[\s_-]?sommes|[üu]ber[\s_-]?uns|quienes[\s_-]?somos",
    re.IGNORECASE,
)
_IMPRINT_STRONG_RE = re.compile(
    r"impressum|imprint|legal[\s_-]?notice|mentions[\s_-]?l[ée]gales|aviso[\s_-]?legal|note[\s_-]?legali|"
    r"colofon|informacje[\s_-]?prawne|impresszum", re.IGNORECASE)

_COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–—]\s*\d{2,4})?\s*,?\s*)?(?:by\s+)?"
    r"([A-Za-zÀ-ÿ0-9][^\n|•·©]{1,80}?)(?:\.\s|\s*[|•·]|\s+all rights reserved|\s+alle rechte|\s+tous droits|$)",
    re.IGNORECASE,
)

_ORG_TYPES = {"organization", "newsmediaorganization", "corporation", "localbusiness", "onlinebusiness",
              "onlinestore", "educationalorganization", "governmentorganization", "ngo", "mediaorganization",
              "researchorganization", "store"}


@dataclass
class PageSignals:
    ids: dict[str, set[str]] = field(default_factory=dict)
    fb_domain_verification: set[str] = field(default_factory=set)
    jsonld_orgs: list[dict[str, str]] = field(default_factory=list)
    copyright_holders: list[str] = field(default_factory=list)
    site_name: str | None = None
    imprint_links: list[str] = field(default_factory=list)
    title: str | None = None

    def add_id(self, kind: str, value: str) -> None:
        if value in PLACEHOLDER_VALUES or value.upper() in PLACEHOLDER_VALUES:
            return
        self.ids.setdefault(kind, set()).add(value)


def _code_fragments(soup: BeautifulSoup) -> list[str]:
    frags: list[str] = []
    for tag in soup.find_all(["script", "noscript"]):
        if tag.get("src"):
            frags.append(str(tag.get("src")))
        if tag.string:
            frags.append(tag.string)
        elif tag.name == "noscript":
            frags.append(tag.decode_contents())
    for tag in soup.find_all("ins"):
        for attr in ("data-ad-client", "data-ad-slot"):
            if tag.get(attr):
                frags.append(str(tag.get(attr)))
    for tag in soup.find_all(["iframe", "img"]):
        src = tag.get("src")
        if src and any(h in src for h in ("googletagmanager", "facebook.com/tr", "yandex", "clarity.ms")):
            frags.append(str(src))
    for tag in soup.find_all("link"):
        href = tag.get("href")
        if href and ("googletagmanager" in href or "google-analytics" in href):
            frags.append(str(href))
    for tag in soup.find_all("meta"):
        name = (tag.get("name") or tag.get("property") or "").lower()
        if name in {"google-adsense-account", "google-adsense-platform-account"} and tag.get("content"):
            frags.append(str(tag.get("content")))
    return frags


def _walk_jsonld(obj: object, out: list[dict[str, str]], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_jsonld(item, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    types = obj.get("@type")
    type_list = [types] if isinstance(types, str) else (types if isinstance(types, list) else [])
    if any(str(t).lower() in _ORG_TYPES for t in type_list):
        name = obj.get("legalName") or obj.get("name")
        if isinstance(name, str) and name.strip():
            entry = {"name": name.strip()}
            if isinstance(obj.get("url"), str):
                entry["url"] = obj["url"]
            if isinstance(obj.get("legalName"), str):
                entry["legal_name"] = obj["legalName"].strip()
            out.append(entry)
    for key in ("publisher", "parentOrganization", "sourceOrganization", "copyrightHolder", "@graph", "author",
                "brand", "owner", "provider"):
        if key in obj:
            _walk_jsonld(obj[key], out, depth + 1)


def extract_page(html: str, base_url: str) -> PageSignals:
    soup = BeautifulSoup(html, "lxml")
    sig = PageSignals()
    if soup.title and soup.title.string:
        sig.title = " ".join(soup.title.string.split())[:200]
    frags = _code_fragments(soup)
    blob = "\n".join(frags)
    for kind, pattern, group in _ID_PATTERNS:
        for match in pattern.finditer(blob):
            sig.add_id(kind, match.group(group))
    for tag in soup.find_all("meta"):
        name = (tag.get("name") or tag.get("property") or "").lower()
        content = (tag.get("content") or "").strip()
        if name == "facebook-domain-verification" and re.fullmatch(r"[a-z0-9]{20,40}", content):
            sig.fb_domain_verification.add(content)
        elif name in {"og:site_name", "application-name"} and content and not sig.site_name:
            sig.site_name = content[:120]
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        _walk_jsonld(data, sig.jsonld_orgs)
    # Copyright holder: search footer first, then whole text.
    footer = soup.find("footer")
    texts = [footer.get_text(" ", strip=True)] if footer else []
    texts.append(soup.get_text(" ", strip=True)[-4000:])
    for text in texts:
        for m in _COPYRIGHT_RE.finditer(text):
            holder = m.group(1).strip(" ,.-")
            if 2 <= len(holder) <= 80 and classify_name(holder) != "person":
                sig.copyright_holders.append(holder)
        if sig.copyright_holders:
            break
    sig.copyright_holders = list(dict.fromkeys(sig.copyright_holders))[:3]
    # Imprint / legal links (strong matches first).
    strong, weak = [], []
    for a in soup.find_all("a", href=True):
        label = f"{a.get_text(' ', strip=True)} {a['href']}"
        if IMPRINT_LINK_RE.search(label):
            url = urljoin(base_url, a["href"])
            if not url.startswith("http"):
                continue
            (strong if _IMPRINT_STRONG_RE.search(label) else weak).append(url.split("#")[0])
    sig.imprint_links = list(dict.fromkeys(strong + weak))[:4]
    return sig


def normalise_org_name(name: str) -> str:
    """Normalisation key for comparing organisation names across sources."""
    text = name.lower()
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"\b(gmbh\s*&\s*co\.?\s*kg|gmbh|ag|se|kg|ltd|limited|llc|inc|incorporated|corp|corporation|plc|"
                  r"s\.?a\.?s?|sarl|s\.?r\.?l|s\.?p\.?a|b\.?v|n\.?v|oy|ab|a/s|aps|co|company|the|group|holdings?)\b\.?",
                  " ", text)
    text = re.sub(r"[^a-z0-9à-ÿ]+", " ", text)
    return " ".join(text.split())


def display_org(name: str) -> str | None:
    return safe_name(name)
