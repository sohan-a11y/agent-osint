"""Tortured-phrase detector.

Seed list: every pair in Table 1 of Cabanac, Labbé & Magazinov (2021), "Tortured phrases: A dubious
writing style emerging in science. Evidence of critical issues affecting established journals"
(arXiv:2107.06751), with slash-separated alternatives expanded. Extend with your own list via
``AGENT_OSINT_TORTURED_PHRASES`` (a text file of ``tortured phrase => expected phrase`` lines), e.g. from
the Problematic Paper Screener's published fingerprints.
"""

from __future__ import annotations

import itertools
import os
import re
from functools import lru_cache
from pathlib import Path

# Table 1 as printed (alternatives separated by '/').
_TABLE1: list[tuple[str, str]] = [
    ("profound neural organization", "deep neural network"),
    ("fake/counterfeit neural organization", "artificial neural network"),
    ("versatile organization", "mobile network"),
    ("organization ambush/assault", "network attack"),
    ("organization association", "network connection"),
    ("enormous/huge/immense/colossal information", "big data"),
    # Table 1 prints "information stockroom/distribution center"; the two alternatives are split here
    # because the slot-wise expansion below works on single words.
    ("information stockroom", "data warehouse"),
    ("information distribution center", "data warehouse"),
    ("counterfeit/human-made consciousness", "artificial intelligence (AI)"),
    ("elite figuring", "high performance computing"),
    ("haze figuring", "fog/mist/cloud computing"),
    ("designs preparing unit", "graphics processing unit (GPU)"),
    ("focal preparing unit", "central processing unit (CPU)"),
    ("work process motor", "workflow engine"),
    ("facial acknowledgement", "face recognition"),
    ("discourse acknowledgement", "voice recognition"),
    ("mean square mistake/blunder", "mean square error"),
    ("mean outright/supreme mistake/blunder", "mean absolute error"),
    ("motion/flag/indicator/sign/signal to clamor/commotion/noise", "signal to noise"),
    ("worldwide parameters", "global parameters"),
    ("arbitrary/irregular get right of passage to", "random access"),
    ("arbitrary/irregular backwoods/timberland", "random forest"),  # printed: backwoods/timberland/lush territory
    ("arbitrary/irregular lush territory", "random forest"),
    ("arbitrary/irregular esteem", "random value"),
    ("subterranean insect state/province/area/region/settlement", "ant colony"),
    ("underground creepy crawly state/province/area/region/settlement", "ant colony"),
    ("leftover vitality", "remaining energy"),
    ("territorial normal vitality", "local average energy"),
    ("motor vitality", "kinetic energy"),
    ("credulous/innocent/gullible Bayes", "naïve Bayes"),
    ("individual computerized collaborator", "personal digital assistant (PDA)"),
]
SOURCE = "Cabanac, Labbé & Magazinov 2021, arXiv:2107.06751, Table 1"


def _expand(pattern: str) -> list[str]:
    """Expand 'a/b c/d e' into all word combinations. Multi-word alternatives are handled
    by treating each whitespace-separated slot independently, which matches the table's usage."""
    slots = [tok.split("/") for tok in pattern.split(" ")]
    return [" ".join(combo) for combo in itertools.product(*slots)]


@lru_cache(maxsize=1)
def phrase_table() -> list[tuple[str, str, str]]:
    table: list[tuple[str, str, str]] = []
    for tortured, expected in _TABLE1:
        for variant in _expand(tortured):
            # "signal to noise" itself is the correct phrase; never flag it.
            if variant.lower() == expected.split(" (")[0].lower():
                continue
            table.append((variant.lower(), expected, SOURCE))
    extra = os.environ.get("AGENT_OSINT_TORTURED_PHRASES")
    if extra and Path(extra).is_file():
        for line in Path(extra).read_text(encoding="utf-8").splitlines():
            if "=>" in line and not line.strip().startswith("#"):
                t, e = line.split("=>", 1)
                table.append((t.strip().lower(), e.strip(), f"user list {Path(extra).name}"))
    return table


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[re.Pattern[str], str, str, str]]:
    out = []
    for tortured, expected, src in phrase_table():
        words = [re.escape(w) for w in tortured.split()]
        out.append((re.compile(r"\b" + r"[\s\-]+".join(words) + r"\b", re.IGNORECASE), tortured, expected, src))
    return out


def scan(text: str) -> dict:
    hits = []
    for pattern, tortured, expected, src in _compiled():
        for m in pattern.finditer(text):
            hits.append({"phrase": m.group(0), "tortured_form": tortured, "expected": expected,
                         "offset": m.start(), "source": src,
                         "context": " ".join(text[max(0, m.start() - 60): m.end() + 60].split())})
    hits.sort(key=lambda h: h["offset"])
    n_words = max(1, len(re.findall(r"\w+", text)))
    distinct = len({h["tortured_form"] for h in hits})
    return {"matches": hits, "distinct_phrases": distinct, "words": n_words,
            "per_10k_words": round(len(hits) * 10000 / n_words, 2), "phrases_checked": len(phrase_table())}
