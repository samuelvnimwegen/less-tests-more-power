import contextlib
import http.client
import socket
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JvmSettings, MavenProject
from lesstests.tls import CertificateAuthority

HOP_BY_HOP = ("connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade")


@dataclass(frozen=True, slots=True)
class Address:
    """A host and port on this machine."""

    host: str
    port: int


@dataclass(frozen=True, slots=True)
class Redactor:
    """Hides secret header values in what gets logged, so a recording never holds a token."""

    names: frozenset[str]

    def apply(self, exchange: Exchange) -> Exchange:
        """Replace the values of the secret headers by `<redacted>`, in the request and in the answer.

        Returns:
            The exchange as it may be stored.
        """
        request, response = exchange.request, exchange.response
        return Exchange(
            HttpRequest(request.method, request.target, self._hide(request.headers), request.body),
            HttpResponse(response.status, response.reason, self._hide(response.headers), response.body),
        )

    def _hide(self, headers: Headers) -> Headers:
        return Headers(
            tuple((key, "<redacted>" if key.lower() in self.names else value) for key, value in headers.pairs)
        )


class _LoggingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, listen: Address, handler: type[BaseHTTPRequestHandler], log: "ExchangeLog") -> None:
        super().__init__((listen.host, listen.port), handler)
        self.log = log


