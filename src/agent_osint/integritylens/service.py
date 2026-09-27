"""IntegrityLens service."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from rapidfuzz import fuzz

from ..core.domains import host_from_input, registrable_domain
from ..core.evidence import Evidence
from ..core.http import FetchError
from ..core.results import MissingCredential, ToolInputError, ok
from ..core.runtime import Runtime
from . import tortured
from .localdata import ISSN_RE, LocalData, _norm_issn
from .sources import SEVERITY, Signal, Sources, extract_dois, normalise_doi

LIMITATIONS = [
    "'No signals' means none were found in the sources checked, not that the work is sound.",
    "Signals are attributed to their sources; they are not findings of misconduct by any person.",
]


class IntegrityLens:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.src = Sources(rt.fetcher, pubpeer_devkey=rt.settings.pubpeer_devkey, contact=rt.settings.contact_email)
        self.local = LocalData(rt.settings.data_dir("integritylens") / "local.sqlite")
        self._sem = asyncio.Semaphore(4)

    def sources_status(self) -> dict[str, Any]:
        return ok({
            "crossref": "enabled (no key needed)" + ("" if self.rt.settings.contact_email else
                                                     "; set AGENT_OSINT_CONTACT to use Crossref/OpenAlex polite pools"),
            "openalex": "enabled (no key needed)",
            "doi_handle_check": "enabled",
            "pubpeer": "enabled" if self.src.pubpeer_devkey else "disabled: set PUBPEER_DEVKEY",
            "tortured_phrases": f"{len(tortured.phrase_table())} phrases ({tortured.SOURCE})",
            "local_datasets": self.local.status(),
        })

    def load_dataset(self, kind: str, path: str) -> dict[str, Any]:
        return ok(self.local.load(kind, str(self.rt.settings.check_file(path, (".csv", ".tsv", ".txt")))))

    async def _report(self, doi: str, *, pubpeer: dict | None, use_openalex: bool = True) -> tuple[dict, list[Evidence], list[str]]:
        signals: list[Signal] = []
        evidence: list[Evidence] = []
        checked: list[str] = []
        warnings: list[str] = []
        async with self._sem:
            cr = await self.src.crossref(doi)
        checked.append("crossref")
        if cr.raw is not None:
            ev = self.rt.evidence.record(cr.raw, note=f"Crossref works record for {doi}")
            evidence.append(ev)
            for s in cr.signals:
                s.evidence_id = ev.id
        signals += cr.signals
        registered = None
        if not cr.found:
            registered, res = await self.src.doi_registered(doi)
            checked.append("doi.org handle")
            if res is not None:
                evidence.append(self.rt.evidence.record(res, note=f"doi.org handle lookup for {doi}"))
            if registered is False:
                signals.append(Signal(kind="doi_not_registered", source="doi.org", severity=2,
                                      detail="DOI does not resolve at doi.org; the citation may be fabricated or mistyped",
                                      evidence_id=evidence[-1].id if evidence else None))
        venue = None
        if use_openalex and registered is not False:
            try:
                work, res = await self.src.openalex_work(doi)
                checked.append("openalex")
                if work is not None and res is not None:
                    ev = self.rt.evidence.record(res, note=f"OpenAlex work for {doi}", keep_body=False)
                    evidence.append(ev)
                    if work.get("is_retracted") and not any(s.kind == "retraction" for s in signals):
                        signals.append(Signal(kind="retraction", source="openalex:is_retracted", severity=3,
                                              evidence_id=ev.id))
                    loc = (work.get("primary_location") or {}).get("source") or {}
                    venue = {"name": loc.get("display_name"), "issn_l": loc.get("issn_l"),
                             "in_doaj": loc.get("is_in_doaj"), "publisher": loc.get("host_organization_name")}
            except FetchError as exc:
                warnings.append(f"OpenAlex unavailable: {exc}")
        for r in self.local.rw_for(doi):
            nature = (r.get("nature") or "").lower()
            kind = ("retraction" if "retract" in nature else "expression_of_concern" if "concern" in nature
                    else "correction" if "correct" in nature else "retraction_watch_record")
            if not any(s.kind == kind and s.source.startswith("crossref:updated-by:retraction-watch") for s in signals):
                signals.append(Signal(kind=kind, source="retraction_watch_local", date=(r.get("date") or "")[:10] or None,
                                      detail=(r.get("reason") or "")[:300] or None,
                                      url=f"https://doi.org/{r['notice_doi']}" if r.get("notice_doi") else None,
                                      severity=SEVERITY.get(kind, 2)))
        if self.local.has("retraction_watch"):
            checked.append("retraction_watch_local")
        for flags in self.local.pps_for(doi):
            signals.append(Signal(kind="problematic_paper_screener_flag", source="pps_local", severity=2,
                                  detail=", ".join(f"{k}: {v}" for k, v in list(flags.items())[:6])[:300]))
        if self.local.has("problematic_paper_screener"):
            checked.append("problematic_paper_screener_local")
        if pubpeer is not None:
            checked.append("pubpeer")
            fb = pubpeer.get(doi)
            if fb and (fb.get("total_comments") or 0) > 0:
                signals.append(Signal(kind="pubpeer_discussion", source="pubpeer", severity=1,
                                      date=(fb.get("last_commented_at") or "")[:10] or None, url=fb.get("url"),
                                      detail=f"{fb.get('total_comments')} comment(s) on PubPeer (content not assessed)"))
        signals.sort(key=lambda s: -s.severity)
        top = max((s.severity for s in signals), default=0)
        status = {3: "retracted_or_removed", 2: "serious_concern_signal", 1: "minor_or_discussion_signal"}.get(
            top, "no_signals_in_checked_sources")
        data = {
            "doi": doi, "status": status,
            "title": cr.title, "journal": cr.journal, "publisher": cr.publisher, "year": cr.year,
            "crossref_registered": cr.found, "venue": venue,
            "signals": [s.to_dict() for s in signals], "sources_checked": checked,
        }
        return data, evidence, warnings

    async def integrity_report(self, doi: str) -> dict[str, Any]:
        d = normalise_doi(doi)
        pp = None
        warnings: list[str] = []
        if self.src.pubpeer_devkey:
            try:
                pp, results = await self.src.pubpeer([d])
            except (FetchError, MissingCredential) as exc:
                warnings.append(f"PubPeer unavailable: {exc}")
        else:
            warnings.append("PubPeer not checked (PUBPEER_DEVKEY not set).")
        data, evidence, w2 = await self._report(d, pubpeer=pp)
        return ok(data, evidence=evidence, warnings=warnings + w2, limitations=LIMITATIONS,
                  sources_checked=data["sources_checked"])

    async def screen_references(self, text: str | None = None, dois: list[str] | None = None,
                                *, max_items: int = 100) -> dict[str, Any]:
        found = extract_dois(text or "")
        for d in dois or []:
            try:
                nd = normalise_doi(d)
            except ValueError:
                continue
            if nd not in found:
                found.append(nd)
        if not found:
            raise ToolInputError("no DOIs found", hint="Pass a reference list containing DOIs, or a list of DOIs.")
        truncated = len(found) > max_items
        found = found[:max_items]
        warnings = [f"Only the first {max_items} DOIs were checked."] if truncated else []
        pp = None
        if self.src.pubpeer_devkey:
            try:
                pp, _ = await self.src.pubpeer(found)
            except (FetchError, MissingCredential) as exc:
                warnings.append(f"PubPeer unavailable: {exc}")
        else:
            warnings.append("PubPeer not checked (PUBPEER_DEVKEY not set).")

        async def one(d: str) -> dict[str, Any]:
            try:
                data, _ev, _w = await self._report(d, pubpeer=pp, use_openalex=False)
                return data
            except FetchError as exc:
                return {"doi": d, "status": "check_failed", "error": str(exc)}

        results = await asyncio.gather(*(one(d) for d in found))
        order = {"retracted_or_removed": 0, "serious_concern_signal": 1, "check_failed": 2,
                 "minor_or_discussion_signal": 3, "no_signals_in_checked_sources": 4}
        results.sort(key=lambda r: order.get(r["status"], 5))
        counts: dict[str, int] = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        slim = [{k: r.get(k) for k in ("doi", "status", "title", "journal", "year", "signals", "error") if r.get(k)}
                for r in results]
        return ok({"checked": len(results), "summary": counts, "results": slim}, warnings=warnings,
                  limitations=LIMITATIONS)

    async def journal_legitimacy(self, query: str) -> dict[str, Any]:
        q = query.strip()
        if not q:
            raise ToolInputError("query required (ISSN, journal website URL, or journal title)")
        issn = None
        domain = None
        m = ISSN_RE.search(q)
        if m and len(q) <= 12:
            issn = _norm_issn(m.group(1))
        elif "." in q and " " not in q:
            domain = registrable_domain(host_from_input(q))
        title = None if (issn or domain) else q
        evidence: list[Evidence] = []
        findings: list[dict[str, Any]] = []
        warnings: list[str] = []
        hij = self.local.hijacked_match(issn=issn, domain=domain, title=title)
        if not self.local.has("hijacked_journals"):
            warnings.append("Hijacked-journal list not loaded (load_dataset kind='hijacked_journals').")
        for h in hij:
            sev = "high" if "hijacked_domain" in h["matched_on"] else "info"
            findings.append({"signal": "hijacked_journal_list_match", "severity": sev, **h,
                             "note": ("This website is listed as a hijacked (clone) site." if sev == "high" else
                                      "This journal has been targeted by hijackers; verify you are on the authentic site.")})
        source = None
        try:
            if issn:
                source, res = await self.src.openalex_source(issn)
            else:
                results, res = await self.src.openalex_source_search(title or domain or q)
                if results:
                    best = max(results, key=lambda s: fuzz.token_set_ratio((title or q).lower(),
                                                                          (s.get("display_name") or "").lower()))
                    score = fuzz.token_set_ratio((title or q).lower(), (best.get("display_name") or "").lower())
                    source = best if (domain or score >= 85) else None
                    if domain and source is not None:
                        home = registrable_domain(host_from_input(source.get("homepage_url") or "x.invalid")) \
                            if source.get("homepage_url") else None
                        if home != domain:
                            source = None
            if res is not None:
                evidence.append(self.rt.evidence.record(res, note="OpenAlex source lookup", keep_body=False))
        except FetchError as exc:
            warnings.append(f"OpenAlex unavailable: {exc}")
        venue = None
        if source:
            home = source.get("homepage_url")
            venue = {"name": source.get("display_name"), "issn_l": source.get("issn_l"), "issns": source.get("issn"),
                     "publisher": source.get("host_organization_name"), "homepage": home,
                     "in_doaj": source.get("is_in_doaj"), "works_count": source.get("works_count"),
                     "openalex_id": source.get("id")}
            if domain and home:
                home_dom = registrable_domain(host_from_input(home))
                if home_dom and home_dom != domain:
                    findings.append({"signal": "website_mismatch", "severity": "medium",
                                     "note": f"OpenAlex lists the journal homepage at {home_dom}, not {domain}."})
            if venue["name"]:
                n = self.local.rw_journal_count(venue["name"])
                if n:
                    findings.append({"signal": "retraction_watch_records", "severity": "info", "count": n,
                                     "note": f"{n} Retraction Watch record(s) for this journal (local dataset)."})
        elif not hij:
            warnings.append("Journal not found in OpenAlex; unknown journals are not necessarily illegitimate.")
        return ok({"query": q, "interpreted_as": {"issn": issn, "domain": domain, "title": title}, "venue": venue,
                   "findings": findings}, evidence=evidence, warnings=warnings,
                  limitations=["DOAJ membership and OpenAlex indexing are positive signals, not guarantees; "
                               "absence is not proof of a predatory journal."])

    def scan_tortured_phrases(self, text: str) -> dict[str, Any]:
        if len(text.strip()) < 20:
            raise ToolInputError("text too short")
        res = tortured.scan(text[:2_000_000])
        warnings = []
        if res["distinct_phrases"] == 1:
            warnings.append("A single tortured phrase can be coincidental; paper-mill texts usually contain several.")
        return ok(res, warnings=warnings,
                  limitations=[f"Seed list: {tortured.SOURCE}. Extend via AGENT_OSINT_TORTURED_PHRASES."])


def looks_like_issn(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-?\d{3}[\dXx]", value.strip()))
