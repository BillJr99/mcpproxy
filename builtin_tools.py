"""
Built-in mcpproxy utility tools — registered automatically at startup,
no YAML config file required.

  mcpproxy__listfiles   List files/directories inside the mcpproxy files dir.
  mcpproxy__getfile     Read a file from the mcpproxy files dir (text or base64).

The *base directory* defaults to ``/app/files`` (mounted as a Docker volume so
artefacts persist across container restarts) and can be overridden at runtime with
the ``MCPPROXY_FILES_DIR`` environment variable.  Only files **inside** the base
directory are accessible — path-traversal attempts are rejected.
"""

import base64
import os
from pathlib import Path
from typing import Any


def _base_dir() -> Path:
    """Return the resolved base directory for built-in file access.

    Evaluated on each call so that tests can override MCPPROXY_FILES_DIR
    with monkeypatch without restarting the process.
    """
    raw = os.environ.get("MCPPROXY_FILES_DIR", "/app/files")
    return Path(raw).resolve()


def _safe_resolve(relative: str | None) -> Path:
    """Resolve *relative* under the base dir; raise ValueError on traversal."""
    base = _base_dir()
    target = (base / (relative or "")).resolve()
    # relative_to() raises ValueError if target is not under base
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError(
            f"Path '{relative}' is outside the allowed directory '{base}'"
        )
    return target


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def list_files(
    context: dict[str, Any],
    path: str | None = None,
) -> dict[str, Any]:
    """List files and subdirectories at *path* inside the files base directory.

    Returns a JSON object with an ``entries`` list; each entry has ``name``,
    ``type`` (``"file"`` or ``"directory"``), and ``size`` (bytes, files only).
    If the directory does not exist yet the entries list is empty (not an error).
    """
    try:
        target = _safe_resolve(path)
        base = _base_dir()
        if not target.exists():
            return {
                "ok": True,
                "base_dir": str(base),
                "path": path or "",
                "entries": [],
            }
        if not target.is_dir():
            return {"ok": False, "error": f"'{path}' is not a directory"}
        entries: list[dict[str, Any]] = []
        for entry in sorted(target.iterdir()):
            entries.append(
                {
                    "name": entry.name,
                    "type": "directory" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else None,
                }
            )
        return {
            "ok": True,
            "base_dir": str(base),
            "path": path or "",
            "entries": entries,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def get_file(
    context: dict[str, Any],
    path: str,
    encoding: str = "auto",
) -> dict[str, Any]:
    """Read a file from the files base directory.

    *encoding* controls how the content is returned:
      ``"auto"``   (default) — try UTF-8; fall back to base64 for binary files.
      ``"text"``   — decode as UTF-8; error if the file is binary.
      ``"base64"`` — always return base64-encoded bytes (safe for images etc.).

    Returns a JSON object with ``content`` (string), ``encoding`` used, and
    ``size`` (bytes).
    """
    try:
        target = _safe_resolve(path)
        if not target.exists():
            return {"ok": False, "error": f"File not found: {path}"}
        if not target.is_file():
            return {"ok": False, "error": f"Not a file: {path}"}

        raw = target.read_bytes()
        size = len(raw)

        if encoding == "base64":
            return {
                "ok": True,
                "path": path,
                "size": size,
                "content": base64.b64encode(raw).decode(),
                "encoding": "base64",
            }

        # encoding == "text" or "auto"
        try:
            text = raw.decode("utf-8")
            return {
                "ok": True,
                "path": path,
                "size": size,
                "content": text,
                "encoding": "text",
            }
        except UnicodeDecodeError:
            if encoding == "text":
                return {
                    "ok": False,
                    "error": (
                        f"File '{path}' is not valid UTF-8 text. "
                        "Try encoding='base64'."
                    ),
                }
            # "auto" fallback → base64
            return {
                "ok": True,
                "path": path,
                "size": size,
                "content": base64.b64encode(raw).decode(),
                "encoding": "base64",
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