class _ForwardingHandler(BaseHTTPRequestHandler):
    """Passes one request on to the upstream and hands the exchange to the proxy."""

    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> _LoggingServer:
        return cast("_LoggingServer", self.server)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the console quiet, the recording holds every exchange."""

    def _forward(self) -> None:
        try:
            body = self._read_body()
        except (OSError, ValueError) as error:
            self._proxy.log.fail(f"{self._label()}: unreadable request body: {error}")

            # The rest of a broken body is still on the connection, so it can't carry another request.
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.send_error(400, "Unreadable request body")
            return
        request = self._request(Body(body))
        try:
            response = self._send_upstream(request)
        except (OSError, http.client.HTTPException) as error:
            self._proxy.log.fail(f"{self._label()}: {error}")
            self.send_error(502, "Upstream service unreachable")
            return
        self._keep(Exchange(request, response))

        # The plain send_response adds its own Server and Date, which would double the upstream ones.
        self.send_response_only(response.status, response.reason)
        if self.command == "HEAD":
            for key, value in response.headers.without(*HOP_BY_HOP).pairs:
                self.send_header(key, value)
            self.end_headers()
            return
        for key, value in response.headers.without(*HOP_BY_HOP, "content-length").pairs:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body.data)))
        self.end_headers()
        self.wfile.write(response.body.data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _forward  # noqa: N815

    def _label(self) -> str:
        """Name the request in an error message.

        Returns:
            The method and target.
        """
        return f"{self.command} {self.path}"

    def _request(self, body: Body) -> HttpRequest:
        """Build the request as it goes upstream and into the log.

        Returns:
            The request.
        """
        return HttpRequest(self.command, self.path, Headers(tuple(self.headers.items())), body)

    def _keep(self, exchange: Exchange) -> None:
        """Store one finished exchange."""
        self._proxy.log.add(exchange)

    def _connect(self) -> http.client.HTTPConnection:
        """Open a fresh connection to where the request goes.

        Returns:
            The connection, not yet connected.
        """
        server = cast("_ProxyServer", self.server)
        return http.client.HTTPConnection(server.upstream.host, server.upstream.port, timeout=server.timeout)

    def _send_upstream(self, request: HttpRequest) -> HttpResponse:
        connection = self._connect()
        try:
            return self._talk(connection, request)
        finally:
            connection.close()

    @staticmethod
    def _talk(connection: http.client.HTTPConnection, request: HttpRequest) -> HttpResponse:
        """Send one request on a connection and read the whole answer.

        Returns:
            The answer.
        """
        connection.putrequest(request.method, request.target, skip_host=True, skip_accept_encoding=True)
        for key, value in request.headers.without(*HOP_BY_HOP, "content-length").pairs:
            connection.putheader(key, value)
        if request.body.data or request.method in ("POST", "PUT", "PATCH"):
            connection.putheader("Content-Length", str(len(request.body.data)))
        connection.endheaders(request.body.data or None)
        answer = connection.getresponse()
        return HttpResponse(answer.status, answer.reason, Headers(tuple(answer.getheaders())), Body(answer.read()))

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
                self._skip_trailers()
                return bytes(data)
            data += self.rfile.read(size)
            self.rfile.readline()

    def _skip_trailers(self) -> None:
        """Read the trailer headers up to the blank line, so the next request on the connection starts clean."""
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass


class _ProxyServer(_LoggingServer):
    def __init__(self, listen: Address, upstream: Address, log: "ExchangeLog", timeout: float) -> None:
        super().__init__(listen, _ForwardingHandler, log)
        self.upstream = upstream
        self.timeout = timeout


class _InterceptingServer(_LoggingServer):
    def __init__(
        self,
        listen: Address,
        log: "ExchangeLog",
        authority: CertificateAuthority,
        passthrough: frozenset[str],
        redactor: Redactor,
        upstream_context: ssl.SSLContext,
    ) -> None:
        super().__init__(listen, _ConnectHandler, log)
        self.authority = authority
        self.passthrough = passthrough
        self.redactor = redactor
        self.upstream_context = upstream_context


class _TunnelHandler(_ForwardingHandler):
    """Serves the requests inside one CONNECT tunnel, after the proxy took over its TLS."""

    def __init__(
        self, connection: ssl.SSLSocket, client: tuple[str, int], server: _InterceptingServer, host: str, port: int
    ) -> None:
        # The base constructor serves the whole connection, so the target has to be known before it runs.
        self._host = host
        self._port = port
        self._open: http.client.HTTPConnection | None = None
        try:
            super().__init__(connection, client, server)
        finally:
            if self._open is not None:
                self._open.close()

    @property
    def _intercepting(self) -> _InterceptingServer:
        return cast("_InterceptingServer", self.server)

    def handle(self) -> None:
        """Serve requests until the client goes. A JVM that exits drops its TLS without a goodbye, which is fine."""
        with contextlib.suppress(ConnectionResetError, BrokenPipeError, ssl.SSLError):
            super().handle()

    @property
    def _authority(self) -> str:
        return self._host if self._port == 443 else f"{self._host}:{self._port}"

    def _label(self) -> str:
        return f"{self.command} https://{self._authority}{self._origin_form(self.path)}"

    def _request(self, body: Body) -> HttpRequest:
        headers = Headers(tuple(self.headers.items()))
        if headers.get("Host") is None:
            headers = Headers((("Host", self._authority), *headers.pairs))
        return HttpRequest(self.command, self._origin_form(self.path), headers, body)

    def _keep(self, exchange: Exchange) -> None:
        if self._host.lower() not in self._intercepting.passthrough:
            self._proxy.log.add(self._intercepting.redactor.apply(exchange))

    def _connect(self) -> http.client.HTTPConnection:
        context = self._intercepting.upstream_context
        return http.client.HTTPSConnection(self._host, self._port, timeout=60, context=context)

    def _send_upstream(self, request: HttpRequest) -> HttpResponse:
        """Reuse one upstream connection per tunnel, so every request doesn't pay for a new TLS handshake.

        Returns:
            The answer.

        Raises:
            RemoteDisconnected: If a fresh connection closes before it answers.
            ConnectionResetError: If a fresh connection is reset.
            BrokenPipeError: If a fresh connection breaks while sending.
        """
        reused = self._open is not None
        if self._open is None:
            self._open = self._connect()
        try:
            return self._talk(self._open, request)
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
            self._open.close()
            self._open = None
            if not reused:
                raise

            # The host closed an idle connection before reading the request, so one fresh try is safe.
            self._open = self._connect()
            return self._talk(self._open, request)

    @staticmethod
    def _origin_form(target: str) -> str:
        """Turn `https://host/a?b` into `/a?b`, the form RESTCov reads.

        Returns:
            The path with its query.
        """
        if "://" not in target:
            return target
        rest = target.split("://", 1)[1]
        return "/" + rest.split("/", 1)[1] if "/" in rest else "/"


class _ConnectHandler(BaseHTTPRequestHandler):
    """Answers CONNECT and takes over the TLS of the tunnel. Plain proxy requests are not supported."""

    protocol_version = "HTTP/1.1"

    @property
    def _intercepting(self) -> _InterceptingServer:
        return cast("_InterceptingServer", self.server)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the console quiet."""

    def do_CONNECT(self) -> None:  # noqa: N802
        """Open the tunnel, show the client a certificate for the host and serve the requests inside."""
        host, _, port = self.path.rpartition(":")
        if not host or not port.isdigit():
            self.send_error(400, "CONNECT needs host:port")
            return
        host = host.strip("[]")
        self.send_response_only(200, "Connection Established")
        self.end_headers()
        self.close_connection = True
        try:
            tunnel = self._intercepting.authority.server_context(host).wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError) as error:
            self._intercepting.log.fail(f"CONNECT {self.path}: TLS handshake failed: {error}")
            return
        try:
            _TunnelHandler(tunnel, self.client_address, self._intercepting, host, int(port))
        finally:
            with contextlib.suppress(OSError):
                tunnel.shutdown(socket.SHUT_RDWR)
            tunnel.close()

    def _refuse(self) -> None:
        self.send_error(501, "Only CONNECT tunnels are supported")

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse  # noqa: N815


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
        """Store a request that got no full answer from the service."""
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


