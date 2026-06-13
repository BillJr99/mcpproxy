"""Provider catalog — curated + optional live registry aggregation.

Powers the web UI's "Browse providers" modal.  A *catalog entry* describes a
known provider the user can configure with one click; the UI deep-links the
entry into the existing "New Provider" wizard with its fields pre-filled.

Two entry kinds, discriminated by ``kind``:

  * ``mcp_remote``   — a remote MCP server reachable at ``url`` (bridged with
                       ``npx -y mcp-remote <url>``).
  * ``rest_openapi`` — a REST API described by an OpenAPI/Swagger spec at
                       ``openapi_url`` (optionally with a ``base_url`` and an
                       ``auth_hint``).

Data sources are hybrid:

  * a curated JSON file bundled in the repo (``frontend/catalog.json``) — the
    offline-safe default; and
  * optional live probing of external registries (MCP registry, Smithery,
    APIs.guru), enabled per-request and behind the ``MCPPROXY_CATALOG_LIVE``
    gate.  Live sources are fetched concurrently with per-source error
    isolation and a short-lived cache, so one slow/erroring registry never
    blocks the others or the curated list.

This module is framework-free (no FastAPI imports) so it stays unit-testable.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

# Curated catalog ships next to the UI app so the existing ``COPY frontend/``
# Docker step picks it up.
CURATED_PATH = Path(__file__).parent / "frontend" / "catalog.json"

# Master switch for live registry probing.  Set MCPPROXY_CATALOG_LIVE=0 to
# disable all outbound probes in locked-down deployments (mirrors the
# MCPPROXY_WEB_TERMINAL gate).  Default on.
CATALOG_LIVE = os.environ.get("MCPPROXY_CATALOG_LIVE", "1") not in ("0", "false", "False", "")

# Per-source cache lifetime and outbound timeout (seconds).
CATALOG_TTL = float(os.environ.get("MCPPROXY_CATALOG_TTL", "900"))
CATALOG_TIMEOUT = float(os.environ.get("MCPPROXY_CATALOG_TIMEOUT", "8"))

# Upper bound on how many entries a single live source may contribute, so a
# huge registry (APIs.guru lists thousands) can't flood the UI.
CATALOG_MAX_PER_SOURCE = int(os.environ.get("MCPPROXY_CATALOG_MAX_PER_SOURCE", "150"))

_USER_AGENT = "mcpproxy-catalog/1.0 (+https://github.com/BillJr99/mcpproxy)"

# source -> (fetched_at, entries)
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _slugify(value: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (value or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "provider"


def _http_url(value: Any) -> str | None:
    """Return ``value`` only if it is a plain http(s) URL, else ``None``."""
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _normalize_entry(raw: dict[str, Any], source: str) -> dict[str, Any] | None:
    """Coerce a raw dict into a valid catalog entry, or ``None`` if unusable.

    Enforces that URLs are http(s) only and that the entry carries the field
    its ``kind`` requires.  Untrusted registry text (name/description) is kept
    as-is here; the browser ``esc()``s it at render time.
    """
    kind = raw.get("kind")
    name = (raw.get("name") or "").strip()
    if not name or kind not in ("mcp_remote", "rest_openapi"):
        return None

    entry: dict[str, Any] = {
        "id": raw.get("id") or _slugify(name),
        "kind": kind,
        "name": name,
        "description": (raw.get("description") or "").strip(),
        "categories": [c for c in (raw.get("categories") or []) if isinstance(c, str)],
        "homepage": _http_url(raw.get("homepage")),
        "source": source,
    }

    if kind == "mcp_remote":
        url = _http_url(raw.get("url"))
        if not url:
            return None
        entry["url"] = url
    else:  # rest_openapi
        openapi_url = _http_url(raw.get("openapi_url"))
        if not openapi_url:
            return None
        entry["openapi_url"] = openapi_url
        entry["base_url"] = _http_url(raw.get("base_url"))
        if raw.get("auth_hint"):
            entry["auth_hint"] = str(raw["auth_hint"])

    return entry


def _dedupe(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate entries by (kind, url|openapi_url); first seen wins.

    ``build_catalog`` always prepends curated entries, so curated wins ties.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in entries:
        key = (e["kind"], e.get("url") or e.get("openapi_url") or e["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Curated source
# ---------------------------------------------------------------------------

def load_curated() -> list[dict[str, Any]]:
    """Load and normalize the bundled curated catalog.  Never raises."""
    try:
        data = json.loads(CURATED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = []
    for raw in (data.get("entries") or []):
        norm = _normalize_entry(raw, raw.get("source") or "curated")
        if norm:
            entries.append(norm)
    return entries


# ---------------------------------------------------------------------------
# Live registry adapters
# ---------------------------------------------------------------------------

async def _fetch_mcp_registry(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Official MCP registry — servers exposing a remote endpoint."""
    resp = await client.get("https://registry.modelcontextprotocol.io/v0/servers")
    resp.raise_for_status()
    out: list[dict[str, Any]] = []
    for srv in (resp.json().get("servers") or []):
        remotes = srv.get("remotes") or []
        url = next((_http_url(r.get("url")) for r in remotes if _http_url(r.get("url"))), None)
        if not url:
            continue  # only servers we can bridge by URL belong in the catalog
        out.append({
            "kind": "mcp_remote",
            "name": srv.get("title") or srv.get("name") or url,
            "description": srv.get("description") or "",
            "homepage": (srv.get("repository") or {}).get("url") or srv.get("websiteUrl"),
            "url": url,
        })
    return out


