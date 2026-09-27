"""IntegrityLens tests. Crossref responses are real captures (see tests/fixtures/crossref, from citeguard, MIT)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from agent_osint.integritylens.service import IntegrityLens
from agent_osint.integritylens.sources import analyse_crossref_message, extract_dois, normalise_doi
from tests.conftest import FIXTURES, dumps

WAKEFIELD = "10.1016/S0140-6736(97)11096-0"
SURGI = "10.1016/S0140-6736(20)31180-6"
WATSON = "10.1038/171737a0"
FAKE = "10.9999/fabricated.2024.001"


def _cr(name: str) -> dict:
    return json.loads((FIXTURES / "crossref" / name).read_text())


def cr_url(doi: str) -> str:
    return f"https://api.crossref.org/works/{quote(doi.lower(), safe='')}"


def test_doi_extraction_handles_parentheses_and_trailing_punctuation():
    text = (f"[1] Wakefield AJ. Lancet 1998. doi:{WAKEFIELD}. [2] https://doi.org/{WATSON}, "
            f"[3] ({SURGI}) [4] {{https://doi.org/10.1000/xyz123}}")
    assert extract_dois(text) == [WAKEFIELD.lower(), WATSON, SURGI.lower(), "10.1000/xyz123"]
    assert normalise_doi(f"https://dx.doi.org/{WATSON}") == WATSON
    with pytest.raises(ValueError):
        normalise_doi("not-a-doi")


def test_crossref_analysis_on_real_records():
    wake = analyse_crossref_message(WAKEFIELD, _cr("wakefield_1998.json")["message"])
    kinds = {(s.kind, s.source) for s in wake.signals}
    assert ("retraction", "crossref:updated-by:retraction-watch") in kinds
    assert ("correction", "crossref:updated-by:retraction-watch") in kinds
    assert next(s for s in wake.signals if s.kind == "retraction").date == "2010-02-06"
    surgi = analyse_crossref_message(SURGI, _cr("surgisphere_mehra_2020.json")["message"])
    assert any(s.kind == "retraction" and s.source == "crossref:update-to:publisher" for s in surgi.signals)
    clean = analyse_crossref_message(WATSON, _cr("watson_crick_1953_clean.json")["message"])
    assert clean.signals == [] and clean.journal == "Nature"


@pytest.fixture
def lens(replay, tmp_path):
    replay.add(cr_url(WAKEFIELD), _cr("wakefield_1998.json"))
    replay.add(cr_url(SURGI), _cr("surgisphere_mehra_2020.json"))
    replay.add(cr_url(WATSON), _cr("watson_crick_1953_clean.json"))
    replay.add(cr_url(FAKE), "Resource not found.", status=404)
    replay.add(f"https://doi.org/api/handles/{FAKE}", {"responseCode": 100, "handle": FAKE}, status=404)
    replay.add(f"https://api.openalex.org/works/doi:{WATSON}", {
        "id": "https://openalex.org/W2016370014", "is_retracted": False,
        "primary_location": {"source": {"display_name": "Nature", "issn_l": "0028-0836", "is_in_doaj": False,
                                        "host_organization_name": "Nature Portfolio"}}})
    replay.add(f"https://api.openalex.org/works/doi:{WAKEFIELD.lower()}", {"is_retracted": True, "primary_location": {}})
    replay.add(f"https://api.openalex.org/works/doi:{SURGI.lower()}", {"is_retracted": True})
    replay.add("https://pubpeer.com/v3/publications?devkey=KEY", method="POST",
               json_body={"dois": [SURGI.lower()]},
               body={"feedbacks": [{"id": SURGI.lower(), "url": "https://pubpeer.com/publications/ABC",
                                    "total_comments": 42, "users": "Secret Sleuth, Another Person",
                                    "last_commented_at": {"date": "2021-01-02 10:00:00", "timezone": "UTC"}}]})
    replay.add("https://api.openalex.org/sources/issn:1234-5678", {
        "id": "https://openalex.org/S1", "display_name": "Journal of Authentic Studies", "issn_l": "1234-5678",
        "issn": ["1234-5678"], "homepage_url": "https://authentic-studies.org/", "is_in_doaj": True,
        "works_count": 1500, "host_organization_name": "Authentic Press"})
    replay.add("https://api.openalex.org/sources?search=authentic-studies-journal.com&per-page=5", {"results": []})
    return IntegrityLens(replay.runtime("integritylens", pubpeer_devkey="KEY"))


async def test_integrity_report_levels(lens):
    w = (await lens.integrity_report(f"https://doi.org/{WAKEFIELD}"))["data"]
    assert w["status"] == "retracted_or_removed"
    assert w["signals"][0]["evidence_id"]
    c = (await lens.integrity_report(WATSON))["data"]
    assert c["status"] == "no_signals_in_checked_sources"
    assert c["venue"]["name"] == "Nature"
    f = (await lens.integrity_report(FAKE))["data"]
    assert f["status"] == "serious_concern_signal"
    assert f["signals"][0]["kind"] == "doi_not_registered"


async def test_pubpeer_counts_without_commenter_names(lens):
    res = await lens.integrity_report(SURGI)
    kinds = [s["kind"] for s in res["data"]["signals"]]
    assert "pubpeer_discussion" in kinds and kinds[0] == "retraction"
    assert "Secret Sleuth" not in dumps(res)


async def test_screen_references(lens, replay):
    replay.add("https://pubpeer.com/v3/publications?devkey=KEY", method="POST",
               json_body={"dois": [WAKEFIELD.lower(), WATSON, FAKE]}, body={"feedbacks": []})
    refs = f"1. Wakefield et al. doi:{WAKEFIELD}\n2. Watson & Crick {WATSON}\n3. Invented {FAKE}\n"
    res = await lens.screen_references(refs)
    assert res["data"]["summary"] == {"retracted_or_removed": 1, "serious_concern_signal": 1,
                                      "no_signals_in_checked_sources": 1}
    assert res["data"]["results"][0]["doi"] == WAKEFIELD.lower()


async def test_local_datasets_and_journal_checks(lens, tmp_path: Path):
    rw = tmp_path / "rw.csv"
    rw.write_text("Record ID,Title,Journal,RetractionDate,RetractionDOI,OriginalPaperDOI,RetractionNature,Reason\n"
                  "1,Paper,Journal of Authentic Studies,1/2/2023 0:00,10.5555/notice1,10.5555/paper.1,Retraction,"
                  "+Paper Mill;\n2,Other,Journal of Authentic Studies,1/3/2023,unavailable,10.5555/paper.2,"
                  "Expression of concern,+Concerns\n")
    hij = tmp_path / "hijacked.csv"
    hij.write_text("Journal title,ISSN,Authentic website,Hijacked website\n"
                   "Journal of Authentic Studies,1234-5678,https://authentic-studies.org,"
                   "https://www.authentic-studies-journal.com; http://jas-online.net\n")
    pps = tmp_path / "pps.csv"
    pps.write_text("DOI,Detector,Assessment\n10.5555/paper.3,tortured,\n")
    assert lens.load_dataset("retraction_watch", str(rw))["data"]["rows_loaded"] == 2
    assert lens.load_dataset("hijacked_journals", str(hij))["data"]["rows_loaded"] == 1
    assert lens.load_dataset("problematic_paper_screener", str(pps))["data"]["rows_loaded"] == 1

    clone = await lens.journal_legitimacy("https://www.authentic-studies-journal.com/submit")
    f = clone["data"]["findings"][0]
    assert f["signal"] == "hijacked_journal_list_match" and f["severity"] == "high"
    real = await lens.journal_legitimacy("1234-5678")
    sigs = {x["signal"]: x for x in real["data"]["findings"]}
    assert sigs["hijacked_journal_list_match"]["severity"] == "info"
    assert sigs["retraction_watch_records"]["count"] == 2
    assert real["data"]["venue"]["in_doaj"] is True
    status = lens.sources_status()["data"]["local_datasets"]
    assert set(status) == {"retraction_watch", "hijacked_journals", "problematic_paper_screener"}


def test_tortured_phrases(lens):
    text = ("In this paper we propose a profound neural organization with an irregular timberland classifier on "
            "colossal information, improving the flag to clamor proportion. The signal to noise ratio is reported.")
    res = lens.scan_tortured_phrases(text)["data"]
    assert [m["expected"] for m in res["matches"]] == ["deep neural network", "random forest", "big data",
                                                       "signal to noise"]
    assert res["distinct_phrases"] == 4
    assert all("arXiv:2107.06751" in m["source"] for m in res["matches"])
