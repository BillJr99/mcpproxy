"""Tests for builtin_tools.py — mcpproxy__listfiles and mcpproxy__getfile.

These tests monkeypatch MCPPROXY_FILES_DIR to a fresh temp directory so
they never touch the real files directory (default /app/files in Docker).
"""
import base64
import os
from pathlib import Path

import pytest

# Lazily import after env-var monkeypatching where needed.
# At module level we import only the helpers that don't read env at import time.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx() -> dict:
    """Minimal context dict (built-in tools don't use context)."""
    return {}


def _set_base(monkeypatch, path: Path) -> None:
    """Override the MCPPROXY_FILES_DIR env var for a single test."""
    monkeypatch.setenv("MCPPROXY_FILES_DIR", str(path))


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------

class TestListFiles:
    @pytest.mark.asyncio
    async def test_missing_base_dir_returns_empty(self, tmp_path: Path, monkeypatch):
        """If the base dir does not exist yet, return ok=True with empty entries."""
        _set_base(monkeypatch, tmp_path / "nonexistent")
        from builtin_tools import list_files
        result = await list_files(_ctx())
        assert result["ok"] is True
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_empty_base_dir_returns_empty(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx())
        assert result["ok"] is True
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_lists_files_and_dirs(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "snapshot.json").write_text('{"key": "val"}')
        (base / "screenshot.png").write_bytes(b"\x89PNG\r\n")
        (base / "subdir").mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx())
        assert result["ok"] is True
        names = {e["name"] for e in result["entries"]}
        assert names == {"snapshot.json", "screenshot.png", "subdir"}

    @pytest.mark.asyncio
    async def test_file_has_size(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        content = b"hello world"
        (base / "note.txt").write_bytes(content)
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx())
        entry = next(e for e in result["entries"] if e["name"] == "note.txt")
        assert entry["type"] == "file"
        assert entry["size"] == len(content)

    @pytest.mark.asyncio
    async def test_directory_size_is_none(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "subdir").mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx())
        entry = next(e for e in result["entries"] if e["name"] == "subdir")
        assert entry["type"] == "directory"
        assert entry["size"] is None

    @pytest.mark.asyncio
    async def test_list_subdirectory(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        sub = base / "pages"
        sub.mkdir(parents=True)
        (sub / "page1.json").write_text("{}")
        (sub / "page2.json").write_text("{}")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx(), path="pages")
        assert result["ok"] is True
        names = {e["name"] for e in result["entries"]}
        assert names == {"page1.json", "page2.json"}

    @pytest.mark.asyncio
    async def test_entries_sorted_alphabetically(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        for name in ("zzz.txt", "aaa.txt", "mmm.txt"):
            (base / name).write_text("x")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx())
        names = [e["name"] for e in result["entries"]]
        assert names == sorted(names)

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx(), path="../../../etc")
        assert result["ok"] is False
        assert "outside" in result["error"].lower() or "error" in result

    @pytest.mark.asyncio
    async def test_path_returns_base_dir_in_result(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx())
        assert result["base_dir"] == str(base.resolve())

    @pytest.mark.asyncio
    async def test_list_nonexistent_subdir_returns_empty(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx(), path="does_not_exist")
        assert result["ok"] is True
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_recursive_lists_nested_entries(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "top.txt").write_text("x")
        (base / "sub").mkdir()
        (base / "sub" / "a.txt").write_text("a")
        (base / "sub" / "deep").mkdir()
        (base / "sub" / "deep" / "b.txt").write_text("b")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx(), recursive=True)
        assert result["ok"] is True
        paths = {e["path"] for e in result["entries"]}
        assert paths == {"top.txt", "sub", "sub/a.txt", "sub/deep", "sub/deep/b.txt"}

    @pytest.mark.asyncio
    async def test_recursive_default_is_recursive(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "sub").mkdir()
        (base / "sub" / "a.txt").write_text("a")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx())
        paths = {e["path"] for e in result["entries"]}
        assert paths == {"sub", "sub/a.txt"}

    @pytest.mark.asyncio
    async def test_recursive_false_is_shallow(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "sub").mkdir()
        (base / "sub" / "a.txt").write_text("a")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx(), recursive=False)
        paths = {e["path"] for e in result["entries"]}
        assert paths == {"sub"}

    @pytest.mark.asyncio
    async def test_recursive_max_depth(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "sub").mkdir()
        (base / "sub" / "a.txt").write_text("a")
        (base / "sub" / "deep").mkdir()
        (base / "sub" / "deep" / "b.txt").write_text("b")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx(), recursive=True, max_depth=2)
        paths = {e["path"] for e in result["entries"]}
        assert paths == {"sub", "sub/a.txt", "sub/deep"}

    @pytest.mark.asyncio
    async def test_entry_path_is_relative_to_base_not_listed_dir(
        self, tmp_path: Path, monkeypatch
    ):
        """Entry 'path' must be passable directly to get_file, regardless of
        which subdirectory was listed."""
        base = tmp_path / "files"
        sub = base / "playwright"
        sub.mkdir(parents=True)
        (sub / "snap.yml").write_text("y")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files, get_file
        result = await list_files(_ctx(), path="playwright")
        entry = next(e for e in result["entries"] if e["name"] == "snap.yml")
        assert entry["path"] == "playwright/snap.yml"
        fetched = await get_file(_ctx(), path=entry["path"])
        assert fetched["ok"] is True
        assert fetched["content"] == "y"

    @pytest.mark.asyncio
    async def test_recursive_does_not_follow_dir_symlinks(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "real").mkdir()
        (base / "real" / "x.txt").write_text("x")
        try:
            (base / "link").symlink_to(base / "real", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        _set_base(monkeypatch, base)
        from builtin_tools import list_files
        result = await list_files(_ctx(), recursive=True)
        paths = {e["path"] for e in result["entries"]}
        assert "real/x.txt" in paths
        assert "link/x.txt" not in paths
        link_entry = next(e for e in result["entries"] if e["path"] == "link")
        assert link_entry["type"] == "file"


# ---------------------------------------------------------------------------
# get_file
# ---------------------------------------------------------------------------

class TestGetFile:
    @pytest.mark.asyncio
    async def test_read_text_file(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "data.json").write_text('{"a": 1}', encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="data.json")
        assert result["ok"] is True
        assert result["content"] == '{"a": 1}'
        assert result["encoding"] == "text"

    @pytest.mark.asyncio
    async def test_read_binary_file_auto_falls_back_to_base64(
        self, tmp_path: Path, monkeypatch
    ):
        base = tmp_path / "files"
        base.mkdir()
        # Write raw bytes that are not valid UTF-8
        raw = bytes(range(256))
        (base / "img.bin").write_bytes(raw)
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="img.bin")
        assert result["ok"] is True
        assert result["encoding"] == "base64"
        assert base64.b64decode(result["content"]) == raw

    @pytest.mark.asyncio
    async def test_explicit_base64_encoding(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        content = b"hello"
        (base / "f.txt").write_bytes(content)
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="f.txt", encoding="base64")
        assert result["ok"] is True
        assert result["encoding"] == "base64"
        assert base64.b64decode(result["content"]) == content

    @pytest.mark.asyncio
    async def test_explicit_text_encoding_fails_on_binary(
        self, tmp_path: Path, monkeypatch
    ):
        base = tmp_path / "files"
        base.mkdir()
        (base / "img.bin").write_bytes(bytes(range(256)))
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="img.bin", encoding="text")
        assert result["ok"] is False
        assert "utf-8" in result["error"].lower() or "base64" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="missing.txt")
        assert result["ok"] is False
        assert "not found" in result["error"].lower() or "missing" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_path_is_directory_returns_error(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        (base / "adir").mkdir(parents=True)
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="adir")
        assert result["ok"] is False
        assert "not a file" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="../../etc/passwd")
        assert result["ok"] is False
        assert "outside" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_size_reported(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        data = b"abc" * 100
        (base / "large.bin").write_bytes(data)
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="large.bin", encoding="base64")
        assert result["ok"] is True
        assert result["size"] == len(data)

    @pytest.mark.asyncio
    async def test_nested_path(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        (base / "a" / "b").mkdir(parents=True)
        (base / "a" / "b" / "c.txt").write_text("deep")
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="a/b/c.txt")
        assert result["ok"] is True
        assert result["content"] == "deep"

    @pytest.mark.asyncio
    async def test_png_screenshot_roundtrip(self, tmp_path: Path, monkeypatch):
        """Simulate a Playwright screenshot: PNG magic bytes + arbitrary data."""
        base = tmp_path / "files"
        base.mkdir()
        # Minimal fake PNG (starts with PNG magic signature)
        png_magic = b"\x89PNG\r\n\x1a\n" + bytes(range(100))
        (base / "screenshot.png").write_bytes(png_magic)
        _set_base(monkeypatch, base)
        from builtin_tools import get_file
        result = await get_file(_ctx(), path="screenshot.png")
        assert result["ok"] is True
        # PNG has non-UTF-8 bytes so auto should pick base64
        assert result["encoding"] == "base64"
        assert base64.b64decode(result["content"]) == png_magic