async def _fetch_smithery(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Smithery registry — requires a SMITHERY_API_KEY to query."""
    api_key = os.environ.get("SMITHERY_API_KEY")
    if not api_key:
        raise RuntimeError("SMITHERY_API_KEY not set")
    resp = await client.get(
        "https://registry.smithery.ai/servers",
        params={"pageSize": CATALOG_MAX_PER_SOURCE},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    out: list[dict[str, Any]] = []
    for srv in (resp.json().get("servers") or []):
        url = _http_url(srv.get("deploymentUrl") or srv.get("url"))
        if not url:
            continue
        out.append({
            "kind": "mcp_remote",
            "name": srv.get("displayName") or srv.get("qualifiedName") or url,
            "description": srv.get("description") or "",
            "homepage": srv.get("homepage"),
            "url": url,
        })
    return out


async def _fetch_apis_guru(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """APIs.guru directory — preferred OpenAPI/Swagger spec per API."""
    resp = await client.get("https://api.apis.guru/v2/list.json")
    resp.raise_for_status()
    out: list[dict[str, Any]] = []
    for api in resp.json().values():
        versions = api.get("versions") or {}
        ver = versions.get(api.get("preferred")) or next(iter(versions.values()), None)
        if not ver:
            continue
        spec_url = _http_url(ver.get("swaggerUrl") or ver.get("openapiUrl"))
        if not spec_url:
            continue
        info = ver.get("info") or {}
        out.append({
            "kind": "rest_openapi",
            "name": info.get("title") or spec_url,
            "description": (info.get("description") or "").strip()[:300],
            "categories": info.get("x-apisguru-categories") or [],
            "homepage": info.get("contact", {}).get("url") if isinstance(info.get("contact"), dict) else None,
            "openapi_url": spec_url,
        })
    return out


SOURCES: dict[str, Callable[[httpx.AsyncClient], Awaitable[list[dict[str, Any]]]]] = {
    "mcp_registry": _fetch_mcp_registry,
    "smithery": _fetch_smithery,
    "apis_guru": _fetch_apis_guru,
}


# ---------------------------------------------------------------------------
# Cache + orchestration
# ---------------------------------------------------------------------------

async def _cached_fetch(source: str, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    now = time.time()
    hit = _cache.get(source)
    if hit and now - hit[0] < CATALOG_TTL:
        return hit[1]
    raw_entries = await SOURCES[source](client)
    entries = []
    for raw in raw_entries[:CATALOG_MAX_PER_SOURCE]:
        norm = _normalize_entry(raw, source)
        if norm:
            entries.append(norm)
    _cache[source] = (now, entries)
    return entries


async def build_catalog(
    live: bool = False, sources: list[str] | None = None
) -> dict[str, Any]:
    """Merge curated entries with (optionally) live registry entries.

    Always returns the curated list even if every live source errors.  Live
    probing is skipped entirely unless ``live`` is true *and* the
    ``MCPPROXY_CATALOG_LIVE`` gate is on.  Failing sources are reported in the
    ``errors`` map rather than raising.
    """
    entries = list(load_curated())  # curated first so it wins de-dupe ties
    errors: dict[str, str] = {}
    live = bool(live) and CATALOG_LIVE

    if live:
        wanted = [s for s in (sources or list(SOURCES)) if s in SOURCES]
        async with httpx.AsyncClient(
            timeout=CATALOG_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        ) as client:
            results = await asyncio.gather(
                *(_cached_fetch(s, client) for s in wanted),
                return_exceptions=True,
            )
        for source, res in zip(wanted, results):
            if isinstance(res, Exception):
                errors[source] = str(res) or res.__class__.__name__
            else:
                entries.extend(res)

    return {"entries": _dedupe(entries), "errors": errors, "live": live}
