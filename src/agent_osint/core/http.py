"""Polite async HTTP client used by every server.

Features: per-host rate limiting, retries with backoff, Retry-After handling,
response-size caps, a SQLite response cache, robots.txt checks for HTML pages,
and a record/replay mode so real responses can be captured once and replayed
in tests (``AGENT_OSINT_RECORD_DIR`` / ``AGENT_OSINT_REPLAY_DIR``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import sqlite3
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .config import Settings


class FetchError(Exception):
    """A request failed in a way the caller should report, not crash on."""

    def __init__(self, message: str, *, url: str, status: int | None = None, code: str = "fetch_error"):
        super().__init__(message)
        self.url = url
        self.status = status
        self.code = code


class RateLimited(FetchError):
    def __init__(self, url: str, retry_after: float | None):
        super().__init__(
            f"rate limited by {urlsplit(url).netloc}"
            + (f"; retry after {retry_after:.0f}s" if retry_after else ""),
            url=url,
            status=429,
            code="rate_limited",
        )
        self.retry_after = retry_after


class RobotsDisallowed(FetchError):
    def __init__(self, url: str):
        super().__init__(f"robots.txt disallows fetching {url}", url=url, code="robots_disallowed")


class OfflineError(FetchError):
    def __init__(self, url: str):
        super().__init__(
            f"no recorded response for {url} (offline/replay mode)", url=url, code="offline"
        )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_url(url: str, params: dict[str, Any] | None = None) -> str:
    """Canonical URL with sorted query parameters (used for cache/replay keys)."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if params:
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    query.append((key, str(item)))
            else:
                query.append((key, str(value)))
    query.sort()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def request_key(method: str, url: str, body: bytes | None = None, *, secret_params: tuple[str, ...] = ()) -> str:
    """Stable key for a request. Secret query params (API keys) are excluded."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in secret_params]
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))
    digest = hashlib.sha256()
    digest.update(method.upper().encode())
    digest.update(b"\n")
    digest.update(clean.encode())
    if body:
        digest.update(b"\n")
        digest.update(body)
    return digest.hexdigest()


SECRET_PARAMS = ("api_key", "key", "access_token", "devkey", "token")


@dataclass
class FetchResult:
    url: str
    requested_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    fetched_at: str
    from_cache: bool = False
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def text(self) -> str:
        charset = "utf-8"
        ctype = self.content_type
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8-sig", errors="replace"))


class _HostLimiter:
    """Token bucket: ``rps`` sustained rate, ``burst`` requests may go out back-to-back."""

    def __init__(self, rps: float, burst: int = 1):
        self.rate = rps
        self.capacity = max(1, burst)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.rate <= 0:
            return
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                await asyncio.sleep((1 - self.tokens) / self.rate)
                self.tokens = 1.0
                self.updated = time.monotonic()
            self.tokens -= 1


class ResponseCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS http_cache (key TEXT PRIMARY KEY, url TEXT, final_url TEXT,"
            " status INTEGER, headers TEXT, body BLOB, fetched_at TEXT, stored REAL)"
        )
        self._conn.commit()

    def get(self, key: str, ttl: float) -> FetchResult | None:
        row = self._conn.execute(
            "SELECT url, final_url, status, headers, body, fetched_at, stored FROM http_cache WHERE key=?",
            (key,),
        ).fetchone()
        if not row:
            return None
        if ttl >= 0 and time.time() - row[6] > ttl:
            return None
        return FetchResult(
            url=row[1], requested_url=row[0], status=row[2], headers=json.loads(row[3]),
            content=row[4], fetched_at=row[5], from_cache=True,
        )

    def put(self, key: str, result: FetchResult) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?,?)",
            (key, result.requested_url, result.url, result.status, json.dumps(result.headers),
             result.content, result.fetched_at, time.time()),
        )
        self._conn.commit()


class Fetcher:
    """Shared HTTP client. One instance per server process."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                 cache: bool = True, cache_path: Path | None = None):
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._limiters: dict[str, _HostLimiter] = {}
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        self.cache = ResponseCache(cache_path or settings.home / "http_cache.sqlite") if cache else None

    # -- plumbing -----------------------------------------------------------------
    def _client_for(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self.settings.http_timeout, connect=15.0),
                headers={"User-Agent": self.settings.user_agent, "Accept-Encoding": "gzip, deflate"},
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _limiter(self, host: str) -> _HostLimiter:
        if host not in self._limiters:
            rps = self.settings.host_rps.get(host, 2.0)
            self._limiters[host] = _HostLimiter(rps, self.settings.host_burst.get(host, 1))
        return self._limiters[host]

    # -- replay / record ------------------------------------------------------------
    def _fixture_path(self, base: Path, method: str, url: str, body: bytes | None) -> Path:
        host = urlsplit(url).netloc or "local"
        return base / host / f"{request_key(method, url, body, secret_params=SECRET_PARAMS)[:32]}.json"

    def _replay(self, method: str, url: str, body: bytes | None) -> FetchResult:
        assert self.settings.replay_dir is not None
        path = self._fixture_path(self.settings.replay_dir, method, url, body)
        if not path.exists():
            raise OfflineError(url)
        data = json.loads(path.read_text())
        resp = data["response"]
        content = base64.b64decode(resp["body_b64"]) if "body_b64" in resp else resp.get("body", "").encode()
        return FetchResult(
            url=resp.get("final_url", url), requested_url=url, status=resp["status"],
            headers={k.lower(): v for k, v in resp.get("headers", {}).items()}, content=content,
            fetched_at=resp.get("fetched_at", utcnow_iso()), redirects=resp.get("redirects", []),
        )

    def _record(self, method: str, url: str, body: bytes | None, result: FetchResult) -> None:
        assert self.settings.record_dir is not None
        path = self._fixture_path(self.settings.record_dir, method, url, body)
        path.parent.mkdir(parents=True, exist_ok=True)
        clean_url = url
        for secret in SECRET_PARAMS:
            if f"{secret}=" in clean_url:
                parts = urlsplit(clean_url)
                q = [(k, v if k != secret else "REDACTED") for k, v in parse_qsl(parts.query, keep_blank_values=True)]
                clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))
        headers = {k: v for k, v in result.headers.items() if k.lower() not in {"set-cookie"}}
        path.write_text(json.dumps({
            "request": {"method": method, "url": clean_url, "body": body.decode("utf-8", "replace") if body else None},
            "response": {"status": result.status, "final_url": result.url, "headers": headers,
                         "body_b64": base64.b64encode(result.content).decode(), "fetched_at": result.fetched_at,
                         "redirects": result.redirects},
        }, indent=1))

    # -- robots ----------------------------------------------------------------------
    async def allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is None or time.time() - cached[0] > 86400:
            parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                res = await self.request("GET", origin + "/robots.txt", max_bytes=512 * 1024,
                                         follow_redirects=True, cache_ttl=86400)
                # RFC 9309 s2.3.1: 4xx ("unavailable") => crawler may access everything;
                # 5xx ("unreachable") => assume complete disallow.
                if res.status >= 500:
                    parser.disallow_all = True  # type: ignore[union-attr]
                elif res.status >= 400:
                    parser = None
                else:
                    parser.parse(res.text().splitlines())  # type: ignore[union-attr]
            except FetchError:
                parser = None
            self._robots[origin] = (time.time(), parser)
            cached = self._robots[origin]
        parser = cached[1]
        if parser is None:
            return True
        agent = self.settings.user_agent.split("/")[0]
        return parser.can_fetch(agent, url)

    # -- main entry --------------------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        max_bytes: int | None = None,
        cache_ttl: float | None = None,
        follow_redirects: bool = True,
        max_redirects: int = 5,
        respect_robots: bool = False,
        retries: int = 3,
        max_retry_wait: float = 60.0,
    ) -> FetchResult:
        full_url = normalize_url(url, params)
        body = json.dumps(json_body, sort_keys=True).encode() if json_body is not None else None
        max_bytes = max_bytes or self.settings.max_response_bytes

        if respect_robots and not await self.allowed_by_robots(full_url):
            raise RobotsDisallowed(full_url)
        if self.settings.replay_dir is not None:
            return self._replay(method, full_url, body)
        if self.settings.offline:
            raise OfflineError(full_url)

        ttl = self.settings.cache_ttl_seconds if cache_ttl is None else cache_ttl
        key = request_key(method, full_url, body, secret_params=SECRET_PARAMS)
        if self.cache is not None and ttl > 0:
            cached = self.cache.get(key, ttl)
            if cached is not None:
                return cached

        redirects: list[str] = []
        current = full_url
        for _hop in range(max_redirects + 1):
            result = await self._send(method, current, headers, body, max_bytes, retries, max_retry_wait)
            if follow_redirects and result.status in (301, 302, 303, 307, 308) and "location" in result.headers:
                nxt = str(httpx.URL(current).join(result.headers["location"]))
                redirects.append(nxt)
                current = nxt
                if result.status == 303:
                    method, body = "GET", None
                continue
            break
        else:
            raise FetchError("too many redirects", url=full_url, code="too_many_redirects")

        result.requested_url = full_url
        result.redirects = redirects
        if self.cache is not None and ttl > 0 and result.status < 500 and result.status not in (401, 403, 407, 429):
            self.cache.put(key, result)
        if self.settings.record_dir is not None:
            self._record(method, full_url, body, result)
        return result

    async def _send(self, method: str, url: str, headers: dict[str, str] | None, body: bytes | None,
                    max_bytes: int, retries: int, max_retry_wait: float) -> FetchResult:
        await guard_target(url, self.settings.allow_private_network)
        host = urlsplit(url).netloc
        client = self._client_for()
        last_exc: Exception | None = None
        req_headers = dict(headers or {})
        if body is not None:
            req_headers.setdefault("Content-Type", "application/json")
        for attempt in range(retries):
            await self._limiter(host).wait()
            try:
                async with client.stream(method, url, headers=req_headers, content=body) as resp:
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            chunks.append(chunk[: max(0, max_bytes - (size - len(chunk)))])
                            truncated = True
                            break
                        chunks.append(chunk)
                    result = FetchResult(
                        url=str(resp.url), requested_url=url, status=resp.status_code,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        content=b"".join(chunks), fetched_at=utcnow_iso(), truncated=truncated,
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            if result.status == 429:
                retry_after = _parse_retry_after(result.headers.get("retry-after"))
                if retry_after is not None and retry_after <= max_retry_wait and attempt < retries - 1:
                    await asyncio.sleep(retry_after)
                    continue
                if retry_after is None and attempt < retries - 1:
                    await asyncio.sleep(min(5 * (attempt + 1), max_retry_wait))
                    continue
                raise RateLimited(url, retry_after)
            if result.status >= 500 and attempt < retries - 1:
                await asyncio.sleep(min(2 ** (attempt + 1), 20))
                continue
            return result
        raise FetchError(f"request failed: {last_exc!r}", url=url, code="network_error")

    async def get(self, url: str, **kwargs: Any) -> FetchResult:
        return await self.request("GET", url, **kwargs)

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> FetchResult:
        kwargs.setdefault("cache_ttl", 0)
        return await self.request("POST", url, json_body=payload, **kwargs)

    async def download(self, url: str, dest: Path, *, max_bytes: int, headers: dict[str, str] | None = None) -> Path:
        """Stream a large file to disk (bypasses the SQLite cache)."""
        url = normalize_url(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.settings.replay_dir is not None:
            res = self._replay("GET", url, None)
            if res.status != 200:
                raise FetchError(f"download failed with HTTP {res.status}", url=url, status=res.status)
            dest.write_bytes(res.content)
            return dest
        if self.settings.offline:
            raise OfflineError(url)
        await guard_target(url, self.settings.allow_private_network)
        host = urlsplit(url).netloc
        await self._limiter(host).wait()
        size = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        client = self._client_for()
        async with client.stream("GET", url, headers=headers or {}, follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise FetchError(f"download failed with HTTP {resp.status_code}", url=url, status=resp.status_code)
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        raise FetchError(f"download exceeds {max_bytes} bytes", url=url, code="too_large")
                    fh.write(chunk)
        tmp.replace(dest)
        return dest


class BlockedTarget(FetchError):
    def __init__(self, url: str, reason: str):
        super().__init__(f"refusing to fetch {url}: {reason}", url=url, code="blocked_target")


async def guard_target(url: str, allow_private: bool) -> None:
    """Block requests to loopback/private/link-local addresses (SSRF protection for agent-supplied URLs).

    Resolves the host and checks every address. Set AGENT_OSINT_ALLOW_PRIVATE_NETWORK=1 to disable.
    """
    if allow_private:
        return
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedTarget(url, f"scheme {parts.scheme!r} not allowed")
    host = parts.hostname or ""
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal"):
        raise BlockedTarget(url, "local hostname")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except OSError:
            return  # let the HTTP client report the DNS failure
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0].split("%")[0]))
            except ValueError:
                continue
    for a in addrs:
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified:
            raise BlockedTarget(url, f"{host} resolves to non-public address {a}")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(value)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


def write_fixture(base: Path, method: str, url: str, *, status: int = 200, body: bytes | str = b"",
                  headers: dict[str, str] | None = None, json_body: Any = None, final_url: str | None = None) -> Path:
    """Helper for tests: write a replay fixture for a request."""
    if isinstance(body, str):
        body = body.encode()
    url = normalize_url(url)
    req_body = json.dumps(json_body, sort_keys=True).encode() if json_body is not None else None
    host = urlsplit(url).netloc or "local"
    path = base / host / f"{request_key(method, url, req_body, secret_params=SECRET_PARAMS)[:32]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "request": {"method": method, "url": url},
        "response": {"status": status, "final_url": final_url or url, "headers": headers or {},
                     "body_b64": base64.b64encode(body).decode(), "fetched_at": "2026-09-27T00:00:00Z"},
    }))
    return path
