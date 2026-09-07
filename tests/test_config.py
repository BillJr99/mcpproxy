"""Tests for config.py — .env parsing, and keeping os.environ in step.

The parser here is the single one every reader uses (this module, the Secrets
UI, ProcessSession._build_env) and it has to agree with the shell that sources
the same file, so its rules are pinned rather than left to each call site.

refresh_env() exists because os.environ was previously populated once at start
and never again: a credential rotated through the Secrets UI wrote the file and
changed nothing, so it kept failing until the container was restarted.
"""
import os
from pathlib import Path

import pytest

import config


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point config at a scratch env file and start from a clean cache."""
    env_file = tmp_path / ".env"
    monkeypatch.setenv("MCP_ENV_FILE", str(env_file))
    config.reset_env_cache()
    yield env_file
    config.reset_env_cache()


# ---------------------------------------------------------------------------
# read_env_file
# ---------------------------------------------------------------------------

class TestReadEnvFile:
    """The canonical parser. Every reader of .env must agree with this one."""

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert config.read_env_file(tmp_path / "nope.env") == {}

    def test_basic_key_value(self, _isolated_env):
        _isolated_env.write_text("A=1\nB=two\n")
        assert config.read_env_file(_isolated_env) == {"A": "1", "B": "two"}

    def test_last_assignment_wins(self, _isolated_env):
        """The shell and docker-compose both take the last one; so do we."""
        _isolated_env.write_text("A=first\nA=second\n")
        assert config.read_env_file(_isolated_env)["A"] == "second"

    def test_only_the_first_equals_splits(self, _isolated_env):
        _isolated_env.write_text("A=x=y=z\n")
        assert config.read_env_file(_isolated_env)["A"] == "x=y=z"

    def test_comments_and_blanks_skipped(self, _isolated_env):
        _isolated_env.write_text("# note\n\nA=1\n\n#B=2\n")
        assert config.read_env_file(_isolated_env) == {"A": "1"}

    def test_line_without_equals_skipped(self, _isolated_env):
        _isolated_env.write_text("garbage\nA=1\n")
        assert config.read_env_file(_isolated_env) == {"A": "1"}

    def test_key_whitespace_stripped(self, _isolated_env):
        _isolated_env.write_text("  A  =1\n")
        assert config.read_env_file(_isolated_env)["A"] == "1"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ('A="has space"', "has space"),
            ("A='single'", "single"),
            ('A="has\\"quote"', 'has"quote'),
            ('A="has\\$dollar"', "has$dollar"),
            ("A=plain", "plain"),
            ('A=""', ""),
        ],
    )
    def test_quoting_round_trip(self, _isolated_env, raw, expected):
        _isolated_env.write_text(raw + "\n")
        assert config.read_env_file(_isolated_env)["A"] == expected

    def test_matches_the_previous_frontend_parser(self, _isolated_env):
        """Unifying the parsers must not change behaviour."""
        from frontend.app import _read_env_file
        _isolated_env.write_text(
            '# c\n\nA=1\nB="a b"\nC=x=y\nA=2\njunk\n  D  =4\n'
        )
        assert _read_env_file(_isolated_env) == config.read_env_file(_isolated_env)


# ---------------------------------------------------------------------------
# refresh_env
# ---------------------------------------------------------------------------

class TestRefreshEnv:
    """A credential rotated in the UI must reach os.environ without a restart."""

    def test_values_reach_os_environ(self, _isolated_env, monkeypatch):
        monkeypatch.delenv("NEW_SECRET", raising=False)
        _isolated_env.write_text("NEW_SECRET=fresh\n")
        assert config.refresh_env() == ["NEW_SECRET"]
        assert os.environ["NEW_SECRET"] == "fresh"

    def test_unchanged_file_reports_nothing(self, _isolated_env):
        _isolated_env.write_text("A=1\n")
        config.refresh_env()
        assert config.refresh_env() == []

    def test_rotated_value_is_picked_up(self, _isolated_env, monkeypatch):
        monkeypatch.delenv("TOKEN", raising=False)
        _isolated_env.write_text("TOKEN=old\n")
        config.refresh_env()
        _isolated_env.write_text("TOKEN=new\n")
        assert config.refresh_env() == ["TOKEN"]
        assert os.environ["TOKEN"] == "new"

    def test_force_rereads_even_when_stamp_matches(self, _isolated_env, monkeypatch):
        """A save inside one filesystem tick must not be missed."""
        monkeypatch.delenv("TOKEN", raising=False)
        _isolated_env.write_text("TOKEN=old\n")
        config.refresh_env()
        os.environ["TOKEN"] = "clobbered"
        assert config.refresh_env(force=True) == ["TOKEN"]
        assert os.environ["TOKEN"] == "old"

    def test_key_removed_from_file_is_retired(self, _isolated_env, monkeypatch):
        monkeypatch.delenv("GONE", raising=False)
        _isolated_env.write_text("GONE=x\nKEEP=y\n")
        config.refresh_env()
        _isolated_env.write_text("KEEP=y\n")
        assert config.refresh_env() == ["GONE"]
        assert "GONE" not in os.environ
        assert os.environ["KEEP"] == "y"

    def test_removal_does_not_clobber_a_value_set_elsewhere(
        self, _isolated_env, monkeypatch
    ):
        """Only retire what we set and nobody has overwritten since."""
        monkeypatch.delenv("SHARED", raising=False)
        _isolated_env.write_text("SHARED=fromfile\n")
        config.refresh_env()
        os.environ["SHARED"] = "set-by-something-else"
        _isolated_env.write_text("")
        config.refresh_env()
        assert os.environ["SHARED"] == "set-by-something-else"

    def test_missing_file_is_not_an_error(self, _isolated_env):
        assert config.refresh_env() == []

    def test_returns_names_never_values(self, _isolated_env, monkeypatch):
        """The return value is safe to log; the secret must not be in it."""
        monkeypatch.delenv("API_KEY", raising=False)
        _isolated_env.write_text("API_KEY=super-secret-value\n")
        changed = config.refresh_env()
        assert changed == ["API_KEY"]
        assert "super-secret-value" not in repr(changed)

    def test_unreadable_file_reports_no_content(self, _isolated_env, capsys, monkeypatch):
        """A read error names the path only, never a line of the file."""
        _isolated_env.write_text("A=super-secret-value\n")
        config.refresh_env()
        _isolated_env.write_text("B=another-secret\n")

        def boom(*a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_text", boom)
        assert config.refresh_env(force=True) == []
        out = capsys.readouterr().out
        assert "another-secret" not in out and "super-secret-value" not in out
