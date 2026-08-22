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
import time
from collections.abc import Iterable


DEFAULT_IDLE_TIMEOUT_SECONDS = 30.0

# How long to keep an arriving callback waiting for the loopback listener to
# appear.  The forwarder binds at startup, but mcp-remote binds its callback
# port only once its bridge reaches the OAuth step — so a callback that lands
# while dependencies are still installing would otherwise hit an accepted
# socket with nothing behind it and be dropped without trace.
DEFAULT_UPSTREAM_WAIT_SECONDS = float(
    os.environ.get("MCPPROXY_CALLBACK_WAIT_SECONDS", "60")
)

# Served when the wait elapses, so the browser shows a reason instead of an
# empty reply.  A bare TCP close looks like a network fault to the user.
_UNAVAILABLE_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"<h3>No authorization is in progress</h3>"
    b"<p>mcpproxy is forwarding this port, but no bridge is currently waiting "
    b"for a callback. Start the authorization again from the mcpproxy UI, then "
    b"retry - or copy this page's URL and use "
    b"<b>Paste callback URL</b> there.</p>"
)

# Forwarders currently relaying, for diagnostics only.  ``start_callback_forwarders``
# is the sole registrar: a ``CallbackForwarder`` constructed directly (as the unit
# tests do) stays out of the registry, so what the UI reports is exactly what the
# server started.
_active_forwarders: list["CallbackForwarder"] = []
_registry_lock = threading.Lock()


def active_forwarders() -> list["CallbackForwarder"]:
    """Return a snapshot of the forwarders started by this process."""
    with _registry_lock:
        return list(_active_forwarders)


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
        upstream_wait: float = DEFAULT_UPSTREAM_WAIT_SECONDS,
    ) -> None:
        self.bind_host = bind_host
        self.port = port
        self.idle_timeout = idle_timeout
        self.upstream_wait = upstream_wait
        target_port = port
        timeout = idle_timeout
        wait = upstream_wait

        class Handler(socketserver.BaseRequestHandler):
            def _connect_upstream(self) -> socket.socket | None:
                """Connect to the loopback listener, waiting for it to appear.

                An authorization code is single-use and short-lived: failing
                fast here loses it outright, whereas the browser will happily
                hold the connection for a few seconds while the bridge starts.
                """
                deadline = time.monotonic() + wait
                delay = 0.05
                while True:
                    try:
                        return socket.create_connection(
                            ("127.0.0.1", target_port), timeout=timeout
                        )
                    except OSError:
                        if time.monotonic() >= deadline:
                            return None
                        time.sleep(delay)
                        delay = min(delay * 2, 1.0)

            def handle(self) -> None:
                upstream = self._connect_upstream()
                if upstream is None:
                    try:
                        self.request.sendall(_UNAVAILABLE_RESPONSE)
                    except OSError:
                        pass
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
                            except BlockingIOError:
                                # select() readability is advisory; a spurious
                                # wake must not tear down a live callback.
                                continue
                            except (ConnectionResetError, OSError):
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
        with _registry_lock:
            if self in _active_forwarders:
                _active_forwarders.remove(self)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)


def start_callback_forwarders(ports: Iterable[int]) -> list[CallbackForwarder]:
    """Start forwarders for every non-loopback address/port combination."""
    started: list[CallbackForwarder] = []
    try:
        for host in non_loopback_ipv4_addresses():
            for port in ports:
                forwarder = CallbackForwarder(host, port).start()
                started.append(forwarder)
                with _registry_lock:
                    _active_forwarders.append(forwarder)
    except Exception:
        for forwarder in started:
            forwarder.stop()  # also de-registers
        raise
    return started


def start_callback_forwarders_from_env(
    extra_ports: Iterable[int] = (),
) -> list[CallbackForwarder]:
    """Start forwarders for ``MCPPROXY_CALLBACK_FORWARD_PORTS`` plus *extra_ports*.

    *extra_ports* are the callback ports the configured providers actually
    declare.  Forwarding those by default removes a standing trap: the port in a
    provider's YAML, the published Docker port and this environment variable are
    three independent settings, and nothing used to notice when they disagreed —
    the callback simply never arrived.
    """
    ports = parse_forward_ports(os.environ.get("MCPPROXY_CALLBACK_FORWARD_PORTS", ""))
    for port in extra_ports:
        if port not in ports:
            ports.append(port)
    if not ports:
        return []
    forwarders = start_callback_forwarders(ports)
    for forwarder in forwarders:
        print(
            "[mcpproxy] OAuth callback forwarding "
            f"{forwarder.bind_host}:{forwarder.port} -> 127.0.0.1:{forwarder.port}"
        )
    return forwarders
