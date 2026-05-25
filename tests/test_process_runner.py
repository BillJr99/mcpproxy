"""Tests for process_runner.py — cwd threading and session keying."""
import process_runner


class TestProcessSessionCwd:
    def test_session_stores_cwd(self):
        s = process_runner.ProcessSession("echo hi", cwd="/some/path")
        assert s.cwd == "/some/path"

    def test_default_cwd_is_none(self):
        s = process_runner.ProcessSession("echo hi")
        assert s.cwd is None


class TestSessionRegistry:
    def setup_method(self):
        process_runner._sessions.clear()

    def teardown_method(self):
        process_runner._sessions.clear()

    def test_same_command_same_cwd_returns_same_session(self):
        a = process_runner.get_session("echo hi", cwd="/a")
        b = process_runner.get_session("echo hi", cwd="/a")
        assert a is b

    def test_same_command_different_cwd_are_distinct(self):
        a = process_runner.get_session("echo hi", cwd="/a")
        b = process_runner.get_session("echo hi", cwd="/b")
        assert a is not b
        assert a.cwd == "/a"
        assert b.cwd == "/b"

    def test_no_cwd_is_distinct_from_explicit_none(self):
        # Both None — should be the same session
        a = process_runner.get_session("echo hi")
        b = process_runner.get_session("echo hi", cwd=None)
        assert a is b
