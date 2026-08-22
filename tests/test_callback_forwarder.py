"""Tests for the Docker-to-loopback OAuth callback forwarder."""

import socket
import threading
import time

import pytest

import callback_forwarder
from callback_forwarder import (
    CallbackForwarder,
    active_forwarders,
    non_loopback_ipv4_addresses,
    parse_forward_ports,
    start_callback_forwarders,
)


class TestParseForwardPorts:
    def test_empty_configuration_disables_forwarding(self):
        assert parse_forward_ports("") == []
        assert parse_forward_ports("   ") == []

    def test_parses_deduplicates_and_preserves_order(self):
        assert parse_forward_ports("8887, 9000 8887") == [8887, 9000]

    @pytest.mark.parametrize("value", ["0", "65536", "not-a-port", "8887,-1"])
    def test_rejects_invalid_ports(self, value):
        with pytest.raises(ValueError):
            parse_forward_ports(value)


class TestAddressDiscovery:
    def test_returns_only_non_loopback_ipv4_addresses(self):
        addresses = non_loopback_ipv4_addresses()
        assert addresses
        assert all(not value.startswith("127.") for value in addresses)
        assert len(addresses) == len(set(addresses))


class TestCallbackForwarder:
    def test_forwards_container_interface_to_same_port_on_loopback(self):
        bind_host = non_loopback_ipv4_addresses()[0]

        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        target.bind(("127.0.0.1", 0))
        port = target.getsockname()[1]
        target.listen(1)

        received = []

        def serve_once():
            conn, _ = target.accept()
            with conn:
                data = conn.recv(1024)
                received.append(data)
                conn.sendall(b"callback-ok")

        target_thread = threading.Thread(target=serve_once, daemon=True)
        target_thread.start()

        forwarder = CallbackForwarder(bind_host, port)
        forwarder.start()
        try:
            with socket.create_connection((bind_host, port), timeout=3) as client:
                client.sendall(b"authorization-code")
                assert client.recv(1024) == b"callback-ok"
            target_thread.join(timeout=3)
            assert received == [b"authorization-code"]
        finally:
            forwarder.stop()
            target.close()

    def test_starts_one_forwarder_per_address_and_port(self, monkeypatch):
        created = []

        class FakeForwarder:
            def __init__(self, host, port):
                created.append((host, port))

            def start(self):
                return self

            def stop(self):
                callback_forwarder._active_forwarders.remove(self)

        monkeypatch.setattr("callback_forwarder.CallbackForwarder", FakeForwarder)
        monkeypatch.setattr(
            "callback_forwarder.non_loopback_ipv4_addresses",
            lambda: ["172.18.0.2", "10.0.0.2"],
        )

        started = start_callback_forwarders([8887, 9000])
        assert created == [
            ("172.18.0.2", 8887),
            ("172.18.0.2", 9000),
            ("10.0.0.2", 8887),
            ("10.0.0.2", 9000),
        ]
        for forwarder in started:
            forwarder.stop()
        assert len(started) == 4


class TestActiveForwarderRegistry:
    """The UI reports which relays actually bound, so the registry must track
    exactly what start_callback_forwarders started — no more, no less."""

    @pytest.fixture(autouse=True)
    def _isolate_registry(self):
        callback_forwarder._active_forwarders.clear()
        yield
        callback_forwarder._active_forwarders.clear()

    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("", 0))
            return probe.getsockname()[1]

    def test_registers_on_start_and_deregisters_on_stop(self):
        assert active_forwarders() == []
        forwarders = start_callback_forwarders([self._free_port()])
        try:
            assert len(active_forwarders()) == len(forwarders)
            assert all(f in active_forwarders() for f in forwarders)
        finally:
            for forwarder in forwarders:
                forwarder.stop()
        assert active_forwarders() == []

    def test_directly_constructed_forwarders_stay_out_of_the_registry(self):
        forwarder = CallbackForwarder(
            non_loopback_ipv4_addresses()[0], self._free_port()
        ).start()
        try:
            assert active_forwarders() == []
        finally:
            forwarder.stop()

    def test_a_failed_start_rolls_the_registry_back(self):
        blocked = self._free_port()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind((non_loopback_ipv4_addresses()[0], blocked))
        blocker.listen(1)
        try:
            # The first port binds and registers; the second collides, so the
            # rollback has to un-register the one that succeeded.
            with pytest.raises(OSError):
                start_callback_forwarders([self._free_port(), blocked])
        finally:
            blocker.close()
        assert active_forwarders() == []


