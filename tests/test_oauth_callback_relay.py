"""Tests for the manual OAuth callback relay.

Covers the three things that have to be right for a pasted callback URL to work
for *any* provider: finding the loopback port, parsing what the user pasted, and
delivering it without leaking the authorization code.
"""

import http.server
import socket
import threading

import pytest

import oauth_callback_relay as relay


ASANA_COMMAND = (
    "npx -y mcp-remote@0.1.38 https://mcp.asana.com/v2/mcp 8887 "
    "--static-oauth-client-info @/app/tools/secrets/asana/client_info.json "
    "--resource https://mcp.asana.com/v2 --auth-timeout 600"
)


class TestCallbackPortFromCommand:
    @pytest.mark.parametrize(
        "command,expected",
        [
            (ASANA_COMMAND, 8887),
            ("npx -y mcp-remote https://mcp.linear.app/sse 9100", 9100),
            ("mcp-remote https://example.com/mcp 8888", 8888),
            ("/usr/local/bin/mcp-remote https://example.com/mcp 8080", 8080),
            ("npx -y mcp-remote@0.1.38 https://example.com/mcp 3000 --allow-http", 3000),
        ],
    )
    def test_reads_the_second_positional(self, command, expected):
        assert relay.callback_port_from_command(command) == expected

    def test_flag_value_is_never_mistaken_for_a_port(self):
        # The port is optional; --auth-timeout's 600 must not be picked up.
        assert relay.callback_port_from_command(
            "npx -y mcp-remote https://example.com/mcp --auth-timeout 600"
        ) is None

    @pytest.mark.parametrize(
        "command",
        [
            "npx -y mcp-remote https://example.com/mcp",
            "npx @playwright/mcp@latest --isolated",
            "npx -y mcp-remote https://example.com/mcp notaport",
            "npx -y mcp-remote https://example.com/mcp 0",
            "npx -y mcp-remote https://example.com/mcp 70000",
            'npx -y mcp-remote "https://example.com/mcp 8887',
        ],
    )
    def test_returns_none_when_there_is_no_usable_port(self, command):
        assert relay.callback_port_from_command(command) is None


class TestResolveCallbackPort:
    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        import process_runner

        process_runner.callback_listener_ports.clear()
        yield
        process_runner.callback_listener_ports.clear()

    def test_scraped_port_wins_over_the_command(self):
        import process_runner

        # mcp-remote picks its own port when the YAML omits one, so what it
        # announced always beats what the command asked for.
        process_runner.callback_listener_ports[ASANA_COMMAND] = 3334
        assert relay.resolve_callback_port(ASANA_COMMAND) == (3334, "stderr")

    def test_falls_back_to_the_command(self):
        assert relay.resolve_callback_port(ASANA_COMMAND) == (8887, "command")

    def test_reports_nothing_when_neither_knows(self):
        assert relay.resolve_callback_port("npx -y mcp-remote https://x/mcp") == (None, None)


class TestParseCallbackInput:
    def test_full_url(self):
        path, params = relay.parse_callback_input(
            "http://localhost:8887/oauth/callback?code=abc&state=xyz"
        )
        assert path == "/oauth/callback"
        assert params == {"code": "abc", "state": "xyz"}

    def test_host_scheme_and_port_are_ignored(self):
        # The replay target is decided server-side; only the path/query matter.
        _, params = relay.parse_callback_input(
            "https://evil.example:1234/oauth/callback?code=abc&state=xyz"
        )
        assert params == {"code": "abc", "state": "xyz"}

    @pytest.mark.parametrize(
        "raw", ["code=abc&state=xyz", "?code=abc&state=xyz", "  code=abc&state=xyz  "]
    )
    def test_bare_query_strings(self, raw):
        path, params = relay.parse_callback_input(raw)
        assert path == relay.DEFAULT_CALLBACK_PATH
        assert params == {"code": "abc", "state": "xyz"}

    def test_wrapped_paste_is_reassembled(self):
        _, params = relay.parse_callback_input(
            "http://localhost:8887/oauth/callback?code=ab\n  c&state=xyz"
        )
        assert params["code"] == "abc"

    def test_fragment_is_dropped(self):
        _, params = relay.parse_callback_input("code=abc&state=xyz#anything")
        assert params == {"code": "abc", "state": "xyz"}

    def test_unknown_parameters_are_not_forwarded(self):
        _, params = relay.parse_callback_input("code=abc&state=xyz&redirect=http://evil")
        assert params == {"code": "abc", "state": "xyz"}

    def test_oidc_and_broker_parameters_survive(self):
        # Non-Asana providers add these; they must reach the listener.
        _, params = relay.parse_callback_input(
            "code=abc&state=xyz&iss=https%3A%2F%2Fissuer&session_state=s1&scope=a+b"
        )
        assert params["iss"] == "https://issuer"
        assert params["session_state"] == "s1"
        assert params["scope"] == "a b"

    def test_percent_encoded_code_is_decoded(self):
        _, params = relay.parse_callback_input("code=a%2Bb%2Fc&state=s")
        assert params["code"] == "a+b/c"

    def test_missing_code_is_rejected(self):
        with pytest.raises(relay.CallbackInputError, match="No code="):
            relay.parse_callback_input("state=xyz")

    def test_provider_error_is_surfaced(self):
        with pytest.raises(relay.CallbackInputError, match="access_denied"):
            relay.parse_callback_input("error=access_denied&error_description=nope")

    def test_error_description_is_not_echoed(self):
        with pytest.raises(relay.CallbackInputError) as exc:
            relay.parse_callback_input("error=access_denied&error_description=SECRETTEXT")
        assert "SECRETTEXT" not in str(exc.value)

    def test_empty_input_is_rejected(self):
        with pytest.raises(relay.CallbackInputError):
            relay.parse_callback_input("   ")

    def test_oversized_input_is_rejected(self):
        with pytest.raises(relay.CallbackInputError, match="too long"):
            relay.parse_callback_input("code=" + "a" * relay.MAX_INPUT_CHARS)

    def test_traversal_path_is_rejected(self):
        with pytest.raises(relay.CallbackInputError, match="Unexpected callback path"):
            relay.parse_callback_input("http://localhost:8887/../admin?code=abc")

    def test_control_characters_in_code_are_rejected(self):
        with pytest.raises(relay.CallbackInputError, match="code value"):
            relay.parse_callback_input("code=a%00b&state=s")


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Stands in for mcp-remote's loopback callback listener."""

    status = 200
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's naming
        type(self).seen.append(self.path)
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence the default stderr logging
        pass


