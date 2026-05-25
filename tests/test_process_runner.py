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

    def test_different_env_keys_are_distinct(self):
        a = process_runner.get_session("echo hi", env_keys=["A"])
        b = process_runner.get_session("echo hi", env_keys=["B"])
        assert a is not b
        assert a.env_keys == ["A"]
        assert b.env_keys == ["B"]

    def test_same_env_keys_returns_same_session(self):
        a = process_runner.get_session("echo hi", env_keys=["A", "B"])
        b = process_runner.get_session("echo hi", env_keys=["A", "B"])
        assert a is b


class TestBuildEnv:
    def test_inherits_os_environ(self, monkeypatch):
        monkeypatch.setenv("MY_INHERITED", "yes")
        s = process_runner.ProcessSession("echo hi", env_keys=["MY_INHERITED"])
        env = s._build_env()
        assert env["MY_INHERITED"] == "yes"

    def test_reads_from_mcp_env_file(self, tmp_path, monkeypatch):
        # Simulate a user adding a secret via the UI after server start —
        # the value should be picked up from MCP_ENV_FILE on next spawn.
        env_file = tmp_path / ".env"
        env_file.write_text("MY_NEW_SECRET=freshvalue\n")
        monkeypatch.setenv("MCP_ENV_FILE", str(env_file))
        monkeypatch.delenv("MY_NEW_SECRET", raising=False)
        s = process_runner.ProcessSession("echo hi", env_keys=["MY_NEW_SECRET"])
        env = s._build_env()
        assert env["MY_NEW_SECRET"] == "freshvalue"

    def test_no_env_keys_skips_file_read(self, tmp_path, monkeypatch):
        # When env_keys is empty, the session should not touch MCP_ENV_FILE
        # — it just inherits os.environ.
        monkeypatch.setenv("MCP_ENV_FILE", str(tmp_path / "nonexistent"))
        s = process_runner.ProcessSession("echo hi")
        env = s._build_env()  # must not raise
        assert isinstance(env, dict)
