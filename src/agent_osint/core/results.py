"""Uniform result envelope returned by every tool.

Agents get the same shape everywhere:
    {"ok": true, "data": ..., "evidence": [...], "warnings": [...],
     "limitations": [...], "sources_checked": [...]}
or on failure
    {"ok": false, "error": {"code": ..., "message": ..., "hint": ...}}
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any

from .domains import InvalidDomain
from .evidence import Evidence
from .http import FetchError, RateLimited

log = logging.getLogger("agent_osint")


def ok(data: Any, *, evidence: list[Evidence | dict] | None = None, warnings: list[str] | None = None,
       limitations: list[str] | None = None, sources_checked: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "data": data}
    if evidence:
        seen: set[str] = set()
        ev_list = []
        for ev in evidence:
            d = ev.to_dict() if isinstance(ev, Evidence) else ev
            if d.get("id") in seen:
                continue
            seen.add(d.get("id", ""))
            ev_list.append(d)
        out["evidence"] = ev_list
    if warnings:
        out["warnings"] = list(dict.fromkeys(warnings))
    if limitations:
        out["limitations"] = list(dict.fromkeys(limitations))
    if sources_checked:
        out["sources_checked"] = list(dict.fromkeys(sources_checked))
    return out


def fail(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    err = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    return {"ok": False, "error": err}


class ToolInputError(ValueError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class MissingCredential(Exception):
    def __init__(self, env_var: str, how_to_get: str):
        super().__init__(f"{env_var} is not set")
        self.env_var = env_var
        self.how_to_get = how_to_get


def tool_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Convert expected exceptions into structured failures instead of MCP errors."""

    def _handle(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, RateLimited):
            return fail("rate_limited", str(exc), "Wait and retry; results fetched so far are cached.")
        if isinstance(exc, FetchError):
            return fail(exc.code, str(exc), f"url: {exc.url}")
        if isinstance(exc, MissingCredential):
            return fail("missing_credential", f"{exc.env_var} is not configured", exc.how_to_get)
        if isinstance(exc, json.JSONDecodeError):
            return fail("upstream_error", f"upstream returned malformed JSON: {exc}")
        if isinstance(exc, PermissionError):
            return fail("forbidden_path", str(exc), "Set AGENT_OSINT_FILE_ROOTS to include this location.")
        if isinstance(exc, (ToolInputError,)):
            return fail("invalid_input", str(exc), exc.hint)
        if isinstance(exc, (InvalidDomain, ValueError)):
            return fail("invalid_input", str(exc))
        log.exception("unexpected tool error")
        return fail("internal_error", f"{type(exc).__name__}: {exc}")

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - boundary
                return _handle(exc)
        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - boundary
            return _handle(exc)
    return wrapper
