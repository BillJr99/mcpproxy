"""Tests for process_runner.py — cwd threading and session keying."""
import asyncio
import asyncio.streams
import json
from types import SimpleNamespace

from unittest.mock import patch

import pytest

import process_runner


class _NullStdin:
    """Accepts writes and drains instantly — the tests below exercise the read
    side, but _request has to send before it can wait."""

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        return None



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
        import asyncio

        reader = asyncio.StreamReader(limit=process_runner.STREAM_LIMIT)
        reader.feed_eof()

        session = process_runner.ProcessSession("does-not-matter")
        # Simulate what the background stderr reader would have captured.
        session._stderr_tail = ["Environment validation failed: KEY: Required"]
        session._proc = SimpleNamespace(stdout=reader, stdin=_NullStdin())
        session._start_stdout_reader()

        with pytest.raises(EOFError) as exc_info:
            await session._request("tools/list", {}, timeout=5)
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
        session._proc = SimpleNamespace(stdout=reader, stdin=_NullStdin())
        session._start_stdout_reader()
        message = await session._request("tools/list", {}, timeout=5)
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
        session._proc = SimpleNamespace(stdout=reader, stdin=_NullStdin())
        session._start_stdout_reader()
        with pytest.raises(RuntimeError, match="MCPPROXY_STREAM_LIMIT"):
            await session._request("tools/list", {}, timeout=5)

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


class TestIsSpawning:
    """The re-authorize endpoint must consult this before scheduling: introspect
    is a coroutine, so its own guard raises only when the task runs, and the
    failure would otherwise be swallowed and reported as a silent success."""

    def teardown_method(self):
        process_runner._spawning.clear()

    def test_reports_an_in_flight_spawn(self):
        cmd = "npx -y mcp-remote https://x/mcp 8887"
        assert process_runner.is_spawning(cmd) is False
        process_runner._spawning.add(cmd)
        assert process_runner.is_spawning(cmd) is True

    def test_is_specific_to_the_command(self):
        process_runner._spawning.add("command-a")
        assert process_runner.is_spawning("command-b") is False


class TestConcurrentSpawnErrorShape:
    def test_carries_the_command_without_repeating_it_in_the_message(self):
        cmd = (
            "npx -y mcp-remote https://mcp.asana.com/v2/mcp 8887 "
            "--static-oauth-client-info @/app/tools/secrets/asana/client_info.json"
        )
        exc = process_runner.ConcurrentSpawnError(cmd, "This bridge is already starting.")
        assert exc.command == cmd
        # Commands carry credential-file paths; a log line should not echo them.
        assert "client_info.json" not in str(exc)


# A minimal stdio MCP server: answers any request with an empty result and stays
# alive, so a full initialize handshake can be exercised without a real bridge.
_STUB_SERVER = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    msg = json.loads(line)\n"
    "    if 'id' in msg:\n"
    "        sys.stdout.write(json.dumps("
    "{'jsonrpc': '2.0', 'id': msg['id'], 'result': {'tools': []}}) + '\\n')\n"
    "        sys.stdout.flush()\n"
)


class TestSuccessfulHandshake:
    def teardown_method(self):
        process_runner.bridge_errors.clear()
        process_runner.authenticated_commands.clear()
        process_runner.pending_auth_urls.clear()

    async def _start_stub(self, command: str) -> process_runner.ProcessSession:
        session = process_runner.ProcessSession(command)
        session._parts = ["python3", "-c", _STUB_SERVER]
        await session._start()
        return session

    @pytest.mark.asyncio
    async def test_marks_the_command_authenticated(self):
        session = await self._start_stub("stub-a")
        try:
            assert "stub-a" in process_runner.authenticated_commands
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_clears_a_previous_bridge_error(self):
        process_runner.bridge_errors["stub-b"] = "an older failure"
        session = await self._start_stub("stub-b")
        try:
            assert "stub-b" not in process_runner.bridge_errors
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_clears_a_pending_authorization(self):
        process_runner.pending_auth_urls["stub-c"] = "https://example.com/authorize"
        session = await self._start_stub("stub-c")
        try:
            assert "stub-c" not in process_runner.pending_auth_urls
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_releases_the_concurrency_guard_on_success(self):
        session = process_runner.ProcessSession("stub-d")
        session._parts = ["python3", "-c", _STUB_SERVER]
        with patch.object(process_runner, "ProcessSession", return_value=session):
            await process_runner.introspect("stub-d")
        assert "stub-d" not in process_runner._spawning


