import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lesstests.service import ContainerSpec, DockerService

# The health check answers these in order, then 200 for good.
ANSWERS: list[int] = []
HITS: list[str] = []


class HealthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        HITS.append(self.path)
        self.send_response(ANSWERS.pop(0) if ANSWERS else 200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def health_port() -> Iterator[int]:
    ANSWERS.clear()
    HITS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str, check: bool = True) -> None:
        calls.append(args)

    monkeypatch.setattr(DockerService, "_docker", staticmethod(fake_docker))
    return calls


SPEC = ContainerSpec(image="petstore", port=8080, health_path="/health", env={"BASE": "/v2"})


def test_start_replaces_the_container_and_waits_for_a_200(health_port: int, docker: list[tuple[str, ...]]) -> None:
    ANSWERS.extend([503, 503])
    DockerService(SPEC, health_port, "lesstests-test").start()
    assert docker == [
        ("rm", "-f", "lesstests-test"),
        ("run", "-d", "--name", "lesstests-test", "-e", "BASE=/v2", "-p", f"127.0.0.1:{health_port}:8080", "petstore"),
    ]
    assert HITS == ["/health"] * 3


def test_reset_restarts_and_waits_again(health_port: int, docker: list[tuple[str, ...]]) -> None:
    ANSWERS.append(503)
    DockerService(SPEC, health_port, "lesstests-test").reset()
    assert docker == [("restart", "-t", "1", "lesstests-test")]
    assert len(HITS) == 2


def test_stop_removes_the_container(docker: list[tuple[str, ...]]) -> None:
    DockerService(SPEC, 1, "lesstests-test").stop()
    assert docker == [("rm", "-f", "lesstests-test")]


def test_a_service_that_never_answers_times_out(docker: list[tuple[str, ...]]) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    with pytest.raises(TimeoutError):
        DockerService(SPEC, free_port, "lesstests-test", ready_timeout=0.5).start()
