"""Heuristic hints about who organised a comment campaign.

Signals: organisation field values, URLs/domains mentioned in the template, "member of X" phrases,
and footers left by advocacy/comment-submission platforms. These are *hints* for a human to verify,
not attributions.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ..core.domains import registrable_domain
from ..core.privacy import WITHHELD, safe_name

# Text markers of bulk-comment / advocacy platforms (case-insensitive substrings or regexes).
PLATFORM_MARKERS: dict[str, str] = {
    "Action Network": r"actionnetwork\.org|action network",
    "EveryAction / NGP VAN": r"everyaction|ngpvan",
    "Quorum (Phone2Action)": r"phone2action|quorum\.us",
    "VoterVoice": r"votervoice",
    "Countable": r"countable\.us|via countable",
    "Resistbot": r"resistbot",
    "Engaging Networks": r"engagingnetworks|engaging networks",
    "Salsa Labs": r"salsalabs|salsa labs",
    "Muster": r"muster\.com",
    "CQ Engage / FiscalNote": r"cqengage|fiscalnote",
    "Change.org": r"change\.org",
    "MoveOn": r"moveon\.org",
    "Generic 'sent via' footer": r"\b(sent|submitted|delivered) (via|through|using) [a-z0-9 .-]{2,40}",
}
_URL_RE = re.compile(r"\bhttps?://([a-z0-9.-]+\.[a-z]{2,24})\b|\bwww\.([a-z0-9.-]+\.[a-z]{2,24})\b", re.IGNORECASE)
_MEMBER_RE = re.compile(
    r"\b(?:as an? (?:proud |concerned |longtime )?(?:member|supporter|volunteer|activist) of|on behalf of|member of)"
    r"\s+(?:the\s+)?([A-Z][\w&'’.-]*(?:\s+(?:of|for|and|the|[A-Z][\w&'’.-]*)){0,7})")
_IGNORE_DOMAINS = {"regulations.gov", "federalregister.gov", "gov.uk", "youtube.com", "google.com", "wikipedia.org"}


def sponsor_hints(members: list[dict[str, Any]], cluster_size: int) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    orgs = Counter()
    for r in members:
        if r.get("organization"):
            orgs[safe_name(r["organization"])] += 1
    for name, n in orgs.most_common(8):
        if name == WITHHELD:
            continue
        hints.append({"type": "organization_field", "value": name, "comments": n,
                      "share": round(n / cluster_size, 3)})
    domains: Counter[str] = Counter()
    platforms: Counter[str] = Counter()
    member_of: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    for r in members:
        text = r.get("text") or ""
        seen_d = set()
        for m in _URL_RE.finditer(text):
            host = (m.group(1) or m.group(2) or "").lower()
            root = registrable_domain(host)
            if root and root not in _IGNORE_DOMAINS and not root.endswith(".gov") and root not in seen_d:
                seen_d.add(root)
                domains[root] += 1
                samples.setdefault(f"domain:{root}", []).append(r["comment_id"])
        for label, pattern in PLATFORM_MARKERS.items():
            if re.search(pattern, text, re.IGNORECASE):
                platforms[label] += 1
                samples.setdefault(f"platform:{label}", []).append(r["comment_id"])
        for m in _MEMBER_RE.finditer(text):
            org = " ".join(m.group(1).split()).rstrip(".,")
            if len(org) > 3 and safe_name(org) != WITHHELD:
                member_of[org] += 1
                samples.setdefault(f"member:{org}", []).append(r["comment_id"])
    for d, n in domains.most_common(8):
        hints.append({"type": "domain_mentioned", "value": d, "comments": n, "share": round(n / cluster_size, 3),
                      "sample_comment_ids": samples[f"domain:{d}"][:5]})
    for p, n in platforms.most_common():
        hints.append({"type": "submission_platform_marker", "value": p, "comments": n,
                      "share": round(n / cluster_size, 3), "sample_comment_ids": samples[f"platform:{p}"][:5]})
    for o, n in member_of.most_common(5):
        hints.append({"type": "affiliation_phrase", "value": o, "comments": n, "share": round(n / cluster_size, 3),
                      "sample_comment_ids": samples[f"member:{o}"][:5]})
    hints.sort(key=lambda h: -h["comments"])
    return hints
