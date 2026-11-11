import json
import os
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lesstests.service import ContainerSpec, DockerService, RemoteService
from lesstests.subjects import PETSTORE

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


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_service_that_never_answers_times_out(docker: list[tuple[str, ...]]) -> None:
    with pytest.raises(TimeoutError):
        DockerService(SPEC, _free_port(), "lesstests-test", ready_timeout=0.5).start()


def _status(url: str, body: dict[str, object] | None = None) -> int:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("LESSTESTS_LIVE") != "1", reason="needs Docker and the PetStore image")
def test_reset_puts_petstore_back_to_its_sample_data() -> None:
    service = DockerService(PETSTORE.container, _free_port(), "lesstests-live-reset")
    pets = f"http://{service.address.host}:{service.address.port}/v2/pet"
    service.start()
    try:
        assert _status(f"{pets}/1") == 200
        assert _status(pets, {"id": 987654, "name": "live", "photoUrls": [], "status": "available"}) == 200
        assert _status(f"{pets}/987654") == 200
        service.reset()
        assert _status(f"{pets}/987654") == 404
        assert _status(f"{pets}/1") == 200
    finally:
        service.stop()


def test_a_remote_service_names_its_host_and_never_touches_it(docker: list[tuple[str, ...]]) -> None:
    service = RemoteService("www.googleapis.com")
    service.start()
    service.reset()
    service.stop()
    assert (service.address.host, service.address.port) == ("www.googleapis.com", 443)
    assert docker == []
