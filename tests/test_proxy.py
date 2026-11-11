import http.client
import socket
import ssl
import struct
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lesstests.proxy import Address, InterceptingProxy, RecordingProxy, _TunnelHandler
from lesstests.tls import CertificateAuthority


class EchoHandler(BaseHTTPRequestHandler):
    """Answers every request with its method, path and body, the way a tiny service would."""

    protocol_version = "HTTP/1.1"

    def _echo(self) -> None:
        if self.path == "/cut":
            self._cut_off()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        answer = f"{self.command} {self.path} {body.decode()}".encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def _cut_off(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "10")
        self.end_headers()
        self.wfile.write(b"abc")
        self.close_connection = True

    do_GET = do_POST = do_DELETE = _echo  # noqa: N815

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "42")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def upstream() -> Iterator[Address]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Address("127.0.0.1", server.server_address[1])
    server.shutdown()
    server.server_close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _call(port: int, method: str, target: str, body: bytes | None = None, chunked: bool = False) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        if chunked and body is not None:
            connection.request(method, target, body=iter([body[:3], body[3:]]), encode_chunked=True)
        else:
            connection.request(method, target, body=body)
        answer = connection.getresponse()
        return answer.status, answer.read()
    finally:
        connection.close()


def _raw(port: int, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(data)
        answer = b""
        while chunk := client.recv(4096):
            answer += chunk
    return answer


def test_proxy_passes_requests_on_and_records_them(upstream: Address) -> None:
    with RecordingProxy(Address("127.0.0.1", 0), upstream) as proxy:
        assert _call(proxy.port, "GET", "/v2/pet/findByStatus?status=sold") == (
            200,
            b"GET /v2/pet/findByStatus?status=sold ",
        )
        assert _call(proxy.port, "POST", "/v2/pet", b'{"id": 1}') == (201, b'POST /v2/pet {"id": 1}')
        exchanges, errors = proxy.log.take()
    assert errors == []
    assert [(e.request.method, e.request.target, e.response.status) for e in exchanges] == [
        ("GET", "/v2/pet/findByStatus?status=sold", 200),
        ("POST", "/v2/pet", 201),
    ]
    assert exchanges[1].request.body.data == b'{"id": 1}'
    assert exchanges[1].response.headers.get("Content-Type") == "text/plain"


def test_proxy_reads_chunked_request_bodies(upstream: Address) -> None:
    with RecordingProxy(Address("127.0.0.1", 0), upstream) as proxy:
        assert _call(proxy.port, "POST", "/v2/pet", b"abcdef", chunked=True) == (201, b"POST /v2/pet abcdef")
        exchanges, _ = proxy.log.take()
    assert exchanges[0].request.body.data == b"abcdef"


def test_take_empties_the_log(upstream: Address) -> None:
    with RecordingProxy(Address("127.0.0.1", 0), upstream) as proxy:
        _call(proxy.port, "GET", "/a")
        assert len(proxy.log.take()[0]) == 1
        assert proxy.log.take() == ([], [])


def test_unreachable_service_gives_502_and_an_error_not_an_exchange() -> None:
    with RecordingProxy(Address("127.0.0.1", 0), Address("127.0.0.1", _free_port())) as proxy:
        status, _ = _call(proxy.port, "GET", "/v2/pet/1")
        exchanges, errors = proxy.log.take()
    assert status == 502
    assert exchanges == []
    assert len(errors) == 1


def test_answers_keep_the_upstream_headers_once_and_head_keeps_its_length(upstream: Address) -> None:
    with RecordingProxy(Address("127.0.0.1", 0), upstream) as proxy:
        connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        try:
            connection.request("GET", "/a")
            got = connection.getresponse()
            got.read()
            names = [name.lower() for name, _ in got.getheaders()]
            connection.request("HEAD", "/a")
            head = connection.getresponse()
            head.read()
        finally:
            connection.close()
    assert names.count("server") == 1
    assert names.count("date") == 1
    assert head.getheader("Content-Length") == "42"


def test_chunk_trailers_do_not_break_the_next_request(upstream: Address) -> None:
    raw = (
        b"POST /v2/pet HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"3\r\nabc\r\n0\r\nX-One: 1\r\nX-Two: 2\r\n\r\n"
        b"GET /a HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    )
    with RecordingProxy(Address("127.0.0.1", 0), upstream) as proxy:
        answer = _raw(proxy.port, raw)
        exchanges, _ = proxy.log.take()
    assert [(e.request.method, e.request.target) for e in exchanges] == [("POST", "/v2/pet"), ("GET", "/a")]
    assert exchanges[0].request.body.data == b"abc"
    assert answer.count(b"HTTP/1.1 ") == 2


def test_answer_cut_off_by_the_service_gives_502_and_an_error(upstream: Address) -> None:
    with RecordingProxy(Address("127.0.0.1", 0), upstream) as proxy:
        status, _ = _call(proxy.port, "GET", "/cut")
        exchanges, errors = proxy.log.take()
    assert status == 502
    assert exchanges == []
    assert len(errors) == 1
    assert errors[0].startswith("GET /cut")


def test_unreadable_request_body_gives_400_and_an_error(upstream: Address) -> None:
    raw = b"POST /x HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\nabc\r\n"
    with RecordingProxy(Address("127.0.0.1", 0), upstream) as proxy:
        answer = _raw(proxy.port, raw)
        exchanges, errors = proxy.log.take()
    assert answer.startswith(b"HTTP/1.1 400")
    assert exchanges == []
    assert len(errors) == 1
    assert "unreadable request body" in errors[0]


class TlsEchoHandler(BaseHTTPRequestHandler):
    """Answers with the method, target, body and the Host and Authorization it got, and sets a cookie."""

    protocol_version = "HTTP/1.1"

    def _echo(self) -> None:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = b""
            while size := int(self.rfile.readline().strip(), 16):
                body += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
        else:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
        answer = (
            f"{self.command} {self.path} {body.decode()} host={self.headers.get('Host')} "
            f"auth={self.headers.get('Authorization')}"
        ).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "session=abc")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    do_GET = do_POST = _echo  # noqa: N815

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def authorities(tmp_path: Path) -> tuple[CertificateAuthority, CertificateAuthority]:
    """The proxy's CA and a second CA that plays the public one the real API uses."""
    return (
        CertificateAuthority.load_or_create(tmp_path / "proxy-ca"),
        CertificateAuthority.load_or_create(tmp_path / "public-ca"),
    )


def _tls_server(authority: CertificateAuthority, host: str) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), TlsEchoHandler)
    server.socket = authority.server_context(host).wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def api_port(authorities: tuple[CertificateAuthority, CertificateAuthority]) -> Iterator[int]:
    yield from _tls_server(authorities[1], "localhost")