class TestSpawnGuardIsAlwaysReleased:
    """A leaked guard entry is permanent: every later introspect and re-authorize
    for that command reports a bridge that is "already starting" until restart."""

    def teardown_method(self):
        process_runner._spawning.clear()

    @pytest.mark.asyncio
    async def test_an_unparseable_command_does_not_strand_the_guard(self):
        # shlex.split raises inside ProcessSession.__init__, which used to run
        # outside the try that releases the guard.
        bad = "npx -y mcp-remote 'https://unclosed"
        with pytest.raises(RuntimeError):
            await process_runner.introspect(bad)
        assert bad not in process_runner._spawning

        # And the next attempt must reach the real error, not the guard.
        with pytest.raises(RuntimeError) as exc:
            await process_runner.introspect(bad)
        assert not isinstance(exc.value, process_runner.ConcurrentSpawnError)

    @pytest.mark.asyncio
    async def test_a_failure_in_close_does_not_strand_the_guard(self):
        cmd = "stub-close-fail"

        class Boom(process_runner.ProcessSession):
            async def _start(self):
                return None

            async def list_tools(self):
                return []

            async def close(self):
                raise RuntimeError("close blew up")

        with patch.object(process_runner, "ProcessSession", Boom):
            with pytest.raises(RuntimeError, match="close blew up"):
                await process_runner.introspect(cmd)
        assert cmd not in process_runner._spawning


class TestFailedStartLeavesNothingUsable:
    def teardown_method(self):
        process_runner.bridge_errors.clear()
        process_runner.authenticated_commands.clear()

    @pytest.mark.asyncio
    async def test_a_process_that_outlived_a_failed_handshake_is_killed(self):
        # _alive() is "returncode is None", so a surviving process would make the
        # next call_tool skip _start and write to a bridge that never completed
        # initialize — and read the stale initialize reply as its tool result.
        session = process_runner.ProcessSession("stub-hang")
        # A process that never answers: the handshake times out, it stays alive.
        session._parts = ["python3", "-c", "import time; time.sleep(30)"]
        session.init_timeout = 0.5
        with pytest.raises(Exception):
            await session._start()
        assert session._proc is not None
        await asyncio.wait_for(session._proc.wait(), timeout=5)
        assert session._alive() is False

    @pytest.mark.asyncio
    async def test_a_failed_start_marks_the_command_unauthenticated(self):
        cmd = "stub-fail-auth"
        process_runner.authenticated_commands.add(cmd)
        session = process_runner.ProcessSession(cmd)

        async def boom():
            raise EOFError("closed")

        session._start_inner = boom
        with pytest.raises(EOFError):
            await session._start()
        assert cmd not in process_runner.authenticated_commands


class TestRefreshDoesNotBlankHealthyStatus:
    """The hourly refresh spawns a throwaway session under the same command key
    as a live one. Pre-emptively clearing the status made the UI report
    "unknown" for the duration of every renewal."""

    def teardown_method(self):
        process_runner.authenticated_commands.clear()
        process_runner.bridge_errors.clear()

    @pytest.mark.asyncio
    async def test_a_successful_respawn_never_blanks_the_status(self):
        cmd = "stub-refresh"
        process_runner.authenticated_commands.add(cmd)
        session = process_runner.ProcessSession(cmd)
        session._parts = ["python3", "-c", _STUB_SERVER]
        await session._start()
        try:
            assert cmd in process_runner.authenticated_commands
        finally:
            await session.close()


