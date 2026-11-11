import http.client
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaSuite, JavaTest, JvmRunner, MavenProject, Outcome, RunResult
from lesstests.proxy import Address, RecordingProxy
from lesstests.recording import RecordedTest, Recorder, Recording

SUITE = """package suite;

public class Api {
    @Test public void a() {}
    @Test public void b() {}
    @Test public void c() {}
}
"""


def _record(method: str, outcome: Outcome, statuses: list[int]) -> RecordedTest:
    exchanges = [
        Exchange(
            HttpRequest("GET", f"/v2/pet/{number}", Headers((("Host", "localhost:8081"),)), Body()),
            HttpResponse(status, "", Headers((("Content-Type", "application/json"),)), Body(b"{}")),
        )
        for number, status in enumerate(statuses)
    ]
    return RecordedTest(JavaTest("suite.Pet", method), outcome, 1.25, exchanges)


def test_saved_records_come_back_with_their_logs(tmp_path: Path) -> None:
    recording = Recording(tmp_path)
    first, second = _record("b", Outcome.PASSED, [200, 404]), _record("a", Outcome.FAILED, [])
    recording.save(first)
    recording.save(second)
    assert recording.has(first.test)
    assert [record.test.method for record in recording.records()] == ["a", "b"]
    assert recording.records()[1].exchanges == first.exchanges
    assert sorted(path.name for path in recording.logs_of(first.test).iterdir()) == [
        "1-request.txt",
        "1-response.txt",
        "2-request.txt",
        "2-response.txt",
    ]
    assert list(recording.logs_of(second.test).iterdir()) == []


def test_saving_again_replaces_old_logs(tmp_path: Path) -> None:
    recording = Recording(tmp_path)
    recording.save(_record("a", Outcome.PASSED, [200, 200, 200]))
    recording.save(_record("a", Outcome.PASSED, [200]))
    assert len(list(recording.logs_of(JavaTest("suite.Pet", "a")).iterdir())) == 2