@pytest.fixture
def token_port(authorities: tuple[CertificateAuthority, CertificateAuthority]) -> Iterator[int]:
    yield from _tls_server(authorities[1], "127.0.0.1")


@pytest.fixture
def intercepting(authorities: tuple[CertificateAuthority, CertificateAuthority]) -> Iterator[InterceptingProxy]:
    proxy_ca, public_ca = authorities
    upstream_context = ssl.create_default_context(cafile=public_ca.ca_pem)
    proxy = InterceptingProxy(
        Address("127.0.0.1", 0), proxy_ca, passthrough=frozenset({"127.0.0.1"}), upstream_context=upstream_context
    )
    with proxy:
        yield proxy


def _tunnel(proxy: InterceptingProxy, host: str, port: int, ca: Path) -> http.client.HTTPSConnection:
    connection = http.client.HTTPSConnection(
        "127.0.0.1", proxy.port, timeout=5, context=ssl.create_default_context(cafile=ca)
    )
    connection.set_tunnel(host, port)
    return connection


def _ask(connection: http.client.HTTPSConnection, method: str, target: str, **kwargs: Any) -> tuple[int, bytes]:
    connection.request(method, target, **kwargs)
    answer = connection.getresponse()
    return answer.status, answer.read()


def test_intercepting_proxy_records_get_and_post_inside_one_tunnel(
    intercepting: InterceptingProxy, authorities: tuple[CertificateAuthority, CertificateAuthority], api_port: int
) -> None:
    connection = _tunnel(intercepting, "localhost", api_port, authorities[0].ca_pem)
    try:
        status, body = _ask(connection, "GET", "/drive/v3/files?corpora=user", headers={"Authorization": "Bearer t"})
        assert status == 200
        assert body == f"GET /drive/v3/files?corpora=user  host=localhost:{api_port} auth=Bearer t".encode()
        assert _ask(connection, "POST", "/drive/v3/files", body=b'{"a": 1}')[0] == 201
    finally:
        connection.close()
    exchanges, errors = intercepting.log.take()
    assert errors == []
    assert [(e.request.method, e.request.target, e.response.status) for e in exchanges] == [
        ("GET", "/drive/v3/files?corpora=user", 200),
        ("POST", "/drive/v3/files", 201),
    ]
    assert exchanges[1].request.body.data == b'{"a": 1}'


def test_intercepting_proxy_hides_secret_headers_in_the_log_but_not_on_the_wire(
    intercepting: InterceptingProxy, authorities: tuple[CertificateAuthority, CertificateAuthority], api_port: int
) -> None:
    connection = _tunnel(intercepting, "localhost", api_port, authorities[0].ca_pem)
    try:
        connection.request("GET", "/about", headers={"Authorization": "Bearer secret"})
        answer = connection.getresponse()
        body = answer.read()
        cookie = answer.getheader("Set-Cookie")
    finally:
        connection.close()
    assert b"auth=Bearer secret" in body
    assert cookie == "session=abc"
    exchange = intercepting.log.take()[0][0]
    assert exchange.request.headers.get("Authorization") == "<redacted>"
    assert exchange.response.headers.get("Set-Cookie") == "<redacted>"
    assert exchange.response.headers.get("Content-Type") == "text/plain"


