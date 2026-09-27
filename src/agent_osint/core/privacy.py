"""Natural-person suppression.

These tools are about organizations. Whenever a record could name a private
individual (a sole trader in sellers.json, a managing director in an imprint, a
commenter on a rulemaking docket, a PubPeer commenter, a sole-trader supplier),
the name is withheld. The heuristics deliberately err toward withholding:
a false "withheld" costs a little utility, a false "shown" can expose a person.

There is no switch to turn this off from an agent. Operators who have a lawful
basis to process personal data should use the upstream sources directly.
"""

from __future__ import annotations

import re

WITHHELD = "[individual — withheld]"

_ORG_SUFFIXES = [
    r"inc\.?", r"incorporated", r"corp\.?", r"corporation", r"co\.?", r"company", r"llc", r"l\.l\.c\.?",
    r"llp", r"l\.l\.p\.?", r"lp", r"ltd\.?", r"limited", r"plc", r"gmbh", r"ggmbh", r"mbh", r"ag", r"kg",
    r"kgaa", r"ohg", r"e\.\s?v\.?", r"eg", r"ug", r"se", r"s\.?a\.?", r"s\.?a\.?s\.?", r"sarl", r"s\.?à\.?\s?r\.?l\.?",
    r"s\.?r\.?l\.?", r"s\.?p\.?a\.?", r"b\.?v\.?", r"n\.?v\.?", r"oy", r"oyj", r"ab", r"a/s", r"aps", r"as", r"asa",
    r"sp\.?\s?z\s?o\.?\s?o\.?", r"s\.?r\.?o\.?", r"a\.?s\.?", r"kft\.?", r"zrt\.?", r"nyrt\.?", r"d\.?o\.?o\.?",
    r"oü", r"pty", r"pvt", r"k\.?k\.?", r"s\.?l\.?", r"s\.?l\.?u\.?", r"lda\.?", r"ltda\.?", r"s\.?c\.?",
    r"sas", r"sasu", r"eurl", r"sa de cv", r"gbr", r"partg", r"bhd", r"sdn", r"tbk", r"pte", r"jsc", r"ooo",
    r"pjsc", r"ojsc", r"cic", r"cio", r"ev", r"e\.v", r"gie", r"scop", r"scic", r"ug \(haftungsbeschränkt\)",
]
_ORG_SUFFIX_RE = re.compile(r"(?:^|[\s,(])(?:" + "|".join(_ORG_SUFFIXES) + r")(?=$|[\s,.)])", re.IGNORECASE)

# Markers that a registered name belongs to a natural person trading under their own name.
_PERSON_TRADER_RE = re.compile(
    r"\b(e\.\s?k\.?|eingetragene[rn]? kauf(?:mann|frau)|einzelunternehme[rn]|inh\.|inhaber(?:in)?|"
    r"sole (?:trader|proprietor(?:ship)?)|freelancer?|auto-?entrepreneur|micro-?entrepreneur|"
    r"entreprise individuelle|ditta individuale|eenmanszaak)\b",
    re.IGNORECASE,
)

_ORG_KEYWORDS = {
    "media", "news", "group", "groupe", "gruppe", "holding", "holdings", "network", "networks", "press", "presse",
    "publishing", "publisher", "publishers", "publications", "digital", "technologies", "technology", "tech",
    "solutions", "services", "systems", "labs", "lab", "studio", "studios", "agency", "agentur", "association",
    "associazione", "asociación", "foundation", "stiftung", "fondation", "fundación", "institute", "institut",
    "instituto", "university", "universität", "université", "universidad", "college", "school", "academy",
    "society", "council", "committee", "party", "union", "club", "church", "ministry", "department",
    "government", "office", "authority", "bank", "trust", "fund", "partners", "capital", "ventures",
    "international", "global", "online", "interactive", "entertainment", "productions", "production",
    "marketing", "advertising", "ads", "adtech", "magazine", "magazin", "radio", "tv", "television",
    "broadcasting", "games", "gaming", "software", "apps", "app", "web", "internet", "verlag", "zeitung",
    "journal", "times", "post", "daily", "weekly", "herald", "tribune", "gazette", "observer", "review",
    "exchange", "platform", "marketplace", "commerce", "retail", "store", "shop", "hotel", "hospital",
    "clinic", "center", "centre", "consulting", "consultancy", "industries", "industrie", "enterprises",
    "communications", "telecom", "energy", "insurance", "logistics", "the", "of", "and", "&",
    "und", "et", "y", "e", "ministère", "ministerio", "republic", "national", "federal", "municipal",
    "county", "city", "state", "region", "public", "corporation", "cooperative", "coop", "insider", "today", "now", "report", "wire", "hub", "world", "watch", "guide", "direct", "express", "business", "finance", "money", "sports", "sport", "health", "travel", "food", "news24", "live",
}
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "herr", "frau", "mme", "m", "sr", "sra", "sig", "dott"}
_NAME_TOKEN_RE = re.compile(
    r"^(?:Mc|Mac|O'|D'|De|Van|Von)?[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+(?:-[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+)*\.?$|^[A-ZÀ-ÖØ-Þ]\.$|"
    r"^(?:de|da|di|del|della|van|von|der|den|le|la|du|dos|das|bin|ibn|al)$"
)


