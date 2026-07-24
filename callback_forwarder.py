"""Forward published Docker callback ports to loopback-only OAuth listeners.

``mcp-remote`` deliberately binds its temporary OAuth callback server to
127.0.0.1.  That is safe on a workstation, but Docker's published-port traffic
arrives on the container's non-loopback interface and therefore cannot reach
it.  This module opens the same port only on each non-loopback container IPv4
address and relays bytes to 127.0.0.1 on that port.

The host-side Docker mapping should remain bound to 127.0.0.1 so authorization
codes are never exposed on the LAN.
"""

from __future__ import annotations

import os
import select
import socket
import socketserver
import threading
from collections.abc import Iterable


DEFAULT_IDLE_TIMEOUT_SECONDS = 30.0


def parse_forward_ports(value: str | None) -> list[int]:
    """Parse a comma/whitespace separated callback-port list."""
    if not value or not value.strip():
        return []
    ports: list[int] = []
    for item in value.replace(",", " ").split():
        try:
            port = int(item)
        except ValueError as exc:
            raise ValueError(f"Invalid callback forward port: {item!r}") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"Callback forward port out of range: {port}")
        if port not in ports:
            ports.append(port)
    return ports


def non_loopback_ipv4_addresses() -> list[str]:
    """Return IPv4 addresses assigned to this host, excluding loopback."""
    addresses: list[str] = []

    def add(value: str) -> None:
        if value and not value.startswith("127.") and value not in addresses:
            addresses.append(value)

    try:
        for entry in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ):
            add(entry[4][0])
    except OSError:
        pass

    # Some minimal container DNS configurations resolve the hostname only to
    # loopback.  A UDP connect performs route selection without sending data.
    if not addresses:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            add(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()

    if not addresses:
        raise RuntimeError("No non-loopback IPv4 address available for callback forwarding")
    return addresses


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class CallbackForwarder:
    """Relay one non-loopback ``host:port`` to ``127.0.0.1:port``."""

    def __init__(
        self,
        bind_host: str,
        port: int,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self.bind_host = bind_host
        self.port = port
        self.idle_timeout = idle_timeout
        target_port = port
        timeout = idle_timeout

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    upstream = socket.create_connection(
                        ("127.0.0.1", target_port), timeout=timeout
                    )
                except OSError:
                    return
                with upstream:
                    self.request.setblocking(False)
                    upstream.setblocking(False)
                    sockets = [self.request, upstream]
                    while True:
                        readable, _, exceptional = select.select(
                            sockets, [], sockets, timeout
                        )
                        if exceptional or not readable:
                            return
                        for source in readable:
                            try:
                                data = source.recv(65536)
                            except (BlockingIOError, ConnectionResetError, OSError):
                                return
                            if not data:
                                return
                            destination = upstream if source is self.request else self.request
                            try:
                                destination.sendall(data)
                            except OSError:
                                return

        self._server = _ThreadingTCPServer((bind_host, port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"oauth-callback-forward-{bind_host}-{port}",
        )

    def start(self) -> "CallbackForwarder":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)


def start_callback_forwarders(ports: Iterable[int]) -> list[CallbackForwarder]:
    """Start forwarders for every non-loopback address/port combination."""
    started: list[CallbackForwarder] = []
    try:
        for host in non_loopback_ipv4_addresses():
            for port in ports:
                started.append(CallbackForwarder(host, port).start())
    except Exception:
        for forwarder in started:
            forwarder.stop()
        raise
    return started


def start_callback_forwarders_from_env() -> list[CallbackForwarder]:
    """Start ports named by ``MCPPROXY_CALLBACK_FORWARD_PORTS``."""
    ports = parse_forward_ports(os.environ.get("MCPPROXY_CALLBACK_FORWARD_PORTS", ""))
    if not ports:
        return []
    forwarders = start_callback_forwarders(ports)
    for forwarder in forwarders:
        print(
            "[mcpproxy] OAuth callback forwarding "
            f"{forwarder.bind_host}:{forwarder.port} -> 127.0.0.1:{forwarder.port}"
        )
    return forwarders
