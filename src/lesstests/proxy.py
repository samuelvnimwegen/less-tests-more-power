import http.client
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Self, cast

from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse

HOP_BY_HOP = ("connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade")


@dataclass(frozen=True, slots=True)
class Address:
    """A host and port on this machine."""

    host: str
    port: int


class _ForwardingHandler(BaseHTTPRequestHandler):
    """Passes one request on to the service and hands the exchange to the proxy."""

    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> "_ProxyServer":
        return cast("_ProxyServer", self.server)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the console quiet, the recording holds every exchange."""

    def _forward(self) -> None:
        request = HttpRequest(self.command, self.path, Headers(tuple(self.headers.items())), Body(self._read_body()))
        try:
            response = self._send_upstream(request)
        except OSError as error:
            self._proxy.log.fail(f"{request.method} {request.target}: {error}")
            self.send_error(502, "Upstream service unreachable")
            return
        self._proxy.log.add(Exchange(request, response))
        self.send_response(response.status, response.reason)
        for key, value in response.headers.without(*HOP_BY_HOP, "content-length").pairs:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body.data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body.data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _forward  # noqa: N815

    def _read_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            return self._read_chunked()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _read_chunked(self) -> bytes:
        data = bytearray()
        while True:
            size = int(self.rfile.readline().split(b";")[0].strip(), 16)
            if size == 0:
                self.rfile.readline()
                return bytes(data)
            data += self.rfile.read(size)
            self.rfile.readline()

    def _send_upstream(self, request: HttpRequest) -> HttpResponse:
        upstream = self._proxy.upstream
        connection = http.client.HTTPConnection(upstream.host, upstream.port, timeout=60)
        try:
            connection.putrequest(request.method, request.target, skip_host=True, skip_accept_encoding=True)
            for key, value in request.headers.without(*HOP_BY_HOP, "content-length").pairs:
                connection.putheader(key, value)
            if request.body.data or request.method in ("POST", "PUT", "PATCH"):
                connection.putheader("Content-Length", str(len(request.body.data)))
            connection.endheaders(request.body.data or None)
            answer = connection.getresponse()
            return HttpResponse(answer.status, answer.reason, Headers(tuple(answer.getheaders())), Body(answer.read()))
        finally:
            connection.close()


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, listen: Address, upstream: Address, log: "ExchangeLog") -> None:
        super().__init__((listen.host, listen.port), _ForwardingHandler)
        self.upstream = upstream
        self.log = log


class ExchangeLog:
    """The exchanges and errors the proxy saw since the last `take`."""

    def __init__(self) -> None:
        """Start empty."""
        self._lock = threading.Lock()
        self._exchanges: list[Exchange] = []
        self._errors: list[str] = []

    def add(self, exchange: Exchange) -> None:
        """Store one exchange."""
        with self._lock:
            self._exchanges.append(exchange)

    def fail(self, message: str) -> None:
        """Store a request that never reached the service."""
        with self._lock:
            self._errors.append(message)

    def take(self) -> tuple[list[Exchange], list[str]]:
        """Hand over everything seen so far and start empty again.

        Returns:
            The exchanges in arrival order and the error messages.
        """
        with self._lock:
            taken = (self._exchanges, self._errors)
            self._exchanges, self._errors = [], []
            return taken


class RecordingProxy:
    """A reverse proxy that sits on the port the tests call and records every exchange.

    Example: the tests call `localhost:8081`, so the proxy listens there and forwards to the service on 8082.
    """

    def __init__(self, listen: Address, upstream: Address) -> None:
        """Set up the proxy without opening the port yet."""
        self._listen = listen
        self._upstream = upstream
        self.log = ExchangeLog()
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Open the port and serve in a background thread."""
        self._server = _ProxyServer(self._listen, self._upstream, self.log)
        self._thread = threading.Thread(target=self._server.serve_forever, name="lesstests-proxy", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Close the port and wait for the thread."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        """The port the proxy really listens on, which differs from the asked one only when that was 0.

        Raises:
            RuntimeError: If the proxy is not running.
        """
        if self._server is None:
            raise RuntimeError("The proxy is not running")
        return self._server.server_address[1]

    def __enter__(self) -> Self:
        """Start the proxy.

        Returns:
            The running proxy.
        """
        self.start()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        """Stop the proxy."""
        self.stop()