def test_gathered_logs_hold_one_folder_per_test(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    tests = [_record("a", Outcome.PASSED, [200]), _record("b", Outcome.PASSED, [201])]
    for record in tests:
        recording.save(record)
    target = recording.gather_logs([tests[1].test], tmp_path / "joined")
    assert [path.name for path in target.iterdir()] == ["suite.Pet.b"]


class OkHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def upstream() -> Iterator[Address]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Address("127.0.0.1", server.server_address[1])
    server.shutdown()
    server.server_close()


class FakeService:
    """Logs its calls. A reset leaves a stale exchange behind, like a late answer from the test before."""

    def __init__(self, upstream: Address, proxy: RecordingProxy, start_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._upstream = upstream
        self._proxy = proxy
        self._start_error = start_error

    @property
    def address(self) -> Address:
        return self._upstream

    def start(self) -> None:
        self.calls.append("start")
        if self._start_error is not None:
            raise self._start_error

    def reset(self) -> None:
        self.calls.append("reset")
        stale = HttpRequest("GET", "/stale", Headers(), Body())
        self._proxy.log.add(Exchange(stale, HttpResponse(200, "OK", Headers(), Body())))

    def stop(self) -> None:
        self.calls.append("stop")


class FakeRunner(JvmRunner):
    """Sends one request through the proxy in place of starting a JVM."""

    def __init__(self, tmp_path: Path, proxy: RecordingProxy, calls: list[str]) -> None:
        super().__init__(MavenProject(tmp_path, tmp_path), tmp_path)
        self._proxy = proxy
        self._calls = calls

    def run(self, test: JavaTest) -> RunResult:
        self._calls.append(f"run {test.method}")
        connection = http.client.HTTPConnection("127.0.0.1", self._proxy.port, timeout=5)
        try:
            connection.request("GET", f"/{test.method}")
            connection.getresponse().read()
        finally:
            connection.close()
        return RunResult(Outcome.PASSED, 0.5)


def _suite(tmp_path: Path) -> JavaSuite:
    folder = tmp_path / "project" / "src" / "test" / "java" / "suite"
    folder.mkdir(parents=True)
    (folder / "Api.java").write_text(SUITE)
    return JavaSuite(tmp_path / "project", "suite")


def test_recorder_skips_recorded_tests_resets_before_each_and_stops_last(tmp_path: Path, upstream: Address) -> None:
    recording = Recording(tmp_path / "rec")
    recording.save(RecordedTest(JavaTest("suite.Api", "b"), Outcome.FAILED, 1.0))
    proxy = RecordingProxy(Address("127.0.0.1", 0), upstream)
    service = FakeService(upstream, proxy)
    runner = FakeRunner(tmp_path, proxy, service.calls)
    Recorder(_suite(tmp_path), runner, service, proxy, recording).run({"suite": "x"})

    assert service.calls == ["start", "reset", "run a", "reset", "run c", "stop"]
    records = {record.test.method: record for record in recording.records()}
    assert [exchange.request.target for exchange in records["a"].exchanges] == ["/a"]
    assert [exchange.request.target for exchange in records["c"].exchanges] == ["/c"]
    assert records["b"].outcome is Outcome.FAILED
    assert recording.meta()["tests"] == 3
    assert "finished" in recording.meta()


def test_recorder_stops_the_service_when_its_start_fails(tmp_path: Path, upstream: Address) -> None:
    proxy = RecordingProxy(Address("127.0.0.1", 0), upstream)
    service = FakeService(upstream, proxy, TimeoutError("never answered"))
    runner = FakeRunner(tmp_path, proxy, service.calls)
    recorder = Recorder(_suite(tmp_path), runner, service, proxy, Recording(tmp_path / "rec"))
    with pytest.raises(TimeoutError):
        recorder.run({})
    assert service.calls == ["start", "stop"]


def test_resume_with_other_facts_stops_before_anything_runs(tmp_path: Path, upstream: Address) -> None:
    recording = Recording(tmp_path / "rec")
    recording.write_meta({"suite": "single-agent", "started": "old"})
    before = (recording.folder / "meta.json").read_text()
    proxy = RecordingProxy(Address("127.0.0.1", 0), upstream)
    service = FakeService(upstream, proxy)
    recorder = Recorder(_suite(tmp_path), FakeRunner(tmp_path, proxy, service.calls), service, proxy, recording)
    with pytest.raises(ValueError, match="suite"):
        recorder.run({"suite": "multi-agent"})
    assert service.calls == []
    assert (recording.folder / "meta.json").read_text() == before


def test_resume_with_a_test_that_left_the_suite_stops(tmp_path: Path, upstream: Address) -> None:
    recording = Recording(tmp_path / "rec")
    recording.save(RecordedTest(JavaTest("suite.Api", "gone"), Outcome.PASSED, 1.0))
    proxy = RecordingProxy(Address("127.0.0.1", 0), upstream)
    service = FakeService(upstream, proxy)
    recorder = Recorder(_suite(tmp_path), FakeRunner(tmp_path, proxy, service.calls), service, proxy, recording)
    with pytest.raises(ValueError, match="suite.Api.gone"):
        recorder.run({})
    assert service.calls == []


def test_resume_keeps_the_first_start_time(tmp_path: Path, upstream: Address) -> None:
    recording = Recording(tmp_path / "rec")
    recording.write_meta({"suite": "x", "started": "old"})
    proxy = RecordingProxy(Address("127.0.0.1", 0), upstream)
    service = FakeService(upstream, proxy)
    recorder = Recorder(_suite(tmp_path), FakeRunner(tmp_path, proxy, service.calls), service, proxy, recording)
    recorder.run({"suite": "x"})
    assert recording.meta()["started"] == "old"
    assert "finished" in recording.meta()


def test_a_resume_ignores_the_facts_every_run_writes_itself(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "round")
    recording.write_meta({"suite": "single-agent", "tests": 1, "started": "18:00", "finished": "18:01"})

    # A merge round gets the facts of the recording it came from, with that recording's own counts and times.
    recording.check_resume({"suite": "single-agent", "tests": 7, "started": "17:00", "finished": "17:30"}, [])
    with pytest.raises(ValueError, match="another suite"):
        recording.check_resume({"suite": "multi-agent", "tests": 1}, [])