@pytest.fixture()
def listener():
    """A throwaway loopback listener on an ephemeral port."""
    _Recorder.seen = []
    _Recorder.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDeliverToBridge:
    @pytest.mark.asyncio
    async def test_replays_path_and_whitelisted_query(self, listener):
        port = listener.server_address[1]
        status = await relay.deliver_to_bridge(
            port, "/oauth/callback", {"code": "abc", "state": "xyz"}
        )
        assert status == 200
        assert _Recorder.seen == ["/oauth/callback?code=abc&state=xyz"]

    @pytest.mark.asyncio
    async def test_listener_rejection_is_reported(self, listener):
        _Recorder.status = 400
        with pytest.raises(relay.CallbackDeliveryError, match="HTTP 400"):
            await relay.deliver_to_bridge(
                listener.server_address[1], "/oauth/callback", {"code": "abc"}
            )

    @pytest.mark.asyncio
    async def test_closed_port_gives_an_actionable_error(self):
        port = _free_port()
        with pytest.raises(relay.CallbackDeliveryError, match="Nothing is listening"):
            await relay.deliver_to_bridge(port, "/oauth/callback", {"code": "abc"})

    @pytest.mark.asyncio
    async def test_connection_failure_never_leaks_the_code(self):
        # httpx puts the request URL — and with it the code — in its own message
        # and in the chained context.  `raise ... from None` must keep both out
        # of anything that gets printed or returned.
        import traceback

        port = _free_port()
        # Held in a variable, not a literal: pytest renders the calling source
        # line into the traceback, which would otherwise fail this on its own.
        params = {"code": "SECRETC" + "ODE"}
        with pytest.raises(relay.CallbackDeliveryError) as exc:
            await relay.deliver_to_bridge(port, "/oauth/callback", params)
        assert "SECRETCODE" not in str(exc.value)
        assert exc.value.__cause__ is None
        assert exc.value.__suppress_context__ is True
        rendered = "".join(
            traceback.format_exception(
                type(exc.value), exc.value, exc.value.__traceback__
            )
        )
        assert "SECRETCODE" not in rendered


class TestProbeLoopbackPort:
    def test_true_for_a_bound_port(self, listener):
        assert relay.probe_loopback_port(listener.server_address[1]) is True

    def test_false_for_a_closed_port(self):
        assert relay.probe_loopback_port(_free_port()) is False


class TestAuthorizeUrlState:
    """A callback from an earlier attempt still parses, but the bridge has since
    generated a new PKCE verifier. Comparing state is what stops the single-use
    code being burned on an opaque code_verifier mismatch."""

    def test_reads_the_state_parameter(self):
        assert relay.authorize_url_state(
            "https://app.asana.com/-/oauth_authorize?client_id=1&state=ABC123"
        ) == "ABC123"

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/login/oauth",          # issuer base, no query
            "https://x/authorize?client_id=1",         # no state
            "https://x/authorize?state=",              # blank state
            "",
            "not a url at all",
        ],
    )
    def test_returns_none_when_there_is_no_state(self, url):
        assert relay.authorize_url_state(url) is None

    def test_percent_encoded_state_is_decoded(self):
        assert relay.authorize_url_state("https://x/a?state=a%2Bb") == "a+b"


class TestReplayIsAlwaysLoopback:
    """The replay target must never come from the pasted value."""

    def test_a_pasted_host_cannot_redirect_the_replay(self, listener):
        # parse_callback_input discards scheme/host/port by design; deliver_to_bridge
        # takes only a port. There is no code path that accepts a caller host.
        import inspect

        source = inspect.getsource(relay.deliver_to_bridge)
        assert '"http://127.0.0.1:{port}{path}"' in source or "127.0.0.1" in source
        _, params = relay.parse_callback_input(
            "https://evil.example:9999/oauth/callback?code=abc&state=s"
        )
        assert params == {"code": "abc", "state": "s"}

    def test_proxy_environment_is_ignored(self):
        # An HTTPS_PROXY in the container must never see a URL carrying a code.
        import inspect

        assert "trust_env=False" in inspect.getsource(relay.deliver_to_bridge)
