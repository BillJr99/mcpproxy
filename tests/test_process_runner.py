"""Tests for process_runner.py — cwd threading and session keying."""
import pytest

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


class TestIntrospectStderrCapture:
    """When the subprocess crashes during the handshake, the error message
    should include stderr so the user can see the cause.  stderr is consumed by
    a background reader into ``_stderr_tail``; ``_drain_stderr_tail`` reports it."""

    @pytest.mark.asyncio
    async def test_eof_error_includes_stderr_tail(self):
        class FakeStdout:
            async def readline(self):
                return b""

        session = process_runner.ProcessSession("does-not-matter")
        # Simulate what the background reader would have captured.
        session._stderr_tail = ["Environment validation failed: KEY: Required"]
        class _Proc:
            stdout = FakeStdout()
            stderr = None
        session._proc = _Proc()

        with pytest.raises(EOFError) as exc_info:
            await session._recv(timeout=1.0)
        assert "Environment validation failed" in str(exc_info.value)


class TestAuthUrlExtraction:
    """mcp-remote prints an OAuth authorization URL to stderr; we scrape it so
    the UI can offer a clickable Authorize link."""

    def test_extracts_url_from_authorize_prompt(self):
        line = "Please authorize this client by visiting: https://app.asana.com/-/oauth_authorize?client_id=123"
        url = process_runner._extract_auth_url(line)
        assert url == "https://app.asana.com/-/oauth_authorize?client_id=123"

    def test_extracts_url_from_open_this_url_prompt(self):
        line = "If your browser does not open, open this URL: https://example.com/oauth?x=1"
        assert process_runner._extract_auth_url(line) == "https://example.com/oauth?x=1"

    def test_ignores_unrelated_lines_with_urls(self):
        # A URL with no authorization hint must not be treated as an auth prompt.
        assert process_runner._extract_auth_url("Fetching https://mcp.asana.com/v2/mcp") is None

    def test_ignores_hint_without_url(self):
        assert process_runner._extract_auth_url("authorization pending…") is None


