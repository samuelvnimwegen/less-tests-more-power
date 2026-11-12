import contextlib
import json
from pathlib import Path

import pytest
from fake_restcov import FakeRestCov

from lesstests.api import ApiDescription
from lesstests.bench import Benchmark, BenchReport
from lesstests.coverage import CoverageSet
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, Outcome, SuiteRun
from lesstests.proxy import Address
from lesstests.versions import SuiteVersion, VersionPlan

SPEC = {"openapi": "3.0.0", "paths": {"/files": {"get": {"parameters": [{"in": "query", "name": "corpora"}]}}}}


class FakeLog:
    def __init__(self) -> None:
        self.pending: list[Exchange] = []

    def take(self) -> tuple[list[Exchange], list[str]]:
        taken, self.pending = self.pending, []
        return taken, []


class FakeRunner:
    """Every test sends one request, and a run takes one second per test."""

    def __init__(self, log: FakeLog) -> None:
        self.log = log
        self.order: list[int] = []

    def run_suite(self, tests: list[JavaTest], name: str) -> SuiteRun:
        self.order.append(len(tests))
        for test in tests:
            request = HttpRequest("GET", f"/files?corpora={test.method}", Headers(), Body())
            self.log.pending.append(Exchange(request, HttpResponse(200, "OK", Headers(), Body())))
        return SuiteRun(dict.fromkeys(tests, Outcome.PASSED), float(len(tests)))


class FakeService:
    def __init__(self) -> None:
        self.resets = 0

    @property
    def address(self) -> Address:
        return Address("127.0.0.1", 1)

    def start(self) -> None:
        pass

    def reset(self) -> None:
        self.resets += 1

    def stop(self) -> None:
        pass


def _plan() -> VersionPlan:
    def version(name: str, methods: str) -> SuiteVersion:
        return SuiteVersion(name, [JavaTest("g.Files", m) for m in methods], CoverageSet(), frozenset(), len(methods))

    return VersionPlan([version("Original", "abcd"), version("Select", "ab"), version("Select + Merge", "a")])


def _bench(tmp_path: Path) -> tuple[Benchmark, FakeRunner, FakeService]:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC))
    log = FakeLog()
    runner = FakeRunner(log)
    service = FakeService()
    measure = (FakeRestCov(), ApiDescription.load(spec))
    return Benchmark(_plan(), runner, contextlib.nullcontext(), log, service, measure), runner, service


def test_rounds_rotate_the_order_and_warm_up_runs_count_for_nothing(tmp_path: Path) -> None:
    benchmark, runner, service = _bench(tmp_path)
    runs = benchmark.run(tmp_path / "bench", rounds=3, warmup=1)
    assert runner.order == [4, 4, 2, 1, 2, 1, 4, 1, 4, 2]
    assert len(runs) == 9
    assert service.resets == 10
    assert {run.round for run in runs} == {1, 2, 3}


def test_a_stopped_benchmark_resumes_from_the_saved_runs(tmp_path: Path) -> None:
    benchmark, runner, _ = _bench(tmp_path)
    benchmark.run(tmp_path / "bench", rounds=2, warmup=0)
    runner.order.clear()
    benchmark.run(tmp_path / "bench", rounds=3, warmup=0)
    assert runner.order == [1, 4, 2]


def test_the_report_compares_every_round_with_its_own_original(tmp_path: Path) -> None:
    benchmark, _, _ = _bench(tmp_path)
    runs = benchmark.run(tmp_path / "bench", rounds=6, warmup=0)
    report = BenchReport(_plan(), runs).markdown()
    assert "| Original | 4 | 4 | 4.0 s (4.0 to 4.0) |  | 4 | 100.0% of 4 | 100.0% of 4 |" in report
    assert "| Select | 2 | 2 | 2.0 s (2.0 to 2.0) | -50.0% | 2 | 50.0% of 4 | 50.0% of 4 |" in report
    assert "Select + Merge against Original: faster in 6 of 6 rounds, median change -75.0%" in report
    assert "Select + Merge against Select: faster in 6 of 6 rounds, median change -50.0%" in report


