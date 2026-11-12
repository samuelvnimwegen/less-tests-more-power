import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fake_restcov import FakeRestCov

from lesstests import cli, subjects
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, MavenProject, Outcome
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

    def build(script: Path, spec: Path, base_path: str | None = None) -> FakeRestCov:
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
    support_overlay = staticmethod(MavenProject.support_overlay)

    def __init__(self, source: Path, workdir: Path, overlays: tuple[Any, ...] = ()) -> None:
        self.source = source
        self.workdir = workdir
        self.overlays = overlays

    def prepare(self) -> None:
        pass


class FakeRecorder:
    made: list["FakeRecorder"] = []

    def __init__(
        self, suite: Any, runner: Any, service: Any, proxy: Any, recording: Recording, reset: Any = None
    ) -> None:
        self.suite = suite
        self.service = service
        self.recording = recording
        self.reset = reset
        self.meta: dict[str, Any] = {}
        self.tests: list[Any] | None = None
        FakeRecorder.made.append(self)

    def run(self, meta: dict[str, Any], progress: Any = None, tests: list[Any] | None = None) -> None:
        self.meta = meta
        self.tests = tests


def test_record_wires_the_subject_and_suite_into_the_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subjects, "MavenProject", FakeMaven)
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


def test_env_file_skips_comments_and_strips_quotes(tmp_path: Path) -> None:
    path = tmp_path / "drive.env"
    path.write_text('# Drive\nGOOGLE_CLIENT_ID="abc"\n\nGOOGLE_REFRESH_TOKEN = x=y\nbroken line\n')
    assert cli.EnvFile.read(path) == {"GOOGLE_CLIENT_ID": "abc", "GOOGLE_REFRESH_TOKEN": "x=y"}
    assert cli.EnvFile.read(None) == {}


def test_env_file_reads_a_byte_order_mark_and_export_lines(tmp_path: Path) -> None:
    path = tmp_path / "drive.env"
    path.write_bytes(b"\xef\xbb\xbfGOOGLE_CLIENT_ID=abc\r\nexport GOOGLE_CLIENT_SECRET=def\r\n")
    assert cli.EnvFile.read(path) == {"GOOGLE_CLIENT_ID": "abc", "GOOGLE_CLIENT_SECRET": "def"}


def test_record_on_google_drive_stops_without_the_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subjects, "MavenProject", FakeMaven)
    monkeypatch.setattr(cli, "Recorder", FakeRecorder)
    args = ["record", "--subject", "google-drive", "--project", str(tmp_path / "drive"), "--suite", "single-agent"]
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN"):
        cli.main([*args, "--out", str(tmp_path / "rec")])


class TrackedMaven(FakeMaven):
    made: list["TrackedMaven"] = []

    def __init__(self, source: Path, workdir: Path, overlays: tuple[Any, ...] = ()) -> None:
        super().__init__(source, workdir, overlays)
        TrackedMaven.made.append(self)


def _merge_ready_recording(tmp_path: Path) -> Recording:
    recording = _recording(tmp_path / "rec")
    (tmp_path / "spec.json").write_text(json.dumps({"openapi": "3.0.0", "paths": {}}))
    meta = {"subject": "petstore", "suite": "single-agent", "package": "generations.single_agent"}
    recording.write_meta({**meta, "project": str(tmp_path / "petstore"), "spec": str(tmp_path / "spec.json")})
    return recording


def test_merge_without_merges_still_compiles_the_project_and_plans_three_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    monkeypatch.setattr(subjects, "MavenProject", TrackedMaven)
    TrackedMaven.made.clear()
    recording = _merge_ready_recording(tmp_path)
    out = tmp_path / "merge"
    cli.main(["merge", "--recording", str(recording.folder), "--restcov", "restcov.py", "--out", str(out)])

    merges = json.loads((out / "merges.json").read_text())
    assert (merges["accepted"], merges["attempts"]) == ([], [])

    # The description documents no operation, so every one-request test is listed with that reason.
    assert {entry["reason"] for entry in merges["skipped"]} == {"its request matched no documented operation"}
    assert len(merges["skipped"]) == 4
    assert json.loads((out / "meta.json").read_text())["recording"] == str(recording.folder.resolve())
    assert [maven.workdir for maven in TrackedMaven.made] == [out.resolve() / "project"]
    versions = json.loads((out / "plan.json").read_text())["versions"]
    assert [(version["name"], len(version["tests"])) for version in versions] == [
        ("Original", 4),
        ("Select", 4),
        ("Select + Merge", 4),
    ]


