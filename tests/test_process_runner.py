"""Tests for process_runner.py — cwd threading and session keying."""
import asyncio
import asyncio.streams
import json
from types import SimpleNamespace

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


class TestStreamLimit:
    """One MCP message is one line, and asyncio caps a line at 64 KiB by
    default — which silently makes any server with a large tools/list unusable."""

    @staticmethod
    def _reader(limit: int) -> "asyncio.StreamReader":
        import asyncio

        return asyncio.StreamReader(limit=limit)

    @pytest.mark.asyncio
    async def test_reads_a_line_larger_than_the_asyncio_default(self):
        import asyncio.streams

        size = asyncio.streams._DEFAULT_LIMIT * 3
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"x": "y" * size}})
        reader = self._reader(process_runner.STREAM_LIMIT)
        reader.feed_data(payload.encode() + b"\n")

        session = process_runner.ProcessSession("irrelevant")
        session._proc = SimpleNamespace(stdout=reader)
        message = await session._recv(timeout=5)
        assert len(message["result"]["x"]) == size

    @pytest.mark.asyncio
    async def test_default_asyncio_limit_would_have_failed(self):
        # Guards the premise: without the raised limit this is the exact failure
        # the Asana bridge hit ("chunk is longer than limit").
        import asyncio.streams

        reader = self._reader(asyncio.streams._DEFAULT_LIMIT)
        reader.feed_data(b"z" * (asyncio.streams._DEFAULT_LIMIT * 3) + b"\n")
        with pytest.raises(ValueError, match="limit"):
            await reader.readline()

    @pytest.mark.asyncio
    async def test_oversized_line_error_names_the_knob(self):
        # asyncio's own wording names no remedy; ours has to.
        reader = self._reader(1024)
        reader.feed_data(b"z" * 8192 + b"\n")

        session = process_runner.ProcessSession("irrelevant")
        session._proc = SimpleNamespace(stdout=reader)
        with pytest.raises(RuntimeError, match="MCPPROXY_STREAM_LIMIT"):
            await session._recv(timeout=5)

    @pytest.mark.asyncio
    async def test_spawn_passes_the_raised_limit(self, monkeypatch):
        # The reader tests above prove _recv copes; this proves the spawn is
        # actually created with the raised ceiling rather than asyncio's default.
        seen = {}

        async def fake_exec(*args, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        session = process_runner.ProcessSession("npx -y mcp-remote https://x/mcp")
        with pytest.raises(RuntimeError, match="stop here"):
            await session._start()
        assert seen["limit"] == process_runner.STREAM_LIMIT
        assert process_runner.STREAM_LIMIT > asyncio.streams._DEFAULT_LIMIT


class TestAuthUrlNoiseFiltering:
    """Discovery and error lines mention OAuth but are not a prompt to visit
    anything.  Publishing the issuer base as the authorization URL gave the UI
    a dead link and hid the real failure."""

    @pytest.mark.parametrize(
        "line",
        [
            "[1093] Discovered authorization server: https://github.com/login/oauth",
            "[1093] Discovering OAuth server configuration...",
            "[1093] Warning: Environment variable 'GITHUB_MCP_AUTH_HEADER' not found "
            "for header 'Authorization'.",
            "[1093] Connection error: Error: Incompatible auth server: does not "
            "support dynamic client registration",
            "  errorUri: 'https://asana.com/developers/documentation/getting-started/"
            "authentication'",
        ],
    )
    def test_rejects_discovery_and_error_lines(self, line):
        assert process_runner._extract_auth_url(line) is None

    @pytest.mark.parametrize(
        "line,expected",
        [
            (
                "Please authorize this client by visiting: "
                "https://app.asana.com/-/oauth_authorize?client_id=1",
                "https://app.asana.com/-/oauth_authorize?client_id=1",
            ),
            (
                "If your browser does not open, open this URL: https://example.com/oauth?x=1",
                "https://example.com/oauth?x=1",
            ),
        ],
    )
    def test_still_captures_real_prompts(self, line, expected):
        assert process_runner._extract_auth_url(line) == expected


class TestFailureClassification:
    def test_missing_header_variable(self):
        message = process_runner._classify_failure([
            "[1093] Warning: Environment variable 'GITHUB_MCP_AUTH_HEADER' not found "
            "for header 'Authorization'.",
        ])
        assert "GITHUB_MCP_AUTH_HEADER" in message
        assert "Authorization" in message
        assert "package.env_keys" in message

    def test_root_cause_beats_the_downstream_error(self):
        # A missing header variable makes mcp-remote fall back to OAuth and then
        # fail on dynamic client registration.  Reporting that second error would
        # send the user off to register a client they do not need.
        message = process_runner._classify_failure([
            "[1093] Warning: Environment variable 'GITHUB_MCP_AUTH_HEADER' not found "
            "for header 'Authorization'.",
            "[1093] Fatal error: Error: Incompatible auth server: does not support "
            "dynamic client registration",
        ])
        assert "GITHUB_MCP_AUTH_HEADER" in message

    def test_no_dynamic_client_registration(self):
        message = process_runner._classify_failure([
            "Fatal error: Error: Incompatible auth server: does not support "
            "dynamic client registration",
        ])
        assert "--static-oauth-client-info" in message

    def test_pkce_mismatch(self):
        message = process_runner._classify_failure([
            "[34] Fatal error: InvalidGrantError: The PKCE code_verifier does not "
            "match the stored code challenge.",
        ])
        assert "different authorization attempt" in message

    def test_unrecognised_failure(self):
        assert process_runner._classify_failure(["segmentation fault"]) is None


class TestBridgeErrors:
    def teardown_method(self):
        process_runner.bridge_errors.clear()

    @pytest.mark.asyncio
    async def test_records_an_actionable_reason_when_a_spawn_dies(self):
        cmd = "npx -y mcp-remote https://api.githubcopilot.com/mcp/"
        session = process_runner.ProcessSession(cmd)
        session._stderr_tail = [
            "Warning: Environment variable 'GITHUB_MCP_AUTH_HEADER' not found "
            "for header 'Authorization'.",
        ]

        async def boom():
            raise EOFError("MCP process closed stdout")

        session._start_inner = boom
        with pytest.raises(EOFError):
            await session._start()
        assert "GITHUB_MCP_AUTH_HEADER" in process_runner.bridge_errors[cmd]

    @pytest.mark.asyncio
    async def test_falls_back_to_the_last_stderr_line(self):
        cmd = "npx -y mcp-remote https://example.com/mcp"
        session = process_runner.ProcessSession(cmd)
        session._stderr_tail = ["something nobody has a pattern for"]

        async def boom():
            raise EOFError("closed")

        session._start_inner = boom
        with pytest.raises(EOFError):
            await session._start()
        assert process_runner.bridge_errors[cmd] == "something nobody has a pattern for"


class TestConcurrentSpawnGuard:
    """Two mcp-remote processes for one server share the PKCE verifier cached
    under MCP_REMOTE_CONFIG_DIR and overwrite each other."""

    def teardown_method(self):
        process_runner._spawning.clear()

    @pytest.mark.asyncio
    async def test_refuses_a_second_introspect_of_the_same_command(self):
        cmd = "npx -y mcp-remote https://mcp.asana.com/v2/mcp 8887"
        process_runner._spawning.add(cmd)
        with pytest.raises(process_runner.ConcurrentSpawnError, match="already starting"):
            await process_runner.introspect(cmd)

    @pytest.mark.asyncio
    async def test_releases_the_guard_when_a_spawn_fails(self):
        cmd = "definitely-not-a-real-binary-xyz"
        with pytest.raises(Exception):
            await process_runner.introspect(cmd)
        assert cmd not in process_runner._spawning

    @pytest.mark.asyncio
    async def test_a_different_command_is_unaffected(self):
        process_runner._spawning.add("npx -y mcp-remote https://a/mcp")
        with pytest.raises(RuntimeError, match="Failed to introspect"):
            await process_runner.introspect("definitely-not-a-real-binary-xyz")


class TestSendToDeadProcess:
    """A bridge that fails fatally during startup loses its stdin between spawn
    and the initialize request. The transport's own error names an internal
    uvloop handle and explains nothing."""

    @pytest.mark.asyncio
    async def test_reports_the_real_cause_with_the_stderr_tail(self):
        session = process_runner.ProcessSession("irrelevant")
        session._parts = [
            "python3",
            "-c",
            "import sys; sys.stderr.write('Fatal error: Incompatible auth server: "
            "does not support dynamic client registration\\n'); sys.exit(1)",
        ]
        session._proc = await asyncio.create_subprocess_exec(
            *session._parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=process_runner.STREAM_LIMIT,
        )
        session._start_stderr_reader()
        await session._proc.wait()
        await asyncio.sleep(0.2)
        session._proc.stdin.close()
        await asyncio.sleep(0.1)

        with pytest.raises(EOFError) as exc:
            await session._send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        message = str(exc.value)
        assert "exited before it could be initialized" in message
        assert "dynamic client registration" in message
        # No uvloop transport internals leaking through the chain.
        assert exc.value.__cause__ is None
