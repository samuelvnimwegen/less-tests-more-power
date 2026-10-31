import http.client
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lesstests.proxy import Address, RecordingProxy


class EchoHandler(BaseHTTPRequestHandler):
    """Answers every request with its method, path and body, the way a tiny service would."""

    protocol_version = "HTTP/1.1"

    def _echo(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        answer = f"{self.command} {self.path} {body.decode()}".encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    do_GET = do_POST = do_DELETE = _echo  # noqa: N815

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