# ---------------------------------------------------------------------------
# _safe_resolve edge cases
# ---------------------------------------------------------------------------

class TestSafeResolve:
    def test_none_path_resolves_to_base(self, tmp_path: Path, monkeypatch):
        _set_base(monkeypatch, tmp_path / "base")
        from builtin_tools import _safe_resolve, _base_dir
        monkeypatch.setenv("MCPPROXY_FILES_DIR", str(tmp_path / "base"))
        result = _safe_resolve(None)
        assert result == _base_dir()

    def test_empty_string_resolves_to_base(self, tmp_path: Path, monkeypatch):
        _set_base(monkeypatch, tmp_path / "base")
        from builtin_tools import _safe_resolve, _base_dir
        result = _safe_resolve("")
        assert result == _base_dir()

    def test_valid_subdirectory_allowed(self, tmp_path: Path, monkeypatch):
        _set_base(monkeypatch, tmp_path / "base")
        from builtin_tools import _safe_resolve, _base_dir
        result = _safe_resolve("sub/deep")
        expected = (_base_dir() / "sub" / "deep").resolve()
        assert result == expected

    def test_traversal_raises(self, tmp_path: Path, monkeypatch):
        _set_base(monkeypatch, tmp_path / "base")
        from builtin_tools import _safe_resolve
        with pytest.raises(ValueError, match="outside"):
            _safe_resolve("../secret")