class TestResponseCorrelation:
    """A line off stdout is not necessarily the reply to the request just sent.
    Reading positionally made the first line the "response", and every later
    call was then answered by the previous call's reply — permanently."""

    @staticmethod
    def _session(feed: bytes, eof: bool = False) -> process_runner.ProcessSession:
        reader = asyncio.StreamReader(limit=process_runner.STREAM_LIMIT)
        reader.feed_data(feed)
        if eof:
            reader.feed_eof()
        session = process_runner.ProcessSession("correlation-test")
        session._proc = SimpleNamespace(stdout=reader, stdin=_NullStdin())
        session._start_stdout_reader()
        return session

    @pytest.mark.asyncio
    async def test_a_notification_is_not_mistaken_for_the_reply(self):
        # Servers emit logging/progress notifications whenever they like.
        feed = (
            b'{"jsonrpc":"2.0","method":"notifications/message",'
            b'"params":{"level":"info","data":"working"}}\n'
            b'{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"real"}]}}\n'
        )
        session = self._session(feed)
        result = await session._request("tools/list", {}, timeout=5)
        assert result["result"]["tools"][0]["name"] == "real"

    @pytest.mark.asyncio
    async def test_non_json_output_does_not_break_the_call(self):
        # A wrapper script writing a banner to stdout used to raise
        # JSONDecodeError straight out of whichever call was in flight.
        feed = (
            b"npm notice New major version of npm available!\n"
            b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
        )
        session = self._session(feed)
        result = await session._request("tools/list", {}, timeout=5)
        assert result["result"]["ok"] is True
        assert session._stdout_noise  # kept for diagnostics, bounded

    @pytest.mark.asyncio
    async def test_replies_are_matched_by_id_not_by_arrival_order(self):
        # Out-of-order replies must each reach their own caller.
        reader = asyncio.StreamReader(limit=process_runner.STREAM_LIMIT)
        session = process_runner.ProcessSession("out-of-order")
        session._proc = SimpleNamespace(stdout=reader, stdin=_NullStdin())
        session._start_stdout_reader()

        first_call = asyncio.ensure_future(session._request("a", {}, timeout=5))
        second_call = asyncio.ensure_future(session._request("b", {}, timeout=5))
        # Feed only once both are actually waiting; a real server cannot reply
        # before the request is sent, so pre-feeding would test nothing.
        while len(session._pending) < 2:
            await asyncio.sleep(0)

        reader.feed_data(b'{"jsonrpc":"2.0","id":2,"result":{"which":"second"}}\n')
        reader.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{"which":"first"}}\n')
        first, second = await asyncio.gather(first_call, second_call)
        assert first["result"]["which"] == "first"
        assert second["result"]["which"] == "second"

    @pytest.mark.asyncio
    async def test_a_reply_that_arrives_after_a_timeout_is_discarded(self):
        # The off-by-one that never recovered: a late reply must not become the
        # next caller's answer.
        reader = asyncio.StreamReader(limit=process_runner.STREAM_LIMIT)
        session = process_runner.ProcessSession("late-reply")
        session._proc = SimpleNamespace(stdout=reader, stdin=_NullStdin())
        session._start_stdout_reader()

        with pytest.raises(asyncio.TimeoutError):
            await session._request("slow", {}, timeout=0.1)

        # id 1's reply turns up now, unwanted; id 2 is the live request.
        reader.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{"which":"stale"}}\n')
        await asyncio.sleep(0.05)
        reader.feed_data(b'{"jsonrpc":"2.0","id":2,"result":{"which":"fresh"}}\n')
        result = await session._request("next", {}, timeout=5)
        assert result["result"]["which"] == "fresh"

    @pytest.mark.asyncio
    async def test_stdout_closing_fails_everyone_still_waiting(self):
        session = self._session(b"", eof=True)
        with pytest.raises(EOFError):
            await session._request("tools/list", {}, timeout=5)

    @pytest.mark.asyncio
    async def test_closing_the_session_releases_a_waiting_caller(self):
        reader = asyncio.StreamReader(limit=process_runner.STREAM_LIMIT)
        session = process_runner.ProcessSession("closed-mid-call")
        session._proc = SimpleNamespace(stdout=reader, stdin=_NullStdin())
        session._start_stdout_reader()

        async def close_soon():
            await asyncio.sleep(0.05)
            session._fail_pending(EOFError("MCP session closed"))

        asyncio.ensure_future(close_soon())
        with pytest.raises(EOFError, match="closed"):
            await session._request("tools/list", {}, timeout=5)

    @pytest.mark.asyncio
    async def test_noise_buffer_is_bounded(self):
        session = process_runner.ProcessSession("noisy")
        for i in range(50):
            session._note_noise(f"line {i}")
        assert len(session._stdout_noise) == 10


GH_COMMAND = (
    "npx -y mcp-remote https://api.githubcopilot.com/mcp/ "
    "--header Authorization:${GITHUB_MCP_AUTH_HEADER} --header X-MCP-Toolsets:all"
)