class TestUpstreamWait:
    """An authorization code is single-use and short-lived. The forwarder binds
    at startup but mcp-remote binds its callback port only once its bridge
    reaches the OAuth step, so failing fast loses the code outright."""

    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("", 0))
            return probe.getsockname()[1]

    def test_waits_for_a_listener_that_appears_late(self):
        bind_host = non_loopback_ipv4_addresses()[0]
        port = self._free_port()
        forwarder = CallbackForwarder(bind_host, port, upstream_wait=5).start()
        received: list[bytes] = []

        def serve_late():
            time.sleep(0.75)          # bridge is still starting
            target = socket.socket()
            target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            target.bind(("127.0.0.1", port))
            target.listen(1)
            conn, _ = target.accept()
            with conn:
                received.append(conn.recv(1024))
                conn.sendall(b"callback-ok")
            target.close()

        thread = threading.Thread(target=serve_late, daemon=True)
        thread.start()
        try:
            with socket.create_connection((bind_host, port), timeout=10) as client:
                client.sendall(b"GET /oauth/callback?code=abc HTTP/1.1\r\n\r\n")
                assert client.recv(1024) == b"callback-ok"
            thread.join(timeout=5)
            assert b"code=abc" in received[0]
        finally:
            forwarder.stop()

    def test_explains_itself_when_nothing_ever_listens(self):
        bind_host = non_loopback_ipv4_addresses()[0]
        port = self._free_port()
        forwarder = CallbackForwarder(bind_host, port, upstream_wait=0.2).start()
        try:
            with socket.create_connection((bind_host, port), timeout=5) as client:
                client.sendall(b"GET /oauth/callback?code=abc HTTP/1.1\r\n\r\n")
                body = b""
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    body += chunk
            # A bare TCP close reads as a network fault; say what happened.
            assert b"503" in body
            assert b"Paste callback URL" in body
        finally:
            forwarder.stop()


class TestDeclaredPortForwarding:
    def test_env_and_declared_ports_are_unioned(self, monkeypatch):
        created = []

        class FakeForwarder:
            def __init__(self, host, port):
                self.bind_host = host
                self.port = port
                created.append(port)

            def start(self):
                return self

            def stop(self):
                callback_forwarder._active_forwarders.remove(self)

        monkeypatch.setattr("callback_forwarder.CallbackForwarder", FakeForwarder)
        monkeypatch.setattr(
            "callback_forwarder.non_loopback_ipv4_addresses", lambda: ["172.18.0.2"]
        )
        monkeypatch.setenv("MCPPROXY_CALLBACK_FORWARD_PORTS", "8887")
        started = callback_forwarder.start_callback_forwarders_from_env([8887, 9100])
        try:
            # 8887 is declared in both places and must not be started twice.
            assert created == [8887, 9100]
        finally:
            for f in started:
                f.stop()

    def test_declared_ports_alone_are_enough(self, monkeypatch):
        created = []

        class FakeForwarder:
            def __init__(self, host, port):
                self.bind_host = host
                self.port = port
                created.append(port)

            def start(self):
                return self

            def stop(self):
                callback_forwarder._active_forwarders.remove(self)

        monkeypatch.setattr("callback_forwarder.CallbackForwarder", FakeForwarder)
        monkeypatch.setattr(
            "callback_forwarder.non_loopback_ipv4_addresses", lambda: ["172.18.0.2"]
        )
        monkeypatch.delenv("MCPPROXY_CALLBACK_FORWARD_PORTS", raising=False)
        started = callback_forwarder.start_callback_forwarders_from_env([8887])
        try:
            assert created == [8887]
        finally:
            for f in started:
                f.stop()


class TestRelayResilience:
    def test_a_spurious_readability_wake_does_not_drop_the_callback(self):
        """Both sockets are non-blocking, so select() readability is advisory:
        recv can legitimately raise BlockingIOError. Tearing down there would
        lose a single-use authorization code mid-flight."""
        import inspect

        import callback_forwarder as cf

        source = inspect.getsource(cf.CallbackForwarder.__init__)
        # The bare `except (BlockingIOError, ...): return` used to abort the relay.
        assert "except BlockingIOError:" in source
        assert "continue" in source.split("except BlockingIOError:")[1][:200]