def test_intercepting_proxy_reads_chunked_bodies(
    intercepting: InterceptingProxy, authorities: tuple[CertificateAuthority, CertificateAuthority], api_port: int
) -> None:
    connection = _tunnel(intercepting, "localhost", api_port, authorities[0].ca_pem)
    try:
        status, body = _ask(connection, "POST", "/upload", body=iter([b"abc", b"def"]), encode_chunked=True)
    finally:
        connection.close()
    assert (status, body.split(b" host=")[0]) == (201, b"POST /upload abcdef")
    assert intercepting.log.take()[0][0].request.body.data == b"abcdef"


def test_passthrough_hosts_are_forwarded_but_never_logged(
    intercepting: InterceptingProxy, authorities: tuple[CertificateAuthority, CertificateAuthority], token_port: int
) -> None:
    connection = _tunnel(intercepting, "127.0.0.1", token_port, authorities[0].ca_pem)
    try:
        status, body = _ask(connection, "POST", "/token", body=b"refresh_token=secret")
    finally:
        connection.close()
    assert status == 201
    assert body.startswith(b"POST /token refresh_token=secret")
    assert intercepting.log.take() == ([], [])


def test_a_request_without_host_gets_the_tunnel_host_in_the_log(
    intercepting: InterceptingProxy, authorities: tuple[CertificateAuthority, CertificateAuthority], api_port: int
) -> None:
    with socket.create_connection(("127.0.0.1", intercepting.port), timeout=5) as raw:
        raw.sendall(f"CONNECT localhost:{api_port} HTTP/1.1\r\nHost: localhost:{api_port}\r\n\r\n".encode())
        assert raw.recv(4096).startswith(b"HTTP/1.1 200")
        context = ssl.create_default_context(cafile=authorities[0].ca_pem)
        with context.wrap_socket(raw, server_hostname="localhost") as tls:
            tls.sendall(b"GET /about HTTP/1.1\r\nConnection: close\r\n\r\n")
            answer = b""
            while chunk := tls.recv(4096):
                answer += chunk
    assert answer.startswith(b"HTTP/1.1 200")
    exchange = intercepting.log.take()[0][0]
    assert exchange.request.headers.get("Host") == f"localhost:{api_port}"
    assert exchange.request.target == "/about"


def test_a_client_that_does_not_trust_the_proxy_ca_leaves_an_error(
    intercepting: InterceptingProxy, api_port: int
) -> None:
    connection = http.client.HTTPSConnection(
        "127.0.0.1", intercepting.port, timeout=5, context=ssl.create_default_context()
    )
    connection.set_tunnel("localhost", api_port)
    with pytest.raises(ssl.SSLError):
        connection.request("GET", "/about")
    connection.close()
    deadline = time.monotonic() + 3
    errors: list[str] = []
    while not errors and time.monotonic() < deadline:
        errors = intercepting.log.take()[1]
        time.sleep(0.05)
    assert len(errors) == 1
    assert "TLS handshake failed" in errors[0]


def test_plain_requests_to_the_forward_proxy_get_501(intercepting: InterceptingProxy) -> None:
    assert _call(intercepting.port, "GET", "http://example.com/a")[0] == 501


def test_absolute_targets_turn_into_origin_form() -> None:
    assert _TunnelHandler._origin_form("https://www.googleapis.com/drive/v3/files?q=1") == "/drive/v3/files?q=1"
    assert _TunnelHandler._origin_form("https://www.googleapis.com") == "/"
    assert _TunnelHandler._origin_form("/files") == "/files"


def test_jvm_settings_point_the_listener_at_the_running_proxy(intercepting: InterceptingProxy) -> None:
    settings = intercepting.jvm_settings(Path("/x/trust.p12"), "pw")
    assert settings.properties == {
        "lesstests.proxy.host": "127.0.0.1",
        "lesstests.proxy.port": str(intercepting.port),
        "lesstests.truststore": "/x/trust.p12",
        "lesstests.truststore.password": "pw",
    }
    assert settings.listeners == ("lesstests.LesstestsProxyListener",)


def test_a_client_that_drops_its_tunnel_leaves_no_traceback(
    intercepting: InterceptingProxy,
    authorities: tuple[CertificateAuthority, CertificateAuthority],
    api_port: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = socket.create_connection(("127.0.0.1", intercepting.port), timeout=5)
    raw.sendall(f"CONNECT localhost:{api_port} HTTP/1.1\r\n\r\n".encode())
    assert raw.recv(4096).startswith(b"HTTP/1.1 200")
    tls = ssl.create_default_context(cafile=authorities[0].ca_pem).wrap_socket(raw, server_hostname="localhost")

    # A zero linger close sends a reset, the way an exiting JVM drops its connections.
    tls.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    tls.close()
    time.sleep(0.3)
    assert "Traceback" not in capsys.readouterr().err