class FakeBenchmark:
    made: list["FakeBenchmark"] = []

    def __init__(self, plan: Any, runner: Any, proxy: Any, log: Any, service: Any, measure: Any) -> None:
        self.plan = plan
        self.runner = runner
        self.args: tuple[Any, ...] = ()
        FakeBenchmark.made.append(self)

    def run(self, out: Path, rounds: int, warmup: int = 1, progress: Any = None) -> list[Any]:
        self.args = (out, rounds, warmup)
        out.mkdir(parents=True, exist_ok=True)
        return []

    @staticmethod
    def repeated(out: Path) -> int:
        return 0


class FakeBenchReport:
    def __init__(self, plan: Any, runs: list[Any], repeated: int = 0) -> None:
        pass

    def markdown(self) -> str:
        return "report\n"


@dataclass
class FakeRunner:
    project: Any
    output: Path
    timeout: float = 300.0
    settings: Any = None
    launcher: Any = None
    suite_timeout: float = 1800.0


def test_bench_runs_the_merge_projects_copy_with_the_recordings_base_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "merge"
    folder.mkdir()
    (tmp_path / "spec.json").write_text(json.dumps({"openapi": "3.0.0", "paths": {}}))
    meta = {"subject": "petstore", "spec": str(tmp_path / "spec.json"), "restcov_base_path": "/v2"}
    (folder / "meta.json").write_text(json.dumps({**meta, "project": str(tmp_path / "petstore")}))
    (folder / "plan.json").write_text(json.dumps({"versions": []}))
    base_paths: list[str | None] = []

    def build(script: Path, spec: Path, base_path: str | None = None) -> FakeRestCov:
        base_paths.append(base_path)
        return FakeRestCov()

    monkeypatch.setattr(cli, "RestCov", build)
    monkeypatch.setattr(subjects, "JvmRunner", FakeRunner)
    monkeypatch.setattr(cli, "Benchmark", FakeBenchmark)
    monkeypatch.setattr(cli, "BenchReport", FakeBenchReport)
    FakeBenchmark.made.clear()
    cli.main(["bench", "--merge", str(folder), "--rounds", "3", "--restcov", "restcov.py"])

    bench = FakeBenchmark.made[0]
    assert base_paths == ["/v2"]
    assert bench.runner.project.workdir == folder.resolve() / "project"
    assert bench.runner.output == folder.resolve() / "bench"
    assert bench.args == (folder.resolve() / "bench", 3, 1)
    assert (folder / "bench" / "report.md").read_text() == "report\n"