class TestHeaderCredentialChecks:
    """mcp-remote warns only when a variable is unset. Set-but-empty and
    quote-wrapped values both reach the server as a broken header, and the 401
    comes back disguised as an unrelated OAuth failure."""

    def test_finds_the_variables_a_command_interpolates(self):
        assert process_runner.header_env_vars(GH_COMMAND) == [
            ("Authorization", "GITHUB_MCP_AUTH_HEADER")
        ]

    def test_a_command_with_no_interpolated_headers(self):
        assert process_runner.header_env_vars("npx -y mcp-remote https://x/mcp") == []

    def test_unset_is_reported(self):
        msg = process_runner.check_header_credentials(GH_COMMAND, {})
        assert "GITHUB_MCP_AUTH_HEADER is not set" in msg
        assert "package.env_keys" in msg

    @pytest.mark.parametrize("value", ["", "   "])
    def test_set_but_empty_is_reported(self, value):
        msg = process_runner.check_header_credentials(
            GH_COMMAND, {"GITHUB_MCP_AUTH_HEADER": value}
        )
        assert "set but empty" in msg

    @pytest.mark.parametrize("value", ['"Bearer ghp_x"', "'Bearer ghp_x'"])
    def test_doubly_quoted_values_are_reported(self, value):
        # Quotes in the *file* are correct and often required — a value with a
        # space must be quoted or the shell that sources .env truncates it.
        # Quotes surviving into the resolved value mean it was quoted twice.
        msg = process_runner.check_header_credentials(
            GH_COMMAND, {"GITHUB_MCP_AUTH_HEADER": value}
        )
        assert "quoted twice" in msg

    def test_a_correctly_quoted_file_value_is_accepted(self, tmp_path, monkeypatch):
        # End to end: the file holds "Bearer ghp_x" with quotes, and what the
        # bridge receives has none.
        from config import env_quote

        env_file = tmp_path / ".env"
        env_file.write_text(f"GITHUB_MCP_AUTH_HEADER={env_quote('Bearer ghp_x')}\n")
        monkeypatch.setenv("MCP_ENV_FILE", str(env_file))
        session = process_runner.ProcessSession(
            GH_COMMAND, env_keys=["GITHUB_MCP_AUTH_HEADER"]
        )
        env = session._build_env()
        assert env["GITHUB_MCP_AUTH_HEADER"] == "Bearer ghp_x"
        assert process_runner.check_header_credentials(GH_COMMAND, env) is None

    def test_a_usable_value_passes(self):
        assert process_runner.check_header_credentials(
            GH_COMMAND, {"GITHUB_MCP_AUTH_HEADER": "Bearer ghp_realtoken"}
        ) is None

    @pytest.mark.asyncio
    async def test_a_broken_credential_fails_before_spawning(self, monkeypatch):
        spawned = []

        async def fake_exec(*args, **kwargs):
            spawned.append(args)
            raise AssertionError("should not have spawned")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.delenv("GITHUB_MCP_AUTH_HEADER", raising=False)
        session = process_runner.ProcessSession(GH_COMMAND)
        try:
            with pytest.raises(RuntimeError, match="is not set"):
                await session._start()
            assert spawned == []
            assert "GITHUB_MCP_AUTH_HEADER" in process_runner.bridge_errors[GH_COMMAND]
        finally:
            process_runner.bridge_errors.pop(GH_COMMAND, None)


class TestRejectedCredentialClassification:
    """A bridge that already sends a credential and then falls back to OAuth was
    told to register an OAuth client — which is the wrong direction entirely."""

    DCR = (
        "Fatal error: Error: Incompatible auth server: does not support "
        "dynamic client registration"
    )

    def test_names_the_credential_when_the_bridge_sends_one(self):
        msg = process_runner._classify_failure([self.DCR], GH_COMMAND)
        assert "rejected the credential" in msg
        assert "GITHUB_MCP_AUTH_HEADER" in msg
        assert "static-oauth-client-info" not in msg

    def test_still_suggests_a_static_client_when_there_is_no_credential(self):
        msg = process_runner._classify_failure(
            [self.DCR], "npx -y mcp-remote https://mcp.example.com/mcp"
        )
        assert "--static-oauth-client-info" in msg