class TestCallbackPortExtraction:
    """mcp-remote announces the loopback port it binds; the manual-callback
    replay aims at whatever it says, which is the only way to know the port when
    the provider YAML leaves it for mcp-remote to choose."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Using specified callback port: 8887", 8887),
            ("Using automatically selected callback port: 3334", 3334),
            ("OAuth callback server listening at http://127.0.0.1:8887", 8887),
            ("Callback server port: 65535", 65535),
        ],
    )
    def test_extracts_announced_port(self, line, expected):
        assert process_runner._extract_callback_port(line) == expected

    def test_ignores_non_loopback_urls(self):
        # The provider's own authorize URL must never be read as a local port.
        assert process_runner._extract_callback_port(
            "Please authorize by visiting: https://app.asana.com:443/-/oauth"
        ) is None

    @pytest.mark.parametrize(
        "line", ["booting mcp-remote", "callback port: 0", "callback port: 70000"]
    )
    def test_returns_none_otherwise(self, line):
        assert process_runner._extract_callback_port(line) is None


class TestAuthTimeoutFromCommand:
    """mcp-remote holds the handshake open for its own --auth-timeout; giving up
    first kills the callback listener out from under the user."""

    @pytest.mark.parametrize(
        "command,expected",
        [
            ("npx -y mcp-remote https://x/mcp 8887 --auth-timeout 600", 600.0),
            ("npx -y mcp-remote https://x/mcp --auth-timeout=600", 600.0),
        ],
    )
    def test_reads_the_declared_timeout(self, command, expected):
        assert process_runner._auth_timeout_from_command(command.split()) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "npx -y mcp-remote https://x/mcp 8887",
            "npx -y mcp-remote https://x/mcp --auth-timeout abc",
            "npx -y mcp-remote https://x/mcp --auth-timeout -5",
            "npx -y mcp-remote https://x/mcp --auth-timeout 999999",
        ],
    )
    def test_returns_none_for_absent_or_nonsense_values(self, command):
        assert process_runner._auth_timeout_from_command(command.split()) is None

    def test_session_waits_at_least_as_long_as_the_bridge(self):
        session = process_runner.ProcessSession(
            "npx -y mcp-remote https://x/mcp 8887 --auth-timeout 600"
        )
        assert session.init_timeout >= 630

    def test_session_falls_back_to_the_configured_default(self):
        session = process_runner.ProcessSession("npx @playwright/mcp@latest")
        assert session.init_timeout == process_runner.AUTH_INIT_TIMEOUT

    def test_declared_timeout_never_shortens_the_wait(self):
        # The env var stays a floor: a tiny --auth-timeout must not undercut it.
        session = process_runner.ProcessSession(
            "npx -y mcp-remote https://x/mcp 8887 --auth-timeout 5"
        )
        assert session.init_timeout == process_runner.AUTH_INIT_TIMEOUT


class TestConsumeStderr:
    def teardown_method(self):
        process_runner.pending_auth_urls.clear()
        process_runner.callback_listener_ports.clear()

    @pytest.mark.asyncio
    async def test_consume_stderr_captures_auth_url_and_tail(self):
        lines = [
            b"booting mcp-remote\n",
            b"Please authorize by visiting: https://app.asana.com/-/oauth_authorize?c=1\n",
            b"",  # EOF
        ]

        class FakeStderr:
            async def readline(self):
                return lines.pop(0) if lines else b""

        cmd = "npx -y mcp-remote https://mcp.asana.com/v2/mcp"
        session = process_runner.ProcessSession(cmd)
        class _Proc:
            stderr = FakeStderr()
        session._proc = _Proc()

        await session._consume_stderr()

        assert session.pending_auth_url == "https://app.asana.com/-/oauth_authorize?c=1"
        # Exposed in the shared registry the UI polls, keyed by command.
        assert process_runner.pending_auth_urls[cmd] == "https://app.asana.com/-/oauth_authorize?c=1"
        # Tail retains the captured lines for crash diagnostics.
        assert "booting mcp-remote" in "\n".join(session._stderr_tail)

    @pytest.mark.asyncio
    async def test_callback_listener_url_does_not_replace_authorization_url(self):
        lines = [
            b"Please authorize by visiting: https://app.asana.com/-/oauth_authorize?c=1\n",
            b"OAuth callback server listening at http://127.0.0.1:8887\n",
            b"",
        ]

        class FakeStderr:
            async def readline(self):
                return lines.pop(0) if lines else b""

        cmd = "npx -y mcp-remote https://mcp.asana.com/v2/mcp"
        session = process_runner.ProcessSession(cmd)

        class _Proc:
            stderr = FakeStderr()

        session._proc = _Proc()
        await session._consume_stderr()

        expected = "https://app.asana.com/-/oauth_authorize?c=1"
        assert session.pending_auth_url == expected
        assert process_runner.pending_auth_urls[cmd] == expected
        # The loopback URL is no longer merely discarded: its port is recorded
        # so a pasted callback knows where to be delivered.
        assert process_runner.callback_listener_ports[cmd] == 8887

    @pytest.mark.asyncio
    async def test_port_announcement_alone_does_not_look_like_an_auth_prompt(self):
        lines = [
            b"Using automatically selected callback port: 3334\n",
            b"",
        ]

        class FakeStderr:
            async def readline(self):
                return lines.pop(0) if lines else b""

        cmd = "npx -y mcp-remote https://mcp.linear.app/sse"
        session = process_runner.ProcessSession(cmd)

        class _Proc:
            stderr = FakeStderr()

        session._proc = _Proc()
        await session._consume_stderr()

        assert process_runner.callback_listener_ports[cmd] == 3334
        assert cmd not in process_runner.pending_auth_urls

    @pytest.mark.asyncio
    async def test_clear_pending_auth_removes_registry_entry(self):
        cmd = "some-cmd"
        session = process_runner.ProcessSession(cmd)
        session.pending_auth_url = "https://x/oauth"
        process_runner.pending_auth_urls[cmd] = "https://x/oauth"
        session._clear_pending_auth()
        assert session.pending_auth_url is None
        assert cmd not in process_runner.pending_auth_urls
