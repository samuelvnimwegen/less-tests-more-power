import json
import os
from pathlib import Path
from typing import Any

import pytest
from fake_restcov import FakeRestCov

from lesstests import cli
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest, Recording


def _recording(folder: Path) -> Recording:
    recording = Recording(folder)
    for name in ("a", "b", "c", "d"):
        request = HttpRequest("GET", f"/v2/{name}", Headers(), Body())
        exchange = Exchange(request, HttpResponse(200, "OK", Headers(), Body()))
        recording.save(RecordedTest(JavaTest("suite.Api", name), Outcome.PASSED, 1.0, [exchange]))
    recording.write_meta({"spec": str(folder / "spec.json")})
    return recording


@pytest.fixture
def restcov(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeRestCov, list[tuple[Path, Path]]]:
    fake = FakeRestCov()
    made: list[tuple[Path, Path]] = []

    def build(script: Path, spec: Path) -> FakeRestCov:
        made.append((script, spec))
        return fake

    monkeypatch.setattr(cli, "RestCov", build)
    return fake, made


def test_select_measures_the_suite_once_then_reuses_the_saved_coverage(
    tmp_path: Path, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    fake, _ = restcov
    recording = _recording(tmp_path / "rec")
    args = ["select", "--recording", str(recording.folder), "--restcov", "restcov.py", "--budgets", "50"]
    args += ["--draws", "2"]

    # A cache miss measures 4 single tests and the whole suite, then 1 selected and 2 random versions.
    cli.main(args)
    assert len(fake.measured) == 8
    assert (recording.folder / "coverage" / "coverage.json").exists()
    result = json.loads((recording.folder / "selection" / "selection.json").read_text())
    assert {version["budget"] for version in result["versions"]} == {0.5}
    assert {version["kept"] for version in result["versions"]} == {2}

    # A cache hit only measures the 3 smaller versions again.
    fake.measured.clear()
    cli.main(args)
    assert all(recording.folder / "selection" in logs.parents for logs in fake.measured)
    assert len(fake.measured) == 3


def test_select_measures_again_when_the_recording_changed(
    tmp_path: Path, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    fake, _ = restcov
    recording = _recording(tmp_path / "rec")
    args = ["select", "--recording", str(recording.folder), "--restcov", "restcov.py", "--budgets", "50"]
    args += ["--draws", "2"]
    cli.main(args)
    saved = recording.folder / "coverage" / "coverage.json"

    # A new test: 5 single tests, the whole suite and 3 versions.
    exchange = Exchange(HttpRequest("GET", "/v2/e", Headers(), Body()), HttpResponse(200, "OK", Headers(), Body()))
    recording.save(RecordedTest(JavaTest("suite.Api", "e"), Outcome.PASSED, 1.0, [exchange]))
    fake.measured.clear()
    cli.main(args)
    assert len(fake.measured) == 9
    assert json.loads((recording.folder / "selection" / "selection.json").read_text())["tests"] == 5

    # The same test recorded again. The saved file is pushed back in time, so the order doesn't hang on the clock.
    recording.save(RecordedTest(JavaTest("suite.Api", "a"), Outcome.PASSED, 1.0, [exchange]))
    os.utime(saved, (0, 0))
    fake.measured.clear()
    cli.main(args)
    assert len(fake.measured) == 9
    assert json.loads(saved.read_text())["tests"]["suite.Api.a"] == [["path", "GET /v2/e HTTP/1.1"]]


def test_coverage_takes_restcov_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    _, made = restcov
    recording = _recording(tmp_path / "rec")
    monkeypatch.setenv("LESSTESTS_RESTCOV", str(tmp_path / "from-env" / "restcov.py"))
    cli.main(["coverage", "--recording", str(recording.folder)])
    assert made == [((tmp_path / "from-env" / "restcov.py").resolve(), tmp_path / "rec" / "spec.json")]
    assert (recording.folder / "coverage" / "coverage.json").exists()


def test_coverage_without_restcov_stops_with_a_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LESSTESTS_RESTCOV", raising=False)
    recording = _recording(tmp_path / "rec")
    with pytest.raises(SystemExit, match="LESSTESTS_RESTCOV"):
        cli.main(["coverage", "--recording", str(recording.folder)])


class FakeMaven:
    def __init__(self, source: Path, workdir: Path) -> None:
        self.source = source
        self.workdir = workdir

    def prepare(self) -> None:
        pass


class FakeRecorder:
    made: list["FakeRecorder"] = []

    def __init__(self, suite: Any, runner: Any, service: Any, proxy: Any, recording: Recording) -> None:
        self.suite = suite
        self.service = service
        self.recording = recording
        self.meta: dict[str, Any] = {}
        FakeRecorder.made.append(self)

    def run(self, meta: dict[str, Any], progress: Any = None) -> None:
        self.meta = meta


def test_record_wires_the_subject_and_suite_into_the_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "MavenProject", FakeMaven)
    monkeypatch.setattr(cli, "Recorder", FakeRecorder)
    FakeRecorder.made.clear()
    out = tmp_path / "rec"
    cli.main(["record", "--project", str(tmp_path / "petstore"), "--suite", "multi-agent", "--out", str(out)])

    recorder = FakeRecorder.made[0]
    assert recorder.suite.package == "generations.multi_agent_chain"
    assert recorder.suite.project == out.resolve() / "project"
    assert recorder.recording.folder == out.resolve()
    assert recorder.service.address.port == 8082
    assert recorder.meta["suite"] == "multi-agent"
    assert recorder.meta["spec"] == str((tmp_path / "petstore").resolve() / "specification-petstore.json")
