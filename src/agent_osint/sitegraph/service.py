"""SiteGraph service: fingerprinting, attribution, sibling search."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from typing import Any

from ..core.domains import host_from_input, registrable_domain, root_domain
from ..core.evidence import Evidence
from ..core.http import FetchError, RobotsDisallowed, utcnow_iso
from ..core.privacy import WITHHELD, classify_name, safe_name
from ..core.results import ToolInputError, ok
from ..core.runtime import Runtime
from ..core.wayback import cdx_query
from .adstxt import fetch_ads_txt
from .ct import fetch_ct
from .extract import extract_page, normalise_org_name
from .imprint import parse_imprint
from .scoring import SIGNAL_KINDS, SharedSignal, combine
from .securitytxt import fetch_security_txt
from .sellers import SellersIndex
from .store import SignalStore

FINGERPRINT_MAX_AGE = 7 * 86400
LIVE = "live"
CT_PEER = "ct_peer"

LIMITATIONS = [
    "Shared identifiers indicate a shared account, agency or template, not necessarily common ownership.",
    "Sibling search only covers domains in this server's local index; absence of siblings is not evidence of none.",
    "Identifiers can be copied or planted; weigh the evidence, especially single-family links.",
]


class SiteGraph:
    def __init__(self, rt: Runtime):
        self.rt = rt
        data_dir = rt.settings.data_dir("sitegraph")
        self.store = SignalStore(data_dir / "sitegraph.sqlite")
        self.sellers = SellersIndex(data_dir / "sellers", rt.fetcher)
        self._sem = asyncio.Semaphore(4)

    # ------------------------------------------------------------------ fingerprint
    async def fingerprint(self, domain: str, *, include_ct: bool = True, refresh: bool = False,
                          max_seller_systems: int = 12) -> dict[str, Any]:
        host = host_from_input(domain)
        root = registrable_domain(host)
        if not root:
            raise ToolInputError(f"{domain!r} has no registrable domain")
        if not refresh:
            cached = self.store.load_fingerprint(host, FINGERPRINT_MAX_AGE)
            if cached and (cached.get("certificates") is not None or not include_ct):
                cached["from_index"] = True
                return cached

        fp: dict[str, Any] = {"domain": host, "root": root, "fetched_at": utcnow_iso(), "identifiers": {},
                              "errors": {}}
        evidence: list[Evidence] = []
        signals: list[tuple[str, str, str | None]] = []

        tasks = {
            "homepage": self._homepage(host),
            "ads_txt": fetch_ads_txt(self.rt.fetcher, root, "ads.txt"),
            "app_ads_txt": fetch_ads_txt(self.rt.fetcher, root, "app-ads.txt"),
            "security_txt": fetch_security_txt(self.rt.fetcher, host),
        }
        if include_ct:
            tasks["ct"] = fetch_ct(self.rt.fetcher, root, api_key=self.rt.settings.certspotter_api_key)
        names = list(tasks)
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        out = dict(zip(names, results))

        # -- homepage + imprint
        home = out["homepage"]
        if isinstance(home, Exception):
            fp["errors"]["homepage"] = str(home)
        else:
            page, res, imprint = home
            ev = self.rt.evidence.record(res, note="homepage HTML")
            evidence.append(ev)
            fp["homepage"] = {"url": res.url, "status": res.status, "title": page.title,
                              "site_name": page.site_name, "evidence_id": ev.id}
            for kind, values in page.ids.items():
                fp["identifiers"][kind] = sorted(values)
                for v in values:
                    signals.append((kind, v, ev.id))
                    if kind == "adsense":
                        signals.append(("google_pub", v, ev.id))
            if page.fb_domain_verification:
                fp["identifiers"]["fb_domain_verification"] = sorted(page.fb_domain_verification)
                signals += [("fb_domain_verification", v, ev.id) for v in page.fb_domain_verification]
            declared = {"jsonld_organizations": [], "copyright_holders": []}
            for org in page.jsonld_orgs[:5]:
                name = safe_name(org["name"])
                declared["jsonld_organizations"].append({**org, "name": name})
                if name != WITHHELD:
                    signals.append(("jsonld_org", normalise_org_name(org["name"]), ev.id))
            for holder in page.copyright_holders:
                declared["copyright_holders"].append(holder)
                signals.append(("copyright_holder", normalise_org_name(holder), ev.id))
            fp["self_declared"] = declared
            if imprint is not None:
                ires, facts = imprint
                iev = self.rt.evidence.record(ires, note="legal notice / imprint page")
                evidence.append(iev)
                fp["imprint"] = {"url": ires.url, "entities": facts.entities,
                                 "registration_numbers": facts.registration_numbers, "vat_ids": facts.vat_ids,
                                 "role_emails": facts.role_emails, "evidence_id": iev.id}
                if facts.withheld_person_entities:
                    fp["imprint"]["withheld_person_entities"] = facts.withheld_person_entities
                signals += [("imprint_entity", normalise_org_name(e), iev.id) for e in facts.entities]
                signals += [("imprint_regno", r["key"], iev.id) for r in facts.registration_numbers]
                signals += [("vat_id", v, iev.id) for v in facts.vat_ids]

        # -- ads.txt / app-ads.txt
        direct_pairs: dict[tuple[str, str], str] = {}
        for key in ("ads_txt", "app_ads_txt"):
            val = out[key]
            if isinstance(val, Exception):
                fp["errors"][key] = str(val)
                continue
            parsed, res, err = val
            if parsed is None:
                if key == "ads_txt":
                    fp["errors"][key] = err or "not found"
                continue
            ev = self.rt.evidence.record(res, note=key.replace("_", "-").replace("-txt", ".txt"))
            evidence.append(ev)
            summary = {
                "url": res.url, "evidence_id": ev.id, "records": len(parsed.records),
                "direct_records": len(parsed.direct), "reseller_records": len(parsed.records) - len(parsed.direct),
                "owner_domains": parsed.owner_domains(), "manager_domains": parsed.manager_domains(),
                "contacts": parsed.contacts(), "invalid_lines": parsed.invalid_lines,
            }
            fp[key] = summary
            for d in parsed.owner_domains():
                signals.append(("adstxt_ownerdomain", d, ev.id))
            for m in parsed.manager_domains():
                signals.append(("adstxt_managerdomain", m["domain"], ev.id))
            for d in parsed.contact_domains():
                if d != root:
                    signals.append(("adstxt_contact_domain", d, ev.id))
            for rec in parsed.direct:
                direct_pairs.setdefault((rec.ad_system, rec.account_id), ev.id)
                if rec.ad_system == "google.com" and rec.account_id.startswith("pub-"):
                    signals.append(("google_pub", rec.account_id, ev.id))
                else:
                    signals.append(("adstxt_direct", f"{rec.ad_system}|{rec.account_id}", ev.id))

        # -- sellers.json for DIRECT accounts
        sellers_out = []
        systems = sorted({s for s, _ in direct_pairs}, key=lambda s: (s != "google.com", s))[:max_seller_systems]
        if systems:
            errs = await asyncio.gather(*(self.sellers.ensure(s) for s in systems), return_exceptions=True)
            for system, err in zip(systems, errs):
                if isinstance(err, Exception) or err:
                    fp["errors"][f"sellers.json:{system}"] = str(err)
            for (system, account), _ev_id in direct_pairs.items():
                if system not in systems:
                    continue
                rec = self.sellers.lookup(system, account)
                if rec is None:
                    continue
                info = self.sellers.file_info(system) or {}
                name_class = classify_name(rec.name) if rec.name else "unknown"
                display = None if rec.is_confidential else safe_name(rec.name)
                sev = self.rt.evidence.record_derived(
                    rec.source_url, note=f"sellers.json entry for seller_id {account}",
                    excerpt=json.dumps({"seller_id": account, "seller_type": rec.seller_type,
                                        "name": display, "domain": rec.domain,
                                        "is_confidential": int(rec.is_confidential),
                                        "file_sha256": info.get("sha256")}),
                )
                evidence.append(sev)
                entry = {"ad_system": system, "seller_id": account, "seller_type": rec.seller_type,
                         "name": display, "domain": rec.domain, "confidential": rec.is_confidential,
                         "evidence_id": sev.id}
                sellers_out.append(entry)
                if rec.seller_type in ("PUBLISHER", "BOTH") and not rec.is_confidential:
                    if rec.name and name_class != "person":
                        signals.append(("seller_name", normalise_org_name(rec.name), sev.id))
                    if rec.domain and name_class != "person":
                        sd = registrable_domain(rec.domain.replace("https://", "").replace("http://", "").split("/")[0])
                        if sd:
                            signals.append(("seller_domain", sd, sev.id))
        fp["sellers"] = sellers_out

        # -- security.txt
        st_val = out["security_txt"]
        if isinstance(st_val, Exception):
            fp["errors"]["security_txt"] = str(st_val)
        else:
            st, res, err = st_val
            if st is None:
                fp["errors"]["security_txt"] = err or "not found"
            else:
                ev = self.rt.evidence.record(res, note="security.txt")
                evidence.append(ev)
                fp["security_txt"] = {"url": res.url, "contacts": st.contacts(),
                                      "canonical": st.fields.get("canonical", []),
                                      "policy": st.fields.get("policy", []), "signed": st.signed,
                                      "linking_domains": st.linking_domains(), "evidence_id": ev.id}
                for d in st.linking_domains():
                    if d != root:
                        signals.append(("sectxt_domain", d, ev.id))

        # -- certificate transparency
        peer_signals: list[tuple[str, str, str, str]] = []
        if include_ct:
            ct = out["ct"]
            if isinstance(ct, Exception):
                fp["errors"]["certificates"] = str(ct)
                fp["certificates"] = None
            elif ct.error and not ct.certs:
                fp["errors"]["certificates"] = ct.error
                fp["certificates"] = None
            else:
                ev = self.rt.evidence.record(ct.raw, note="Cert Spotter issuances", keep_body=False) if ct.raw else None
                if ev:
                    evidence.append(ev)
                co = ct.co_domains(root)
                fp["certificates"] = {
                    "certificates_seen": len(ct.certs), "shared_or_cdn_certificates": sum(c.shared for c in ct.certs),
                    "co_certified_domains": {d: len(ids) for d, ids in sorted(co.items(), key=lambda x: -len(x[1]))[:50]},
                    "evidence_id": ev.id if ev else None,
                }
                for cert in ct.certs:
                    if cert.shared:
                        continue
                    if len(cert.roots) > 1:
                        signals.append(("ct_cert", cert.cert_id, ev.id if ev else None))
                        for other in cert.roots:
                            if other != root:
                                peer_signals.append((other, "ct_cert", cert.cert_id, ev.id if ev else ""))
        # Persist
        signals = [(k, v, e) for k, v, e in signals if v]
        self.store.replace_live_signals(host, signals, LIVE)
        by_peer: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
        for other, kind, value, eid in peer_signals:
            by_peer[other].append((kind, value, eid or None))
        for other, sigs in by_peer.items():
            self.store.add_signals(other, sigs, CT_PEER)
        fp["evidence"] = [e.to_dict() for e in evidence]
        fp["signal_count"] = len(signals)
        self.store.save_fingerprint(host, root, fp)
        fp["from_index"] = False
        return fp

    async def _homepage(self, host: str):
        last_exc: Exception | None = None
        for url in (f"https://{host}/", f"http://{host}/"):
            try:
                res = await self.rt.fetcher.get(url, respect_robots=True, max_bytes=4 * 1024 * 1024,
                                                headers={"Accept": "text/html,application/xhtml+xml"})
            except RobotsDisallowed as exc:
                raise exc
            except FetchError as exc:
                last_exc = exc
                continue
            if res.status >= 400:
                last_exc = FetchError(f"homepage HTTP {res.status}", url=url, status=res.status)
                continue
            page = extract_page(res.text(), res.url)
            imprint = None
            base_root = registrable_domain(host)
            for link in page.imprint_links[:2]:
                link_host = link.split("/")[2] if "//" in link else ""
                if registrable_domain(link_host) != base_root:
                    continue
                try:
                    ires = await self.rt.fetcher.get(link, respect_robots=True, max_bytes=2 * 1024 * 1024)
                except FetchError:
                    continue
                if ires.status != 200:
                    continue
                facts = parse_imprint(ires.text())
                if not facts.is_empty():
                    imprint = (ires, facts)
                    break
            return page, res, imprint
        raise last_exc or FetchError("homepage unreachable", url=host)

    # ------------------------------------------------------------------ attribution
    async def attribute_operator(self, domain: str, *, refresh: bool = False) -> dict[str, Any]:
        fp = await self.fingerprint(domain, include_ct=False, refresh=refresh)
        root = fp["root"]
        cands: dict[str, dict[str, Any]] = {}

        def add(key: str, *, name: str | None = None, domain_: str | None = None, role: str, source: str,
                weight: float, value: str, evidence_id: str | None, identifiers: list[str] | None = None) -> None:
            c = cands.setdefault(key, {"name": None, "domains": [], "identifiers": [], "roles": set(),
                                       "sources": []})
            if name and not c["name"]:
                c["name"] = name
            if domain_ and domain_ not in c["domains"]:
                c["domains"].append(domain_)
            for i in identifiers or []:
                if i not in c["identifiers"]:
                    c["identifiers"].append(i)
            c["roles"].add(role)
            c["sources"].append({"source": source, "value": value, "weight": weight, "evidence_id": evidence_id})

        ads = fp.get("ads_txt") or {}
        for d in ads.get("owner_domains", []):
            add(f"domain:{d}", domain_=d, role="owner", source="ads.txt OWNERDOMAIN", weight=0.85, value=d,
                evidence_id=ads.get("evidence_id"))
        for m in ads.get("manager_domains", []):
            add(f"domain:{m['domain']}", domain_=m["domain"], role="monetisation_manager",
                source="ads.txt MANAGERDOMAIN", weight=0.3, value=m["domain"], evidence_id=ads.get("evidence_id"))
        for s in fp.get("sellers", []):
            if s.get("confidential") or s.get("name") in (None, WITHHELD):
                continue
            sd = registrable_domain((s.get("domain") or "").split("/")[0]) if s.get("domain") else None
            key = f"domain:{sd}" if sd else f"name:{normalise_org_name(s['name'])}"
            if s.get("seller_type") in ("PUBLISHER", "BOTH"):
                w = 0.9 if sd == root else 0.75
                add(key, name=s["name"], domain_=sd, role="owner" if sd != root else "operator",
                    source=f"sellers.json ({s['ad_system']}) PUBLISHER record for DIRECT account", weight=w,
                    value=f"{s['name']} / {s.get('domain')}", evidence_id=s.get("evidence_id"))
            else:
                add(key, name=s["name"], domain_=sd, role="monetisation_manager",
                    source=f"sellers.json ({s['ad_system']}) INTERMEDIARY record", weight=0.3,
                    value=f"{s['name']} / {s.get('domain')}", evidence_id=s.get("evidence_id"))
        imp = fp.get("imprint") or {}
        idents = [r["value"] for r in imp.get("registration_numbers", [])] + imp.get("vat_ids", [])
        for ent in imp.get("entities", []):
            add(f"name:{normalise_org_name(ent)}", name=ent, role="operator", source="legal notice / imprint",
                weight=0.9 if idents else 0.75, value=ent, evidence_id=imp.get("evidence_id"),
                identifiers=idents)
        declared = fp.get("self_declared") or {}
        for org in declared.get("jsonld_organizations", []):
            if org.get("name") in (None, WITHHELD):
                continue
            add(f"name:{normalise_org_name(org['name'])}", name=org["name"], role="operator",
                source="schema.org Organization markup", weight=0.55, value=org["name"],
                evidence_id=(fp.get("homepage") or {}).get("evidence_id"))
        for holder in declared.get("copyright_holders", []):
            add(f"name:{normalise_org_name(holder)}", name=holder, role="operator", source="copyright notice",
                weight=0.35, value=holder, evidence_id=(fp.get("homepage") or {}).get("evidence_id"))
        st = fp.get("security_txt") or {}
        for d in st.get("linking_domains", []):
            if d != root:
                add(f"domain:{d}", domain_=d, role="possible_parent", source="security.txt contact/canonical domain",
                    weight=0.4, value=d, evidence_id=st.get("evidence_id"))

        # Merge name-keyed and domain-keyed candidates that describe the same organisation.
        merged = self._merge_candidates(cands)
        ranked = []
        for c in merged:
            p_not = 1.0
            seen_sources = set()
            for s in c["sources"]:
                if s["source"] in seen_sources:
                    continue
                seen_sources.add(s["source"])
                p_not *= 1 - s["weight"]
            conf = round(1 - p_not, 3)
            ranked.append({
                "organization": c["name"], "domains": c["domains"], "identifiers": c["identifiers"],
                "roles": sorted(c["roles"]), "confidence": conf,
                "confidence_tier": "high" if conf >= 0.9 and len(seen_sources) >= 2 else
                ("medium" if conf >= 0.7 else "low"),
                "independent_sources": len(seen_sources), "evidence": c["sources"],
            })
        ranked.sort(key=lambda r: (-("operator" in r["roles"] or "owner" in r["roles"]), -r["confidence"]))
        warnings = []
        if not ranked:
            warnings.append("No operator signals found (no OWNERDOMAIN, sellers.json match, imprint, or schema.org "
                            "organisation). This is common for small sites and is not evidence of concealment.")
        return ok({"domain": fp["domain"], "root": root, "candidates": ranked[:10],
                   "fingerprint_errors": fp.get("errors", {})},
                  evidence=fp.get("evidence", []), warnings=warnings,
                  limitations=LIMITATIONS[:1] + ["Candidate roles: 'operator'/'owner' come from declarations; "
                                                 "'monetisation_manager' is an ad partner, not an owner."],
                  sources_checked=["homepage", "legal notice", "ads.txt", "app-ads.txt", "sellers.json",
                                   "security.txt", "schema.org markup"])

    @staticmethod
    def _merge_candidates(cands: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        items = list(cands.values())
        merged: list[dict[str, Any]] = []
        for c in items:
            target = None
            for m in merged:
                same_domain = set(c["domains"]) & set(m["domains"])
                same_name = (c["name"] and m["name"] and normalise_org_name(c["name"]) == normalise_org_name(m["name"]))
                if same_domain or same_name:
                    target = m
                    break
            if target is None:
                merged.append({**c, "roles": set(c["roles"]), "sources": list(c["sources"]),
                               "domains": list(c["domains"]), "identifiers": list(c["identifiers"])})
            else:
                target["name"] = target["name"] or c["name"]
                target["domains"] += [d for d in c["domains"] if d not in target["domains"]]
                target["identifiers"] += [i for i in c["identifiers"] if i not in target["identifiers"]]
                target["roles"] |= c["roles"]
                target["sources"] += c["sources"]
        return merged

    # ------------------------------------------------------------------ siblings
    _ID_QUERY = [
        ("ga_ua", re.compile(r"^(UA-\d{4,10})(?:-\d{1,4})?$", re.I)),
        ("ga4", re.compile(r"^(G-[A-Z0-9]{8,12})$", re.I)),
        ("gtm", re.compile(r"^(GTM-[A-Z0-9]{4,9})$", re.I)),
        ("google_pub", re.compile(r"^(?:ca-)?(pub-\d{16})$", re.I)),
        ("gads", re.compile(r"^(AW-\d{9,11})$", re.I)),
    ]

    def _query_signals(self, query: str) -> tuple[str, list[tuple[str, str]]]:
        q = query.strip()
        for kind, pat in self._ID_QUERY:
            m = pat.match(q)
            if m:
                val = m.group(1)
                val = val if kind == "google_pub" else val.upper()
                if kind == "google_pub":
                    return "identifier", [("google_pub", val.lower()), ("adsense", val.lower())]
                return "identifier", [(kind, val)]
        if "," in q:  # "adsystem.com, 12345"
            parts = [p.strip() for p in q.split(",")]
            if len(parts) >= 2:
                return "identifier", [("adstxt_direct", f"{parts[0].lower()}|{parts[1]}")]
        rows = self.store.find_value(q)
        if rows:
            return "identifier", sorted({(r["kind"], r["value"]) for r in rows})
        return "domain", []

    async def find_siblings(self, query: str, *, limit: int = 25, fingerprint_if_missing: bool = True,
                            min_score: float = 0.0) -> dict[str, Any]:
        mode, qsigs = self._query_signals(query)
        origin = None
        if mode == "domain":
            origin = host_from_input(query)
            if not self.store.signals_for(origin) and fingerprint_if_missing:
                await self.fingerprint(origin)
            qsigs = [(s["kind"], s["value"]) for s in self.store.signals_for(origin)]
            if not qsigs:
                return ok({"query": query, "mode": mode, "siblings": []},
                          warnings=["The domain yielded no linkable identifiers."], limitations=LIMITATIONS)
        per_domain: dict[str, list[SharedSignal]] = defaultdict(list)
        ev_ids: dict[str, list[str]] = defaultdict(list)
        for kind, value in qsigs:
            if kind not in SIGNAL_KINDS:
                continue
            count = self.store.value_count(kind, value)
            for row in self.store.domains_with(kind, value):
                d = row["domain"]
                if d == origin:
                    continue
                if origin and registrable_domain(d) == registrable_domain(origin):
                    continue
                per_domain[d].append(SharedSignal(kind, value, count))
                if row["evidence_id"]:
                    ev_ids[d].append(row["evidence_id"])
        siblings = []
        for d, shared in per_domain.items():
            uniq = {(s.kind, s.value): s for s in shared}
            verdict = combine(list(uniq.values()))
            if verdict["score"] < min_score:
                continue
            siblings.append({
                "domain": d, **verdict,
                "shared": [{"kind": s.kind, "value": s.value, "meaning": SIGNAL_KINDS[s.kind]["desc"],
                            "domains_with_value": s.n_domains_with_value} for s in uniq.values()],
                "evidence_ids": sorted(set(ev_ids[d]))[:10],
            })
        siblings.sort(key=lambda s: (-s["score"], s["domain"]))
        stats = self.store.stats()
        return ok({"query": query, "mode": mode, "query_signals": [{"kind": k, "value": v} for k, v in qsigs][:50],
                   "siblings": siblings[:limit], "total_matches": len(siblings),
                   "index_size_domains": stats["domains_indexed"]},
                  limitations=LIMITATIONS)

    # ------------------------------------------------------------------ comparisons
    async def compare_sites(self, domains: list[str], *, refresh: bool = False) -> dict[str, Any]:
        if not 2 <= len(domains) <= 10:
            raise ToolInputError("compare_sites takes between 2 and 10 domains")
        hosts = list(dict.fromkeys(host_from_input(d) for d in domains))

        async def fp(h: str) -> dict[str, Any]:
            async with self._sem:
                return await self.fingerprint(h, refresh=refresh)

        fps = await asyncio.gather(*(fp(h) for h in hosts), return_exceptions=True)
        errors = {h: str(f) for h, f in zip(hosts, fps) if isinstance(f, Exception)}
        good = [h for h, f in zip(hosts, fps) if not isinstance(f, Exception)]
        pairs = []
        for i, a in enumerate(good):
            for b in good[i + 1:]:
                pairs.append(self._pair(a, b))
        pairs.sort(key=lambda p: -p["score"])
        return ok({"domains": good, "pairs": pairs, "errors": errors}, limitations=LIMITATIONS)

    def _pair(self, a: str, b: str) -> dict[str, Any]:
        sa = {(s["kind"], s["value"]): s for s in self.store.signals_for(a)}
        sb = {(s["kind"], s["value"]): s for s in self.store.signals_for(b)}
        shared_keys = [k for k in sa if k in sb and k[0] in SIGNAL_KINDS]
        shared = [SharedSignal(k[0], k[1], self.store.value_count(*k)) for k in shared_keys]
        verdict = combine(shared)
        return {"a": a, "b": b, **verdict,
                "shared": [{"kind": k[0], "value": k[1], "meaning": SIGNAL_KINDS[k[0]]["desc"],
                            "evidence_a": sa[k]["evidence_id"], "evidence_b": sb[k]["evidence_id"]}
                           for k in shared_keys]}

    async def explain_link(self, a: str, b: str, *, refresh: bool = False) -> dict[str, Any]:
        ha, hb = host_from_input(a), host_from_input(b)
        for h in (ha, hb):
            if refresh or not self.store.signals_for(h):
                await self.fingerprint(h, refresh=refresh)
        pair = self._pair(ha, hb)
        sentences = []
        for s in pair["shared"]:
            sentences.append(f"Both {ha} and {hb} show {s['kind']} = {s['value']} ({s['meaning']}); "
                             f"evidence {s['evidence_a']} / {s['evidence_b']}.")
        if not sentences:
            sentences.append(f"No shared identifiers between {ha} and {hb} in the index.")
        evidence = []
        for s in pair["shared"]:
            for eid in (s["evidence_a"], s["evidence_b"]):
                if eid:
                    rec = self.rt.evidence.get(eid, include_body=False)
                    if rec:
                        evidence.append(rec)
        return ok({**pair, "explanation": sentences}, evidence=evidence, limitations=LIMITATIONS)

    async def index_domains(self, domains: list[str], *, include_ct: bool = False) -> dict[str, Any]:
        if len(domains) > 25:
            raise ToolInputError("index_domains accepts at most 25 domains per call",
                                 hint="Use the `agent-osint-sitegraph index` CLI for bulk lists.")

        async def one(d: str) -> dict[str, Any]:
            async with self._sem:
                try:
                    fp = await self.fingerprint(d, include_ct=include_ct, refresh=True)
                    return {"domain": fp["domain"], "signals": fp.get("signal_count", 0),
                            "errors": list(fp.get("errors", {}))}
                except Exception as exc:  # noqa: BLE001
                    return {"domain": d, "error": str(exc)}

        results = await asyncio.gather(*(one(d) for d in domains))
        return ok({"indexed": results, "index": self.store.stats()})

    async def site_history(self, domain: str) -> dict[str, Any]:
        root = root_domain(domain)
        first, res1 = await cdx_query(self.rt.fetcher, root, limit=1)
        last, res2 = await cdx_query(self.rt.fetcher, root, limit=1, reverse=True)
        yearly, res3 = await cdx_query(self.rt.fetcher, root, limit=100, collapse="timestamp:4")
        evidence = [self.rt.evidence.record(r, note="Wayback CDX query", keep_body=True) for r in (res1, res2, res3)]
        data = {
            "domain": root,
            "first_capture": {"at": first[0].iso, "url": first[0].replay_url} if first else None,
            "latest_capture": {"at": last[0].iso, "url": last[0].replay_url} if last else None,
            "years_with_captures": sorted({c.timestamp[:4] for c in yearly}),
        }
        warnings = [] if first else ["No successful (HTTP 200) captures in the Wayback Machine."]
        return ok(data, evidence=evidence, warnings=warnings,
                  limitations=["Archive coverage is incomplete; first capture is an upper bound on site age."])
