"""Unit tests for catalog — curated loading, normalization, and the live merge.

Live registry calls are faked with a stub httpx client so no network is
touched; tests focus on error isolation, de-dupe, and the offline default.
"""
import asyncio

import pytest

import catalog


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_requires_kind_name_and_url():
    assert catalog._normalize_entry({"name": "x"}, "s") is None  # no kind
    assert catalog._normalize_entry({"kind": "mcp_remote", "name": ""}, "s") is None
    # mcp_remote without a usable url is dropped
    assert catalog._normalize_entry({"kind": "mcp_remote", "name": "x"}, "s") is None
    # non-http url is rejected
    assert catalog._normalize_entry(
        {"kind": "mcp_remote", "name": "x", "url": "ftp://h/y"}, "s"
    ) is None


def test_normalize_valid_entries_and_slug():
    remote = catalog._normalize_entry(
        {"kind": "mcp_remote", "name": "My Server", "url": "https://h/mcp"}, "src"
    )
    assert remote["id"] == "my-server"
    assert remote["source"] == "src"
    assert remote["url"] == "https://h/mcp"

    rest = catalog._normalize_entry(
        {"kind": "rest_openapi", "name": "API", "openapi_url": "https://h/o.json",
         "base_url": "https://h", "auth_hint": "bearer"}, "src"
    )
    assert rest["openapi_url"] == "https://h/o.json"
    assert rest["base_url"] == "https://h"
    assert rest["auth_hint"] == "bearer"


# ---------------------------------------------------------------------------
# Curated + offline build
# ---------------------------------------------------------------------------

def test_curated_loads_and_is_valid():
    entries = catalog.load_curated()
    assert entries, "bundled catalog.json should contain entries"
    for e in entries:
        assert e["kind"] in ("mcp_remote", "rest_openapi")
        assert e["name"] and e["id"]


def test_build_offline_returns_curated_only():
    data = _run(catalog.build_catalog(live=False))
    assert data["live"] is False
    assert data["errors"] == {}
    assert len(data["entries"]) == len(catalog.load_curated())


# ---------------------------------------------------------------------------
# Live merge: error isolation + de-dupe
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_cache():
    catalog._cache.clear()
    yield
    catalog._cache.clear()


def test_live_isolates_failing_sources(monkeypatch):
    async def good(client):
        return [{"kind": "mcp_remote", "name": "Live One", "url": "https://live/one"}]

    async def bad(client):
        raise RuntimeError("boom")

    monkeypatch.setattr(catalog, "CATALOG_LIVE", True)
    monkeypatch.setattr(catalog, "SOURCES", {"good": good, "bad": bad})

    data = _run(catalog.build_catalog(live=True))
    assert "bad" in data["errors"] and "boom" in data["errors"]["bad"]
    # the good source still contributed despite the bad one raising
    names = [e["name"] for e in data["entries"]]
    assert "Live One" in names
    # curated entries are still present
    assert len(data["entries"]) >= len(catalog.load_curated()) + 1


def test_live_dedupe_prefers_curated(monkeypatch):
    curated = catalog.load_curated()
    dup = next(e for e in curated if e["kind"] == "mcp_remote")

    async def dupe_source(client):
        return [{"kind": "mcp_remote", "name": "DIFFERENT NAME", "url": dup["url"]}]

    monkeypatch.setattr(catalog, "CATALOG_LIVE", True)
    monkeypatch.setattr(catalog, "SOURCES", {"dupe": dupe_source})

    data = _run(catalog.build_catalog(live=True))
    matches = [e for e in data["entries"] if e.get("url") == dup["url"]]
    assert len(matches) == 1
    assert matches[0]["name"] == dup["name"]  # curated won


def test_live_gate_disables_probing(monkeypatch):
    called = {"n": 0}

    async def src(client):
        called["n"] += 1
        return []

    monkeypatch.setattr(catalog, "CATALOG_LIVE", False)  # global gate off
    monkeypatch.setattr(catalog, "SOURCES", {"src": src})

    data = _run(catalog.build_catalog(live=True))
    assert data["live"] is False
    assert called["n"] == 0
    assert len(data["entries"]) == len(catalog.load_curated())
