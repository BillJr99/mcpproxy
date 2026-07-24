"""Tests for the Docker-to-loopback OAuth callback forwarder."""

import socket
import threading

import pytest

from callback_forwarder import (
    CallbackForwarder,
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
        assert len(started) == 4
