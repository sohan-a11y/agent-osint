"""DepCheck tests: parsing (offline), verdict logic (replay), and real registries (pytest -m live)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_osint.depcheck import parse
from agent_osint.depcheck.registries import TOP_NPM_URL, TOP_PYPI_URL
from agent_osint.depcheck.service import DepCheck

PY_CODE = """
import os, sys, json
import numpy as np
import cv2
from PIL import Image
from sklearn.linear_model import LogisticRegression
from google.cloud import storage
import google.cloud.bigquery
from azure.identity import DefaultAzureCredential
from . import sibling
import myproject.utils
import fastapi_websocket_streamer
"""

JS_CODE = """
import React, { useState } from 'react';
import fs from 'node:fs';
import path from "path";
const _ = require('lodash/fp');
import('@scope/pkg/sub/module.js');
export { x } from "./local";
import styles from '../styles.css';
const lodahs = require("lodahs");
"""


def test_python_import_mapping():
    deps, warnings = parse.python_imports(PY_CODE, {"myproject"})
    pkgs = [d["package"] for d in deps]
    assert pkgs == ["numpy", "opencv-python", "Pillow", "scikit-learn", "google-cloud-storage",
                    "google-cloud-bigquery", "azure-identity", "fastapi_websocket_streamer"]
    assert not warnings


def test_js_import_extraction():
    pkgs = [d["package"] for d in parse.js_imports(JS_CODE)]
    assert pkgs == ["react", "lodash", "@scope/pkg", "lodahs"]


def test_manifest_parsers():
    req = "requests>=2.31\n# comment\nuvicorn[standard]==0.30 ; python_version>'3.8'\n-r other.txt\ngit+https://x/y.git\n"
    assert [d["package"] for d in parse.requirements_txt(req)] == ["requests", "uvicorn"]
    pyproj = ('[project]\nname="x"\ndependencies=["httpx>=0.27","pydantic"]\n[project.optional-dependencies]\n'
              'dev=["pytest"]\n[tool.poetry.dependencies]\npython="^3.10"\nrich="^13"\n')
    assert [d["package"] for d in parse.pyproject_toml(pyproj)] == ["httpx", "pydantic", "pytest", "rich"]
    pj = json.dumps({"dependencies": {"express": "^4", "local": "file:../x"}, "devDependencies": {"jest": "29"}})
    assert [d["package"] for d in parse.package_json(pj)] == ["express", "jest"]


def _pypi(name: str, first: datetime, n_releases: int = 1, summary: str = "x", repo: str | None = None) -> dict:
    releases = {}
    for i in range(n_releases):
        t = (first + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        releases[f"0.{i}.0"] = [{"upload_time_iso_8601": t, "yanked": False}]
    return {"info": {"name": name, "version": f"0.{n_releases - 1}.0", "summary": summary,
                     "project_urls": {"Source": repo} if repo else None}, "releases": releases}


@pytest.fixture
def dc(replay):
    now = datetime.now(timezone.utc)
    replay.add(TOP_PYPI_URL, {"rows": [{"project": p, "download_count": 1} for p in
                                       ["boto3", "requests", "numpy", "pandas", "fastapi", "django"]]})
    replay.add("https://pypi.org/pypi/requests/json", _pypi("requests", now - timedelta(days=5000), 150,
                                                            repo="https://github.com/psf/requests"))
    replay.add("https://pypi.org/pypi/reqeusts/json", "Not Found", status=404)
    replay.add("https://pypi.org/pypi/numpyy/json", _pypi("numpyy", now - timedelta(days=5)))
    replay.add("https://pypi.org/pypi/fastapi-websocket-streamer/json",
               _pypi("fastapi-websocket-streamer", now - timedelta(days=200), 2, summary=""))
    replay.add(TOP_NPM_URL, "export const topDownload = [\n  'react',\n  'lodash',\n  'express',\n]\n")
    replay.add("https://registry.npmjs.org/lodahs", {
        "name": "lodahs", "dist-tags": {"latest": "0.0.1-security"}, "versions": {"0.0.1-security": {}},
        "time": {"created": "2019-11-25T16:49:21.220Z", "0.0.1-security": "2019-11-25T16:49:21.312Z"},
        "repository": {"url": "git+https://github.com/npm/security-holder.git"}, "description": "security holding package"})
    replay.add("https://registry.npmjs.org/react", {
        "name": "react", "dist-tags": {"latest": "19.0.0"}, "versions": {f"{i}.0.0": {} for i in range(1, 20)},
        "time": {"created": "2011-10-26T17:46:21.942Z"}, "repository": {"url": "git+https://github.com/facebook/react.git"},
        "description": "React", "maintainers": [{"name": "a"}, {"name": "b"}]})
    replay.add("https://registry.npmjs.org/@scope%2Fpkg", {
        "name": "@scope/pkg", "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {"scripts": {"postinstall": "node x.js"}}},
        "time": {"created": "2020-01-01T00:00:00Z"}, "repository": "github:scope/pkg", "description": "d"})
    return DepCheck(replay.runtime("depcheck"))


async def test_verdicts_pypi(dc):
    res = await dc.check_packages("pypi", ["requests", "reqeusts", "numpyy", "fastapi-websocket-streamer"])
    by = {r["name"]: r for r in res["data"]["results"]}
    assert by["requests"]["verdict"] == "ok"
    assert by["reqeusts"]["verdict"] == "block"
    assert by["reqeusts"]["did_you_mean"][0]["name"] == "requests"
    assert by["numpyy"]["verdict"] == "block"  # very new + look-alike of numpy
    assert {r["code"] for r in by["numpyy"]["risks"]} >= {"very_new", "lookalike_of_popular"}
    fws = by["fastapi-websocket-streamer"]
    assert fws["verdict"] == "block"  # hallucinated name, now registered, <=2 releases, no repository
    assert any(r["code"] == "registered_hallucinated_name" for r in fws["risks"])


async def test_check_imports_js(dc):
    res = await dc.check_imports("import React from 'react'; const x = require('lodahs'); import '@scope/pkg/y';",
                                 "javascript")
    by = {d["package"]: d for d in res["data"]["dependencies"]}
    assert by["react"]["verdict"] == "ok"
    assert by["lodahs"]["verdict"] == "block"
    assert by["@scope/pkg"]["verdict"] == "caution"
    assert any(r["code"] == "install_scripts" for r in by["@scope/pkg"]["risks"])


# ------------------------------------------------------------------ live registry tests

@pytest.mark.live
async def test_live_registries(tmp_path, monkeypatch):
    from agent_osint.core.runtime import get_runtime, reset_runtimes

    monkeypatch.setenv("AGENT_OSINT_HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_OSINT_REPLAY_DIR", raising=False)
    reset_runtimes()
    dc = DepCheck(get_runtime("depcheck"))
    py = await dc.check_packages("pypi", ["requests", "numpy", "reqeusts", "surely-not-a-real-package-xq7"])
    by = {r["name"]: r for r in py["data"]["results"]}
    assert by["requests"]["verdict"] == "ok" and by["numpy"]["verdict"] == "ok"
    assert by["reqeusts"]["verdict"] == "block"
    assert by["reqeusts"]["did_you_mean"][0]["name"] == "requests"
    assert by["surely-not-a-real-package-xq7"]["verdict"] == "block"
    npm = await dc.check_packages("npm", ["react", "express", "crossenv", "lodahs"])
    byn = {r["name"]: r for r in npm["data"]["results"]}
    assert byn["react"]["verdict"] == "ok" and byn["express"]["verdict"] == "ok"
    # crossenv: the 2017 malicious typosquat of cross-env, now an npm security placeholder
    assert byn["crossenv"]["verdict"] == "block"
    assert any(r["code"] == "npm_security_placeholder" for r in byn["crossenv"]["risks"])
    await dc.rt.fetcher.aclose()
    reset_runtimes()
