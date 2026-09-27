"""Parse legal-notice / imprint pages for legal entities and registry numbers.

Only organisation-level facts are returned. Names of natural persons (e.g.
"Geschäftsführer: …") are never extracted; entity names that look like a
person trading under their own name are withheld by the privacy filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..core.privacy import classify_name, safe_email

_SUFFIX = (
    r"GmbH\s*&\s*Co\.?\s*KGaA|GmbH\s*&\s*Co\.?\s*KG|gGmbH|GmbH|AG\s*&\s*Co\.?\s*KG|KGaA|AG|SE|KG|OHG|"
    r"UG\s*\(haftungsbeschränkt\)|e\.\s?V\.|eG|Ltd\.?|Limited|LLC|L\.L\.C\.|Inc\.?|PLC|plc|LLP|"
    r"S\.A\.S\.U?|SASU?|SARL|S\.à\s?r\.l\.|S\.A\.|S\.L\.U?\.?|S\.r\.l\.|S\.p\.A\.|B\.V\.|N\.V\.|Oyj?|AB|A/S|ApS|ASA|"
    r"sp\.\s?z\s?o\.o\.|s\.r\.o\.|a\.s\.|Kft\.|Zrt\.|d\.o\.o\.|Pty\.?\s?Ltd\.?|Pvt\.?\s?Ltd\.?|Corp\.?|Corporation|"
    r"S\.C\.|Lda\.?|Ltda\.?|SpA|Srl|BV|NV|SA|SL"
)
_ENTITY_RE = re.compile(
    r"((?:[A-ZÄÖÜÀ-Þ0-9][\w.'’&+\-]*\s+){0,7}?[A-ZÄÖÜÀ-Þ0-9][\w.'’&+\-]*\s*,?\s+(?:" + _SUFFIX + r"))(?=[\s,.;:)\]]|$)"
)

_REGNO_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("DE-HR", re.compile(r"\b(HR[AB]|VR|GnR|PR)\s?(\d{1,6}\s?[A-Z]{0,2})\b")),
    ("GB-CH", re.compile(r"(?:company|registered|registration)\s+(?:registration\s+)?(?:number|no\.?)\s*[:#]?\s*"
                         r"((?:[A-Z]{2})?\d{6,8})\b", re.IGNORECASE)),
    ("FR-SIREN", re.compile(r"(?:SIREN|SIRET|RCS[^0-9\n]{0,30})\s*:?\s*(\d{3}\s?\d{3}\s?\d{3})(?:\s?\d{5})?\b")),
    ("NL-KVK", re.compile(r"(?:KvK|Kamer van Koophandel|Chamber of Commerce)[^0-9\n]{0,25}(\d{8})\b", re.IGNORECASE)),
    ("LEI", re.compile(r"\bLEI\b[^A-Z0-9\n]{0,10}([A-Z0-9]{18}\d{2})\b")),
]
_VAT_RE = re.compile(
    r"\b(ATU\d{8}|BE0?\d{9,10}|BG\d{9,10}|CY\d{8}[A-Z]|CZ\d{8,10}|DE\d{9}|DK\d{8}|EE\d{9}|EL\d{9}|ES[A-Z0-9]\d{7}[A-Z0-9]|"
    r"FI\d{8}|FR[A-HJ-NP-Z0-9]{2}\d{9}|HR\d{11}|HU\d{8}|IE\d{7}[A-W][A-I]?|IE\d[A-Z+*]\d{5}[A-W]|IT\d{11}|LT\d{9,12}|"
    r"LU\d{8}|LV\d{11}|MT\d{8}|NL\d{9}B\d{2}|PL\d{10}|PT\d{9}|RO\d{2,10}|SE\d{12}|SI\d{8}|SK\d{10}|GB\d{9}|GB\d{12}|"
    r"CHE[-\s]?\d{3}\.?\d{3}\.?\d{3})\b"
)
_VAT_CONTEXT_RE = re.compile(r"(USt|VAT|TVA|IVA|BTW|MwSt|UID|NIF|CIF|P\.?\s?IVA|Umsatzsteuer|moms)", re.IGNORECASE)
_LABEL_PREFIX_RE = re.compile(
    r"^(?:(?:copyright|operated|operator|owner|owned|betreiber|anbieter|herausgeber|verantwortlich|publisher|"
    r"editor|company|firma|name|address|adresse|impressum|imprint|contact|kontakt|angaben|information|legal|"
    r"notice|site|website|webseite)\s+)+",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")


@dataclass
class ImprintFacts:
    entities: list[str] = field(default_factory=list)
    registration_numbers: list[dict[str, str]] = field(default_factory=list)
    vat_ids: list[str] = field(default_factory=list)
    role_emails: list[str] = field(default_factory=list)
    withheld_person_entities: int = 0

    def is_empty(self) -> bool:
        return not (self.entities or self.registration_numbers or self.vat_ids)


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return main.get_text("\n", strip=True)


def normalise_regno(scheme: str, raw: str) -> str:
    return f"{scheme}:{re.sub(r'[^A-Za-z0-9]', '', raw).upper()}"


def parse_imprint(html: str) -> ImprintFacts:
    text = page_text(html)
    facts = ImprintFacts()
    seen: set[str] = set()
    for m in _ENTITY_RE.finditer(text):
        name = " ".join(m.group(1).split()).strip(" ,")
        # Trim leading label words such as "Anbieter:" or "Operated by"
        name = _LABEL_PREFIX_RE.sub("", name)
        if len(name) < 4 or name.lower() in seen:
            continue
        seen.add(name.lower())
        if classify_name(name) == "person":
            facts.withheld_person_entities += 1
            continue
        facts.entities.append(name)
    facts.entities = facts.entities[:6]
    for scheme, pattern in _REGNO_PATTERNS:
        for m in pattern.finditer(text):
            if scheme == "DE-HR":
                raw = f"{m.group(1)} {m.group(2)}"
                court = re.search(r"(Amtsgericht|Registergericht|AG)\s+([A-ZÄÖÜ][\wäöüß\-]+(?:\s[a-z]{2,3}\s[A-ZÄÖÜ][\w\-]+)?)",
                                  text[max(0, m.start() - 120): m.end() + 120])
                item = {"scheme": "DE-HR", "value": raw.strip(), "key": normalise_regno("DE-HR", raw)}
                if court:
                    item["court"] = court.group(2)
                    item["key"] = normalise_regno("DE-HR", f"{court.group(2)}{raw}")
            else:
                raw = m.group(1)
                item = {"scheme": scheme, "value": raw.strip(), "key": normalise_regno(scheme, raw)}
            if item["key"] not in {r["key"] for r in facts.registration_numbers}:
                facts.registration_numbers.append(item)
    for m in _VAT_RE.finditer(text):
        context = text[max(0, m.start() - 60): m.start()]
        if _VAT_CONTEXT_RE.search(context):
            vat = re.sub(r"[\s.\-]", "", m.group(1)).upper()
            if vat not in facts.vat_ids:
                facts.vat_ids.append(vat)
    for m in _EMAIL_RE.finditer(text):
        safe = safe_email(m.group(0))
        if safe and not safe.startswith("[") and safe not in facts.role_emails:
            facts.role_emails.append(safe)
    facts.role_emails = facts.role_emails[:5]
    return facts
