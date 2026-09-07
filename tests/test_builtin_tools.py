"""Tests for builtin_tools.py — mcpproxy__listfiles / getfile / deletefile.

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
        """Entry 'path' must be passable directly to get_file regardless of
        which subdirectory was listed. Writes one file at the base and one in
        a nested subdir, then round-trips both through list_files -> get_file
        using only the returned 'path' field."""
        base = tmp_path / "files"
        sub = base / "test-paths"
        sub.mkdir(parents=True)
        root_file = base / "root.txt"
        root_file.write_text("root-content")
        nested_file = sub / "nested.txt"
        nested_file.write_text("nested-content")

        _set_base(monkeypatch, base)
        from builtin_tools import list_files, get_file

        # Listing a subdirectory: 'path' should still be base-relative.
        sub_listing = await list_files(_ctx(), path="test-paths")
        nested_entry = next(
            e for e in sub_listing["entries"] if e["name"] == "nested.txt"
        )
        assert nested_entry["path"] == "test-paths/nested.txt"
        fetched_nested = await get_file(_ctx(), path=nested_entry["path"])
        assert fetched_nested["ok"] is True
        assert fetched_nested["content"] == "nested-content"

        # Recursive listing from the root: every entry's 'path' must round-trip
        # through get_file unchanged for files (directories return an error).
        root_listing = await list_files(_ctx(), recursive=True)
        paths = {e["path"] for e in root_listing["entries"]}
        assert paths == {"root.txt", "test-paths", "test-paths/nested.txt"}
        for entry in root_listing["entries"]:
            if entry["type"] != "file":
                continue
            fetched = await get_file(_ctx(), path=entry["path"])
            assert fetched["ok"] is True, f"failed to fetch {entry['path']}"
        # Cross-check the root file specifically.
        root_entry = next(e for e in root_listing["entries"] if e["path"] == "root.txt")
        fetched_root = await get_file(_ctx(), path=root_entry["path"])
        assert fetched_root["content"] == "root-content"

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
# delete_file
# ---------------------------------------------------------------------------

class TestDeleteFile:
    @pytest.mark.asyncio
    async def test_delete_text_file(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "data.json").write_text('{"a": 1}', encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="data.json")
        assert result["ok"] is True
        assert result["deleted"] is True
        assert result["type"] == "file"
        assert result["path"] == "data.json"
        assert not (base / "data.json").exists()

    @pytest.mark.asyncio
    async def test_size_reported(self, tmp_path: Path, monkeypatch):
        """Size is captured before the unlink, so it survives the deletion."""
        base = tmp_path / "files"
        base.mkdir()
        data = b"abc" * 100
        (base / "large.bin").write_bytes(data)
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="large.bin")
        assert result["ok"] is True
        assert result["size"] == len(data)

    @pytest.mark.asyncio
    async def test_delete_binary_file(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "img.bin").write_bytes(bytes(range(256)))
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="img.bin")
        assert result["ok"] is True
        assert not (base / "img.bin").exists()

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="missing.txt")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_empty_directory(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        (base / "empty").mkdir(parents=True)
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="empty")
        assert result["ok"] is True
        assert result["type"] == "directory"
        assert result["size"] == 0
        assert not (base / "empty").exists()

    @pytest.mark.asyncio
    async def test_non_empty_directory_refused(self, tmp_path: Path, monkeypatch):
        """A directory with contents is refused — and its contents survive."""
        base = tmp_path / "files"
        (base / "adir").mkdir(parents=True)
        (base / "adir" / "keep.txt").write_text("keep me", encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="adir")
        assert result["ok"] is False
        assert "not empty" in result["error"].lower()
        assert (base / "adir").is_dir()
        assert (base / "adir" / "keep.txt").read_text(encoding="utf-8") == "keep me"

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, tmp_path: Path, monkeypatch):
        """Traversal is refused AND the outside file is still on disk."""
        base = tmp_path / "files"
        base.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="../secret.txt")
        assert result["ok"] is False
        assert "outside" in result["error"].lower()
        assert outside.exists()

    @pytest.mark.asyncio
    async def test_empty_path_refuses_base_dir(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="")
        assert result["ok"] is False
        assert "base directory" in result["error"].lower()
        assert base.is_dir()

    @pytest.mark.asyncio
    async def test_dot_path_refuses_base_dir(self, tmp_path: Path, monkeypatch):
        """'.' is the other spelling of the same resolved path."""
        base = tmp_path / "files"
        base.mkdir()
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path=".")
        assert result["ok"] is False
        assert "base directory" in result["error"].lower()
        assert base.is_dir()

    @pytest.mark.asyncio
    async def test_nested_path(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        (base / "a" / "b").mkdir(parents=True)
        (base / "a" / "b" / "c.txt").write_text("deep", encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="a/b/c.txt")
        assert result["ok"] is True
        assert not (base / "a" / "b" / "c.txt").exists()
        # The containing directories are left alone
        assert (base / "a" / "b").is_dir()

    @pytest.mark.asyncio
    async def test_siblings_untouched(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        for name in ("one.txt", "two.txt", "three.txt"):
            (base / name).write_text(name, encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="two.txt")
        assert result["ok"] is True
        assert not (base / "two.txt").exists()
        assert (base / "one.txt").exists()
        assert (base / "three.txt").exists()

    @pytest.mark.asyncio
    async def test_second_delete_reports_not_found(self, tmp_path: Path, monkeypatch):
        base = tmp_path / "files"
        base.mkdir()
        (base / "once.txt").write_text("x", encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        first = await delete_file(_ctx(), path="once.txt")
        second = await delete_file(_ctx(), path="once.txt")
        assert first["ok"] is True
        assert second["ok"] is False
        assert "not found" in second["error"].lower()

    @pytest.mark.asyncio
    async def test_symlink_escape_rejected(self, tmp_path: Path, monkeypatch):
        """A symlink pointing outside the base resolves out and is refused."""
        base = tmp_path / "files"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            (base / "link.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file
        result = await delete_file(_ctx(), path="link.txt")
        assert result["ok"] is False
        assert "outside" in result["error"].lower()
        assert outside.exists()

    @pytest.mark.asyncio
    async def test_list_get_delete_roundtrip(self, tmp_path: Path, monkeypatch):
        """The 'path' from listfiles feeds getfile and deletefile unchanged."""
        base = tmp_path / "files"
        (base / "playwright").mkdir(parents=True)
        (base / "playwright" / "shot.txt").write_text("pixels", encoding="utf-8")
        _set_base(monkeypatch, base)
        from builtin_tools import delete_file, get_file, list_files

        listing = await list_files(_ctx())
        entry = next(e for e in listing["entries"] if e["type"] == "file")
        assert entry["path"] == "playwright/shot.txt"

        read = await get_file(_ctx(), path=entry["path"])
        assert read["ok"] is True
        assert read["content"] == "pixels"

        removed = await delete_file(_ctx(), path=entry["path"])
        assert removed["ok"] is True

        after = await list_files(_ctx())
        assert entry["path"] not in [e["path"] for e in after["entries"]]


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
        assert callable(builtin_tools.delete_file)

    def test_builtin_tools_exported(self):
        from builtin_tools import (
            delete_file,
            get_file,
            list_files,
            _base_dir,
            _safe_resolve,
        )
        assert all(
            callable(f)
            for f in (delete_file, get_file, list_files, _base_dir, _safe_resolve)
        )


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
