"""How much does a shared identifier say about two sites having the same operator?

Weights are per signal *kind*; kinds are grouped into *families* of correlated
signals (e.g. a GTM container usually loads the GA4 tag, so they are not
independent evidence). The score is a noisy-OR over families, using the
strongest signal in each family. A pair is never called more than "possible"
on a single family.

Weights are expert priors, documented here so users can audit and tune them;
they are not learned from labelled data.
"""

from __future__ import annotations

from dataclasses import dataclass

SIGNAL_KINDS: dict[str, dict[str, object]] = {
    # kind: weight, family, description
    "imprint_regno": {"w": 0.95, "family": "legal_identity", "desc": "same company registration / LEI number in legal notice"},
    "vat_id": {"w": 0.95, "family": "legal_identity", "desc": "same VAT ID in legal notice"},
    "imprint_entity": {"w": 0.70, "family": "legal_identity", "desc": "same legal entity named in legal notice"},
    "adstxt_ownerdomain": {"w": 0.85, "family": "declared_owner", "desc": "ads.txt OWNERDOMAIN declares the same owner"},
    "seller_domain": {"w": 0.75, "family": "declared_owner", "desc": "sellers.json PUBLISHER record with the same business domain"},
    "seller_name": {"w": 0.75, "family": "declared_owner", "desc": "sellers.json PUBLISHER record with the same seller name"},
    "google_pub": {"w": 0.80, "family": "google_publisher", "desc": "same Google publisher account (AdSense pub- ID / google.com DIRECT)"},
    "adstxt_direct": {"w": 0.75, "family": "ad_accounts", "desc": "same DIRECT seller account at an ad system"},
    "ct_cert": {"w": 0.70, "family": "certificate", "desc": "covered by the same (non-shared) TLS certificate"},
    "fb_domain_verification": {"w": 0.70, "family": "meta_business", "desc": "same Meta Business domain-verification token"},
    "fb_pixel": {"w": 0.50, "family": "meta_business", "desc": "same Meta Pixel"},
    "ga_ua": {"w": 0.60, "family": "google_analytics", "desc": "same Universal Analytics account"},
    "gtm": {"w": 0.55, "family": "google_analytics", "desc": "same Google Tag Manager container"},
    "ga4": {"w": 0.55, "family": "google_analytics", "desc": "same GA4 measurement ID"},
    "gads": {"w": 0.50, "family": "google_analytics", "desc": "same Google Ads conversion ID"},
    "yandex_metrica": {"w": 0.45, "family": "other_trackers", "desc": "same Yandex Metrica counter"},
    "hotjar": {"w": 0.45, "family": "other_trackers", "desc": "same Hotjar site ID"},
    "clarity": {"w": 0.45, "family": "other_trackers", "desc": "same Microsoft Clarity project"},
    "tiktok_pixel": {"w": 0.45, "family": "other_trackers", "desc": "same TikTok pixel"},
    "linkedin_insight": {"w": 0.45, "family": "other_trackers", "desc": "same LinkedIn Insight partner ID"},
    "jsonld_org": {"w": 0.50, "family": "self_declared", "desc": "same organisation declared in schema.org markup"},
    "copyright_holder": {"w": 0.35, "family": "self_declared", "desc": "same copyright holder in footer"},
    "sectxt_domain": {"w": 0.50, "family": "security_contact", "desc": "security.txt points to the same (non-platform) domain"},
    "adstxt_contact_domain": {"w": 0.40, "family": "security_contact", "desc": "ads.txt CONTACT at the same domain"},
    "adstxt_managerdomain": {"w": 0.15, "family": "manager", "desc": "same monetisation manager (managers serve many unrelated sites)"},
}

COMMON_VALUE_THRESHOLD = 150  # a value shared by more domains than this is treated as a platform/template value
COMMON_DAMPING = 0.2


@dataclass
class SharedSignal:
    kind: str
    value: str
    n_domains_with_value: int

    @property
    def weight(self) -> float:
        base = float(SIGNAL_KINDS[self.kind]["w"])  # type: ignore[arg-type]
        return base * COMMON_DAMPING if self.n_domains_with_value > COMMON_VALUE_THRESHOLD else base

    @property
    def family(self) -> str:
        return str(SIGNAL_KINDS[self.kind]["family"])


def combine(shared: list[SharedSignal]) -> dict[str, object]:
    fam_best: dict[str, float] = {}
    for s in shared:
        fam_best[s.family] = max(fam_best.get(s.family, 0.0), s.weight)
    p_not = 1.0
    for w in fam_best.values():
        p_not *= 1.0 - w
    score = round(1.0 - p_not, 3)
    strong_families = [f for f, w in fam_best.items() if w >= 0.4]
    if len(strong_families) >= 2 and score >= 0.9:
        tier = "strong"
    elif len(strong_families) >= 2 and score >= 0.75:
        tier = "likely"
    elif score > 0:
        tier = "possible"
    else:
        tier = "none"
    notes = []
    if len(strong_families) < 2 and score > 0:
        notes.append("Only one independent signal family: not sufficient to conclude a shared operator.")
    if any(s.n_domains_with_value > COMMON_VALUE_THRESHOLD for s in shared):
        notes.append("Some shared values are very common (template, platform or agency) and were down-weighted.")
    return {"score": score, "tier": tier, "families": sorted(fam_best), "notes": notes}