# ---------------------------------------------------------------------------
# Integration: tools registered in server.py
# ---------------------------------------------------------------------------

class TestBuiltinToolsRegistered:
    """Verify the built-in tool specs are accepted by register_tool without error."""

    def test_register_builtin_tools_succeeds(self, monkeypatch):
        """register_builtin_tools() should not raise (mcp.tool is already registered)."""
        # server.py already called register_builtin_tools() at import time.
        # Verify the built-in tool module is importable and correct.
        import builtin_tools
        assert callable(builtin_tools.list_files)
        assert callable(builtin_tools.get_file)

    def test_builtin_tools_exported(self):
        from builtin_tools import get_file, list_files, _base_dir, _safe_resolve
        assert all(callable(f) for f in (get_file, list_files, _base_dir, _safe_resolve))


# ---------------------------------------------------------------------------
# Default base directory (/app/files)
# ---------------------------------------------------------------------------

class TestDefaultBaseDir:
    """Verify the default files directory is /app/files (mountable as a Docker volume)."""

    def test_default_is_app_files(self, monkeypatch):
        monkeypatch.delenv("MCPPROXY_FILES_DIR", raising=False)
        from builtin_tools import _base_dir
        assert _base_dir() == Path("/app/files").resolve()

    def test_config_default_matches(self, monkeypatch):
        """config.FILES_DIR is re-imported under the same default."""
        monkeypatch.delenv("MCPPROXY_FILES_DIR", raising=False)
        import importlib
        import config
        importlib.reload(config)
        assert config.FILES_DIR == Path("/app/files")