def test_merge_stops_when_its_matching_differs_from_restcov(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    monkeypatch.setattr(subjects, "MavenProject", TrackedMaven)
    recording = _merge_ready_recording(tmp_path)

    # The tool matches GET /v2/a to an operation, but the fake RESTCov only ever reports path items.
    (tmp_path / "spec.json").write_text(json.dumps({"openapi": "3.0.0", "paths": {"/v2/a": {"get": {}}}}))
    args = ["merge", "--recording", str(recording.folder), "--restcov", "restcov.py", "--out", str(tmp_path / "m")]
    with pytest.raises(ValueError, match="match 1 tests to other operations, such as suite.Api.a"):
        cli.main(args)


def test_merge_refuses_an_out_folder_inside_the_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    monkeypatch.setattr(subjects, "MavenProject", TrackedMaven)
    recording = _merge_ready_recording(tmp_path)
    args = ["merge", "--recording", str(recording.folder), "--restcov", "restcov.py", "--out", str(recording.folder)]
    with pytest.raises(ValueError, match="sits inside the recording"):
        cli.main(args)


def test_bench_needs_at_least_one_round(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["bench", "--merge", str(tmp_path), "--rounds", "0"])
    assert "needs at least 1" in capsys.readouterr().err


class FakeGradle(FakeMaven):
    def __init__(
        self,
        source: Path,
        workdir: Path,
        module: str,
        overlays: tuple[Any, ...] = (),
        env: Any = None,
        helpers: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(source, workdir, overlays)
        self.module = module
        self.helpers = helpers


class FakeJUnitSuite:
    def __init__(self, project: Any, package: str, settings: Any = None) -> None:
        self.project = project
        self.package = package
        self.settings = settings

    def tests(self) -> list[JavaTest]:
        prefix = "org.apache.fineract.integrationtests"
        return [JavaTest(f"{prefix}.GroupTest", "a"), JavaTest(f"{prefix}.LoanTest", "b")]


def test_record_on_fineract_uses_gradle_junit_and_a_per_class_reset_and_only_records_the_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subjects, "GradleProject", FakeGradle)
    monkeypatch.setattr(subjects, "JUnitSuite", FakeJUnitSuite)
    monkeypatch.setattr(cli, "Recorder", FakeRecorder)
    FakeRecorder.made.clear()
    out = tmp_path / "rec"
    args = ["record", "--subject", "fineract", "--project", str(tmp_path / "fineract"), "--suite", "integration-tests"]
    cli.main([*args, "--out", str(out), "--only", r"\.GroupTest\."])

    recorder = FakeRecorder.made[0]
    assert recorder.suite.project.module == ":integration-tests"
    assert [helper.name for helper in recorder.suite.project.helpers] == ["LesstestsJUnit.java"]
    assert recorder.suite.settings.public_env["BACKEND_PORT"] == "8091"

    # The address is no secret, so the console logs keep it.
    assert recorder.suite.settings.hide_secrets("127.0.0.1:8091") == "127.0.0.1:8091"
    assert recorder.service.address.port == 8092
    assert recorder.reset.facts() == {"reset": "every class"}
    assert recorder.tests is not None
    assert [test.method for test in recorder.tests] == ["a"]
    assert recorder.meta["only"] == r"\.GroupTest\."
    assert recorder.meta["database_image"] == "postgres:18.3"

    # The build makes the description, so it sits in the copy and not in the source.
    assert recorder.meta["spec"] == str(out.resolve() / "project" / subjects.FINERACT.spec_file)


def test_merge_refuses_a_suite_it_cannot_rewrite(
    tmp_path: Path, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    recording = _recording(tmp_path / "rec")
    recording.write_meta({"subject": "fineract", "spec": str(tmp_path / "spec.json")})
    args = ["merge", "--recording", str(recording.folder), "--restcov", "restcov.py", "--out", str(tmp_path / "m")]
    with pytest.raises(ValueError, match="not fineract. Use `lesstests plan`"):
        cli.main(args)
    assert not (tmp_path / "m").exists()


def test_plan_refuses_an_out_folder_inside_the_recording(
    tmp_path: Path, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    recording = _merge_ready_recording(tmp_path)
    before = (recording.folder / "meta.json").read_text()
    args = ["plan", "--recording", str(recording.folder), "--restcov", "restcov.py", "--out", str(recording.folder)]
    with pytest.raises(ValueError, match="sits inside the recording"):
        cli.main(args)
    assert (recording.folder / "meta.json").read_text() == before


def test_plan_writes_original_and_select_and_points_bench_at_the_recordings_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restcov: tuple[FakeRestCov, list[tuple[Path, Path]]]
) -> None:
    recording = _merge_ready_recording(tmp_path)
    out = tmp_path / "plan"
    cli.main(["plan", "--recording", str(recording.folder), "--restcov", "restcov.py", "--out", str(out)])

    versions = json.loads((out / "plan.json").read_text())["versions"]
    assert [version["name"] for version in versions] == ["Original", "Select"]
    assert "4 of 4 tests sent exactly one request." in (out / "plan.md").read_text()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["copy"] == str(recording.folder.resolve() / "project")

    monkeypatch.setattr(subjects, "JvmRunner", FakeRunner)
    monkeypatch.setattr(cli, "Benchmark", FakeBenchmark)
    monkeypatch.setattr(cli, "BenchReport", FakeBenchReport)
    FakeBenchmark.made.clear()
    cli.main(["bench", "--plan", str(out), "--rounds", "3", "--restcov", "restcov.py"])
    assert FakeBenchmark.made[0].runner.project.workdir == recording.folder.resolve() / "project"