class Proxy:
    """Runs a logging server in a background thread. Subclasses say which server, a reverse or an intercepting one."""

    def __init__(self, listen: Address) -> None:
        """Set up the proxy without opening the port yet."""
        self._listen = listen
        self.log = ExchangeLog()
        self._server: _LoggingServer | None = None
        self._thread: threading.Thread | None = None

    def _make_server(self) -> _LoggingServer:
        """Build the server that listens on the port.

        Returns:
            The server.
        """
        raise NotImplementedError

    def start(self) -> None:
        """Open the port and serve in a background thread."""
        self._server = self._make_server()
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


class RecordingProxy(Proxy):
    """A reverse proxy that sits on the port the tests call and records every exchange.

    Example: the tests call `localhost:8081`, so the proxy listens there and forwards to the service on 8082.
    """

    def __init__(self, listen: Address, upstream: Address, timeout: float = 60.0) -> None:
        """Set up the proxy without opening the port yet. `timeout` is how long one answer may take."""
        super().__init__(listen)
        self._upstream = upstream
        self._timeout = timeout

    def _make_server(self) -> _LoggingServer:
        return _ProxyServer(self._listen, self._upstream, self.log, self._timeout)


class InterceptingProxy(Proxy):
    """A forward proxy for HTTPS that reads inside each tunnel, for tests that call a live API by its real name.

    The tests trust a private CA, so the proxy can show them its own certificate for any host. Requests to a
    `passthrough` host are forwarded but never logged, and the `REDACTED_HEADERS` are hidden in the log.
    """

    REDACTED_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})

    def __init__(
        self,
        listen: Address,
        authority: CertificateAuthority,
        passthrough: frozenset[str] = frozenset(),
        upstream_context: ssl.SSLContext | None = None,
    ) -> None:
        """Set up the proxy without opening the port yet."""
        super().__init__(listen)
        self._authority = authority
        self._passthrough = frozenset(host.lower() for host in passthrough)
        self._redactor = Redactor(self.REDACTED_HEADERS)
        self._upstream_context = upstream_context or ssl.create_default_context()

    def _make_server(self) -> _LoggingServer:
        return _InterceptingServer(
            self._listen, self.log, self._authority, self._passthrough, self._redactor, self._upstream_context
        )

    def jvm_settings(self, truststore: Path, password: str) -> JvmSettings:
        """Tell a test JVM to send RestAssured through this proxy and trust its CA.

        A fixed port is known before the proxy starts. Port 0 needs a running proxy to learn the real one.

        Returns:
            The settings, with the port the proxy listens on.
        """
        return JvmSettings(
            properties={
                "lesstests.proxy.host": self._listen.host,
                "lesstests.proxy.port": str(self._listen.port or self.port),
                "lesstests.truststore": str(truststore),
                "lesstests.truststore.password": password,
            },
            listeners=(MavenProject.LISTENER,),
        )
