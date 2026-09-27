"""Core hardening tests: SSRF guard, file-root allowlist, rate limiter, cache rules, privacy helpers."""

from __future__ import annotations

import time

import pytest

from agent_osint.core.config import Settings
from agent_osint.core.http import BlockedTarget, Fetcher, _HostLimiter, guard_target, normalize_url
from agent_osint.core.privacy import WITHHELD, classify_name, safe_email, safe_name, scrub_all


@pytest.mark.parametrize("url", ["http://localhost:8080/admin", "http://127.0.0.1/", "http://10.0.0.5/x",
                                 "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                                 "file:///etc/passwd", "http://metadata.internal/"])
async def test_guard_blocks_private_targets(url):
    with pytest.raises(BlockedTarget):
        await guard_target(url, allow_private=False)


async def test_guard_allows_public_ip_and_override():
    await guard_target("https://93.184.215.14/", allow_private=False)
    await guard_target("http://127.0.0.1/", allow_private=True)


async def test_fetcher_refuses_private_url(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OSINT_HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_OSINT_REPLAY_DIR", raising=False)
    f = Fetcher(Settings.from_env(), cache=False)
    with pytest.raises(BlockedTarget):
        await f.get("http://127.0.0.1:9/")
    await f.aclose()


def test_file_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "data"
    allowed.mkdir()
    monkeypatch.setenv("AGENT_OSINT_FILE_ROOTS", str(allowed))
    s = Settings.from_env()
    assert s.check_file(str(allowed / "x.csv")) == (allowed / "x.csv").resolve()
    with pytest.raises(PermissionError):
        s.check_file(str(tmp_path / "secret.txt"))
    with pytest.raises(PermissionError):
        s.check_file(str(allowed / ".." / "secret.txt"))


async def test_token_bucket_burst_then_rate():
    lim = _HostLimiter(rps=20, burst=5)
    t0 = time.monotonic()
    for _ in range(5):
        await lim.wait()
    assert time.monotonic() - t0 < 0.05  # burst passes immediately
    for _ in range(4):
        await lim.wait()
    assert time.monotonic() - t0 >= 0.15  # then ~20/s


def test_normalize_url_sorts_and_encodes():
    assert normalize_url("HTTPS://Example.COM/a?b=2&a=1", {"c": ["x", "y"], "d": None}) == \
        "https://example.com/a?a=1&b=2&c=x&c=y"


def test_privacy_helpers():
    assert classify_name("Acme Holdings Ltd") == "organization"
    assert safe_name("Erika Mustermann") == WITHHELD
    assert safe_email("privacy@example.com") == "privacy@example.com"
    assert safe_email("erika@web.de") == "[personal mailbox — withheld]"
    assert "[name]" in scrub_all("Submitted by Erika Mustermann yesterday")


async def test_robots_rfc9309_status_handling(replay):
    rt = replay.runtime("core")
    replay.add("https://four.example/robots.txt", "forbidden", status=403)
    replay.add("https://five.example/robots.txt", "oops", status=503)
    replay.add("https://rules.example/robots.txt", "User-agent: *\nDisallow: /private\n")
    assert await rt.fetcher.allowed_by_robots("https://four.example/page")
    assert not await rt.fetcher.allowed_by_robots("https://five.example/page")
    assert await rt.fetcher.allowed_by_robots("https://rules.example/public")
    assert not await rt.fetcher.allowed_by_robots("https://rules.example/private/x")
