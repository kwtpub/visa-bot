"""Local proxy auth bridge for Chromium.

Chrome extension based proxy authentication is brittle across Chrome versions.
This bridge exposes a local unauthenticated HTTP proxy and forwards each request
to the real upstream proxy with a Proxy-Authorization header.
"""
from __future__ import annotations

import base64
import select
import socket
import threading
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .util import log


@dataclass(frozen=True)
class UpstreamProxy:
    host: str
    port: int
    username: str
    password: str


def parse_auth_proxy(proxy: str) -> UpstreamProxy | None:
    proxy = (proxy or "").strip()
    if not proxy or "@" not in proxy:
        return None
    if proxy.lower().startswith(("socks4://", "socks5://", "socks5h://", "https://")):
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    parsed = urlsplit(proxy)
    if parsed.scheme and parsed.scheme != "http":
        return None
    if not parsed.hostname or not parsed.port or parsed.username is None:
        return None
    return UpstreamProxy(
        host=parsed.hostname,
        port=int(parsed.port),
        username=unquote(parsed.username),
        password=unquote(parsed.password or ""),
    )


def auth_bridge_supported(proxy: str) -> bool:
    return parse_auth_proxy(proxy) is not None


def _auth_header(username: str, password: str) -> bytes:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}".encode("ascii")


def _inject_proxy_auth(request_head: bytes, auth_header: bytes) -> bytes:
    lines = request_head.split(b"\r\n")
    if not lines:
        return request_head
    is_connect = lines[0].upper().startswith(b"CONNECT ")
    filtered = [
        line
        for line in lines[1:]
        if not line.lower().startswith(b"proxy-authorization:")
        and not (is_connect and line.lower().startswith(b"host:"))
    ]
    return b"\r\n".join([lines[0], auth_header, *filtered])


class ProxyAuthBridge:
    def __init__(self, upstream: UpstreamProxy):
        self.upstream = upstream
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.host = "127.0.0.1"
        self.port = 0

    @property
    def proxy(self) -> str:
        return f"{self.host}:{self.port}"

    def __enter__(self) -> "ProxyAuthBridge":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, 0))
        server.listen(64)
        self._server = server
        self.port = int(server.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, name="proxy-auth-bridge", daemon=True)
        self._thread.start()
        log.info("Local proxy auth bridge listening on %s", self.proxy)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        try:
            with socket.create_connection((self.host, self.port), timeout=0.2):
                pass
        except OSError:
            pass

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        auth = _auth_header(self.upstream.username, self.upstream.password)
        try:
            client.settimeout(30)
            initial = self._read_headers(client)
            if not initial:
                return
            head, sep, body = initial.partition(b"\r\n\r\n")
            if not sep:
                return
            request = _inject_proxy_auth(head, auth) + b"\r\n\r\n" + body
            with socket.create_connection((self.upstream.host, self.upstream.port), timeout=30) as upstream:
                upstream.settimeout(30)
                upstream.sendall(request)
                self._relay(client, upstream)
        except OSError as e:
            log.debug("Proxy auth bridge connection failed: %s", e)
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        while True:
            readable, _, _ = select.select(sockets, [], [], 60)
            if not readable:
                return
            for sock in readable:
                other = right if sock is left else left
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    other.sendall(chunk)
                except OSError:
                    return


def start_proxy_auth_bridge(proxy: str) -> ProxyAuthBridge | None:
    upstream = parse_auth_proxy(proxy)
    if not upstream:
        return None
    return ProxyAuthBridge(upstream)