def classify_name(name: str | None) -> str:
    """Return 'organization', 'person', or 'unknown' for a free-text name."""
    if not name:
        return "unknown"
    text = " ".join(str(name).split())
    if not text:
        return "unknown"
    if _PERSON_TRADER_RE.search(text):
        return "person"
    if _ORG_SUFFIX_RE.search(text):
        return "organization"
    lowered = text.lower()
    if re.search(r"[a-z0-9-]+\.(?:[a-z]{2,24})\b", lowered) or re.search(r"\d", lowered):
        return "organization"  # domain-like or contains digits -> brand/entity
    tokens = re.split(r"[\s,]+", text)
    tokens = [t for t in tokens if t]
    words = {t.strip(".,()").lower() for t in tokens}
    if words & _ORG_KEYWORDS:
        return "organization"
    if any(len(t) >= 3 and t.isupper() and t.isalpha() for t in tokens) and len(tokens) <= 2:
        return "organization"  # acronym, e.g. "BBC", "ACME"
    core = [t for t in tokens if t.strip(".").lower() not in _HONORIFICS]
    if 2 <= len(core) <= 4 and all(_NAME_TOKEN_RE.match(t.strip(",")) for t in core):
        return "person"
    if "," in text and 2 <= len(core) <= 4 and all(_NAME_TOKEN_RE.match(t.strip(",")) for t in core):
        return "person"
    if len(tokens) >= 2 and words & _HONORIFICS:
        return "person"
    return "unknown"


def safe_name(name: str | None) -> str | None:
    """Return the name unless it looks like a natural person."""
    if name is None:
        return None
    return WITHHELD if classify_name(name) == "person" else " ".join(str(name).split())


_ROLE_LOCALPARTS = {
    "security", "abuse", "info", "contact", "admin", "administrator", "support", "privacy", "legal", "press",
    "media", "ads", "adops", "ad-ops", "advertising", "sales", "webmaster", "hostmaster", "postmaster", "noc",
    "soc", "cert", "csirt", "psirt", "hello", "team", "office", "dpo", "gdpr", "compliance", "publishers",
    "publisher", "partners", "partnerships", "bizdev", "business", "marketing", "editor", "editorial",
    "newsroom", "redaktion", "kontakt", "impressum", "datenschutz", "service", "help", "helpdesk", "hr",
    "jobs", "careers", "billing", "accounts", "finance", "investors", "ir", "communications", "comms",
    "vulnerability", "vulns", "bugbounty", "bounty", "disclosure", "responsible-disclosure", "secure",
    "infosec", "it", "tech", "technical", "operations", "ops", "enquiries", "inquiries", "general", "mail",
    "email", "noreply", "no-reply", "ad", "monetization", "programmatic", "revenue", "yield", "seller",
    "sellers", "supply", "demand", "trust", "safety", "trustandsafety", "policy", "registrar", "domains",
}
_FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "gmx.de", "gmx.net", "gmx.com", "web.de",
    "t-online.de", "proton.me", "protonmail.com", "pm.me", "aol.com", "mail.ru", "yandex.ru", "yandex.com",
    "qq.com", "163.com", "126.com", "zoho.com", "tutanota.com", "fastmail.com", "orange.fr", "free.fr",
    "libero.it", "laposte.net", "seznam.cz", "wp.pl", "o2.pl", "interia.pl",
}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+|00)?\d[\d\s().\-/]{7,}\d(?!\w)")


def safe_email(email: str | None) -> str | None:
    """Keep role mailboxes (security@, info@); withhold personal ones."""
    if not email:
        return None
    email = email.strip().removeprefix("mailto:").split("?")[0]
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    domain = domain.lower()
    base_local = local.lower().split("+")[0]
    if domain in _FREE_MAIL:
        return "[personal mailbox — withheld]"
    if base_local in _ROLE_LOCALPARTS or any(base_local.startswith(r + "-") or base_local.startswith(r + ".")
                                             for r in ("security", "abuse", "privacy", "legal", "ads", "adops")):
        return f"{local}@{domain}"
    return f"[person]@{domain}"


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.strip().removeprefix("mailto:").split("?")[0].rsplit("@", 1)[1].lower().strip(".")


def scrub_text(text: str | None) -> str | None:
    """Remove personal emails and phone numbers from free text."""
    if text is None:
        return None
    text = _EMAIL_RE.sub(lambda m: safe_email(m.group(0)) or "", text)
    return _PHONE_RE.sub("[phone — withheld]", text)


_CAP_SEQ_RE = re.compile(
    r"\b(?:(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+)?[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+(?:-[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+)?"
    r"(?:\s+(?:[A-ZÀ-ÖØ-Þ]\.|[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+(?:-[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+)?)){1,3}\b"
)
_NOT_NAME_WORDS = {
    "The", "This", "That", "These", "Those", "I", "We", "You", "My", "Our", "Your", "It", "In", "On", "At", "For",
    "As", "If", "When", "While", "Please", "Thank", "Dear", "Sincerely", "Regards", "However", "Therefore",
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
    "December", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "United", "States",
    "America", "American", "Congress", "Federal", "Department", "Agency", "Administration", "Act", "Rule", "Section",
    "Secretary", "President", "Commission", "Office", "Service", "National", "State", "Public", "Health", "Food",
    "Drug", "Environmental", "Protection", "Security", "Bureau", "Court", "Supreme", "House", "Senate", "New", "York",
    "California", "Texas", "Florida", "Washington", "Medicare", "Medicaid", "Social", "Internal", "Revenue",
}


def mask_names(text: str | None) -> str | None:
    """Mask sequences that look like personal names in free text (conservative)."""
    if text is None:
        return None

    def repl(m: re.Match[str]) -> str:
        seq = m.group(0)
        tokens = seq.split()
        if tokens[0] in _NOT_NAME_WORDS or any(t in _NOT_NAME_WORDS for t in tokens):
            return seq
        return "[name]" if classify_name(seq) == "person" else seq

    return _CAP_SEQ_RE.sub(repl, text)


def scrub_all(text: str | None) -> str | None:
    return mask_names(scrub_text(text))
