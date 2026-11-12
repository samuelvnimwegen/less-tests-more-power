import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lesstests.service import ContainerSpec, DatabaseService, DockerService, PostgresSpec, RemoteService
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


APP = ContainerSpec("fineract", 8080, "/health", {"DB": "db"}, ("--add-host=h:host-gateway",))
DATABASE = PostgresSpec("postgres:18", ("tenants", "default"), password="pw")


class FakeDocker:
    """Records every docker command. `pg_isready` fails the first `not_ready` times."""

    def __init__(self, not_ready: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._not_ready = not_ready

    def __call__(self, *args: str, check: bool = True) -> None:
        self.calls.append(args)
        if "pg_isready" in args and self._not_ready:
            self._not_ready -= 1
            raise subprocess.CalledProcessError(1, ["docker", *args])

    def sql(self) -> list[str]:
        return [call[-1] for call in self.calls if "psql" in call]

    def plain(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] != "exec"]


def test_a_database_service_fills_the_databases_once_and_keeps_a_template_of_each(health_port: int) -> None:
    docker = FakeDocker(not_ready=2)
    DatabaseService(APP, DATABASE, health_port, "lt", docker=docker).start()

    assert docker.plain()[:3] == [
        ("rm", "-f", "lt", "lt-db"),
        ("network", "rm", "lt"),
        ("network", "create", "lt"),
    ]
    postgres = docker.plain()[3]
    assert postgres[-1] == "postgres:18"
    assert {"--network-alias", "db", "POSTGRES_PASSWORD=pw"} <= set(postgres)
    app = docker.plain()[4]
    assert app[:4] == ("run", "-d", "--name", "lt")
    assert app[-3:] == ("-p", f"127.0.0.1:{health_port}:8080", "fineract")
    assert {"--network", "DB=db", "--add-host=h:host-gateway"} <= set(app)
    assert docker.plain()[5:] == [("stop", "-t", "2", "lt"), ("start", "lt")]
    assert [call for call in docker.calls if "pg_isready" in call] != []
    assert docker.sql() == [
        'CREATE DATABASE "tenants"',
        'CREATE DATABASE "default"',
        'CREATE DATABASE "lesstests_start_tenants" TEMPLATE "tenants"',
        'CREATE DATABASE "lesstests_start_default" TEMPLATE "default"',
    ]

    # The app answers twice: once before the templates exist and once after the restart.
    assert len(HITS) == 2


def test_a_database_reset_stops_the_app_before_it_swaps_the_databases(health_port: int) -> None:
    docker = FakeDocker()
    DatabaseService(APP, DATABASE, health_port, "lt", docker=docker).reset()
    assert docker.calls[0] == ("stop", "-t", "2", "lt")
    assert docker.sql() == [
        'DROP DATABASE IF EXISTS "tenants" WITH (FORCE)',
        'CREATE DATABASE "tenants" TEMPLATE "lesstests_start_tenants"',
        'DROP DATABASE IF EXISTS "default" WITH (FORCE)',
        'CREATE DATABASE "default" TEMPLATE "lesstests_start_default"',
    ]
    assert docker.calls[-1] == ("start", "lt")
    assert HITS == ["/health"]


def test_a_database_service_stop_removes_both_containers_and_the_network() -> None:
    docker = FakeDocker()
    DatabaseService(APP, DATABASE, 1, "lt", docker=docker).stop()
    assert docker.calls == [("rm", "-f", "lt", "lt-db"), ("network", "rm", "lt")]


def test_a_database_that_never_takes_connections_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter(range(0, 1000, 60))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    service = DatabaseService(APP, DATABASE, 1, "lt", docker=FakeDocker(not_ready=100))
    with pytest.raises(TimeoutError, match="lt-db never took connections"):
        service.start()
