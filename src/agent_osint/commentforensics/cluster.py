"""Form-letter / campaign detection with exact hashing + MinHash LSH near-duplicate clustering."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from datasketch import MinHash, MinHashLSH

_ATTACHMENT_ONLY_RE = re.compile(
    r"^\s*(please\s+)?(see|refer to|view)\s+(the\s+)?(attached|attachment|enclosed)[\s\w().,:-]{0,60}$", re.IGNORECASE)
_SALUTATION_RE = re.compile(r"^\s*(dear|to whom it may concern|hello|hi|greetings|re:|subject:)[^\n]{0,120}\n", re.IGNORECASE)
_SIGNOFF_RE = re.compile(
    r"\n\s*(sincerely|regards|best regards|kind regards|respectfully|thank you|thanks|yours truly|best)[,.!]?\s*(\n[^\n]{0,80}){0,4}\s*$",
    re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def display_text(text: str | None) -> str:
    """Comment text with salutation/sign-off (where names live) removed."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    t = re.sub(r"<[^>]+>", " ", t)
    t = _SALUTATION_RE.sub("", t, count=1)
    t = _SIGNOFF_RE.sub("", t)
    return t.strip()


def normalise(text: str | None) -> str:
    t = display_text(text).lower()
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def is_attachment_only(text: str | None) -> bool:
    return not text or bool(_ATTACHMENT_ONLY_RE.match(text.strip())) or len(normalise(text)) < 3


def shingles(norm: str, k: int = 5) -> set[str]:
    words = norm.split()
    if len(words) < k:
        return {norm}
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


class _UF:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class ClusterResult:
    clusters: list[dict[str, Any]]
    assignments: dict[str, str]
    n_total: int
    n_clusterable: int
    n_attachment_only: int
    n_unique_texts: int


def cluster_comments(rows: list[dict[str, Any]], *, threshold: float = 0.8, num_perm: int = 128,
                     min_size: int = 2) -> ClusterResult:
    """Group comments whose normalised text has Jaccard similarity >= threshold (5-word shingles)."""
    usable = []
    attachment_only = 0
    for r in rows:
        if r.get("withdrawn"):
            continue
        if is_attachment_only(r.get("text")):
            attachment_only += 1
            continue
        usable.append(r)
    # 1) exact duplicates on normalised text
    by_hash: dict[str, list[dict]] = defaultdict(list)
    norm_of: dict[str, str] = {}
    for r in usable:
        n = normalise(r["text"])
        h = hashlib.sha1(n.encode()).hexdigest()
        by_hash[h].append(r)
        norm_of[h] = n
    # 2) near duplicates among unique texts
    uf = _UF()
    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    sigs: dict[str, MinHash] = {}
    for h, norm in norm_of.items():
        m = MinHash(num_perm=num_perm, seed=7)
        for sh in shingles(norm):
            m.update(sh.encode())
        sigs[h] = m
        for cand in lsh.query(m):
            if sigs[cand].jaccard(m) >= threshold:
                uf.union(h, cand)
        lsh.insert(h, m)
        uf.find(h)
    groups: dict[str, list[str]] = defaultdict(list)
    for h in norm_of:
        groups[uf.find(h)].append(h)

    clusters = []
    assignments: dict[str, str] = {}
    for _root, hashes in groups.items():
        members = [r for h in hashes for r in by_hash[h]]
        size = len(members) + sum(max(0, (r.get("duplicate_comments") or 0)) for r in members)
        if len(members) < min_size and size < min_size:
            continue
        top_hash = max(hashes, key=lambda h: (len(by_hash[h]), -len(norm_of[h])))
        rep = by_hash[top_hash][0]
        exact_share = len(by_hash[top_hash]) / len(members)
        cid = "c" + hashlib.sha1(",".join(sorted(r["comment_id"] for r in members)).encode()).hexdigest()[:10]
        for r in members:
            assignments[r["comment_id"]] = cid
        dates = sorted(d for d in (_parse_date(r.get("posted_date")) for r in members) if d)
        per_day = Counter(d.date().isoformat() for d in dates)
        orgs = Counter(r["organization"] for r in members if r.get("organization"))
        clusters.append({
            "cluster_id": cid,
            "size": len(members),
            "size_including_agency_duplicate_counts": size,
            "unique_text_variants": len(hashes),
            "exact_template_share": round(exact_share, 3),
            "personalised_share": round(1 - exact_share, 3),
            "representative_comment_id": rep["comment_id"],
            "representative_text": display_text(rep["text"])[:1500],
            "first_posted": dates[0].isoformat() if dates else None,
            "last_posted": dates[-1].isoformat() if dates else None,
            "peak_day": max(per_day.items(), key=lambda x: x[1]) if per_day else None,
            "organizations": orgs.most_common(10),
            "member_ids": [r["comment_id"] for r in members],
        })
    clusters.sort(key=lambda c: -c["size"])
    return ClusterResult(clusters=clusters, assignments=assignments, n_total=len(rows), n_clusterable=len(usable),
                         n_attachment_only=attachment_only, n_unique_texts=len(norm_of))


def _parse_date(value: str | None) -> datetime | None:
    """Parse the date formats seen in API JSON and bulk CSVs; always returns an aware UTC datetime."""
    if not value:
        return None
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%MZ", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
