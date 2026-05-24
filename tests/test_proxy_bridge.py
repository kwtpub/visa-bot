from __future__ import annotations

import base64
import socket
import threading

from bot.proxy_bridge import (
    _auth_header,
    _inject_proxy_auth,
    parse_auth_proxy,
    start_proxy_auth_bridge,
)


def test_parse_auth_proxy_plain_http():
    parsed = parse_auth_proxy("user:p%40ss@proxy.example:1000")

    assert parsed is not None
    assert parsed.host == "proxy.example"
    assert parsed.port == 1000
    assert parsed.username == "user"
    assert parsed.password == "p@ss"


def test_parse_auth_proxy_ignores_socks():
    assert parse_auth_proxy("socks5://user:pass@proxy.example:1080") is None


def test_inject_proxy_auth_replaces_existing_header():
    header = _auth_header("user", "pass")
    request = (
        b"CONNECT example.com:443 HTTP/1.1\r\n"
        b"Host: example.com:443\r\n"
        b"Proxy-Authorization: Basic old\r\n"
    )

    result = _inject_proxy_auth(request, header)

    assert result.count(b"Proxy-Authorization:") == 1
    assert b"Basic old" not in result
    assert base64.b64encode(b"user:pass") in result


def test_inject_proxy_auth_removes_host_from_connect():
    request = (
        b"CONNECT example.com:443 HTTP/1.1\r\n"
        b"Host: example.com:443\r\n"
        b"Proxy-Connection: keep-alive\r\n"
    )

    result = _inject_proxy_auth(request, _auth_header("user", "pass"))

    assert b"\r\nHost:" not in result
    assert b"Proxy-Connection: keep-alive" in result


def test_proxy_auth_bridge_forwards_with_auth_header():
    seen = {}
    ready = threading.Event()

    def upstream_server(server: socket.socket):
        ready.set()
        conn, _ = server.accept()
        with conn:
            data = conn.recv(4096)
            seen["request"] = data
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        thread = threading.Thread(target=upstream_server, args=(server,), daemon=True)
        thread.start()
        ready.wait(1)

        bridge = start_proxy_auth_bridge(f"user:pass@127.0.0.1:{port}")
        assert bridge is not None
        with bridge:
            with socket.create_connection(("127.0.0.1", bridge.port), timeout=3) as client:
                client.sendall(b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n")
                assert b"OK" in client.recv(4096)

    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"user:pass") in seen["request"]