class FlakyRunner(FakeRunner):
    """The first run of the full suite dies before any test, like a JVM that loses its token request."""

    def __init__(self, log: FakeLog) -> None:
        super().__init__(log)
        self.failed = False

    def run_suite(self, tests: list[JavaTest], name: str) -> SuiteRun:
        if len(tests) == 4 and not self.failed:
            self.failed = True
            self.order.append(0)
            return SuiteRun(dict.fromkeys(tests, Outcome.ERROR), 40.0)
        return super().run_suite(tests, name)


def test_a_run_whose_suite_never_started_is_repeated_and_counted(tmp_path: Path) -> None:
    benchmark, _, _ = _bench(tmp_path)
    flaky = FlakyRunner(FakeLog())
    benchmark._runner = flaky
    benchmark._log = flaky.log
    runs = benchmark.run(tmp_path / "bench", rounds=1, warmup=0)
    assert flaky.order == [0, 4, 2, 1]
    assert all(run.started for run in runs)
    assert Benchmark.repeated(tmp_path / "bench") == 1
    assert "1 runs were repeated" in BenchReport(_plan(), runs, 1).markdown()


def test_the_report_refuses_a_run_that_never_started(tmp_path: Path) -> None:
    benchmark, _, _ = _bench(tmp_path)
    runs = benchmark.run(tmp_path / "bench", rounds=1, warmup=0)
    runs[0].requests = 0
    runs[0].outcomes = {"error": 4}
    with pytest.raises(ValueError, match="never started: round 1 Original"):
        BenchReport(_plan(), runs)


def test_a_folder_with_runs_of_another_plan_is_refused(tmp_path: Path) -> None:
    benchmark, runner, _ = _bench(tmp_path)
    benchmark.run(tmp_path / "bench", rounds=1, warmup=0)
    other = VersionPlan([SuiteVersion("Original", [JavaTest("g.Files", "z")], CoverageSet(), frozenset(), 1)])
    log = FakeLog()
    measure = (FakeRestCov(), ApiDescription.load(tmp_path / "spec.json"))
    stale = Benchmark(other, FakeRunner(log), contextlib.nullcontext(), log, FakeService(), measure)
    with pytest.raises(ValueError, match="holds runs of another plan"):
        stale.run(tmp_path / "bench", rounds=1, warmup=0)


def test_a_plan_without_select_merge_only_compares_select_with_the_original(tmp_path: Path) -> None:
    benchmark, _, _ = _bench(tmp_path)
    two = VersionPlan(_plan().versions[:2])
    benchmark._plan = two
    runs = benchmark.run(tmp_path / "bench", rounds=5, warmup=0)
    report = BenchReport(two, runs).markdown()
    assert "Select against Original: faster in 5 of 5 rounds" in report
    assert "Select + Merge" not in report


class TimeoutRunner(FakeRunner):
    """The full suite sends its requests but hits the suite timeout the first time."""

    def __init__(self, log: FakeLog) -> None:
        super().__init__(log)
        self.timed_out = False

    def run_suite(self, tests: list[JavaTest], name: str) -> SuiteRun:
        run = super().run_suite(tests, name)
        if len(tests) == 4 and not self.timed_out:
            self.timed_out = True
            return SuiteRun(dict.fromkeys(tests, Outcome.ERROR), 7200.0, finished=False)
        return run


def test_a_run_that_hit_the_suite_timeout_is_repeated_instead_of_counted(tmp_path: Path) -> None:
    benchmark, _, _ = _bench(tmp_path)
    slow = TimeoutRunner(FakeLog())
    benchmark._runner = slow
    benchmark._log = slow.log
    runs = benchmark.run(tmp_path / "bench", rounds=1, warmup=0)

    # It sent 4 requests, which used to make it look like a run that started.
    assert all(run.started and run.finished for run in runs)
    assert Benchmark.repeated(tmp_path / "bench") == 1
    failed = json.loads(next((tmp_path / "bench" / "runs" / "failed").glob("*.json")).read_text())
    assert (failed["requests"], failed["finished"]) == (4, False)
