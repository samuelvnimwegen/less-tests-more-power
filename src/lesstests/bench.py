"""The benchmark: every version of a suite runs as one whole suite, in rounds that take turns between versions."""

import json
import statistics
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self, cast

from scipy.stats import wilcoxon

from lesstests.api import ApiDescription, ValueItem
from lesstests.coverage import CoverageSet, RestCov
from lesstests.exchange import Exchange
from lesstests.java import JavaTest, SuiteRun
from lesstests.recording import RestCovLogFolder
from lesstests.service import Service
from lesstests.versions import SuiteVersion, VersionPlan


class ExchangeSource(Protocol):
    """What the benchmark needs from a proxy: hand over the exchanges seen since the last call."""

    def take(self) -> tuple[list[Exchange], list[str]]:
        """Hand over the exchanges and errors.

        Returns:
            The exchanges in arrival order and the error messages.
        """
        ...


class SuiteRunner(Protocol):
    """Runs a list of tests in one JVM."""

    def run_suite(self, tests: list[JavaTest], name: str) -> SuiteRun:
        """Run the tests together.

        Returns:
            The outcome of each test and the time the JVM took.
        """
        ...


@dataclass(slots=True)
class BenchRun:
    """One whole suite run of one version."""

    version: str
    round: int
    position: int
    seconds: float
    test_seconds: float
    outcomes: dict[str, int]
    requests: int
    errors: int
    coverage: CoverageSet
    values: frozenset[ValueItem]

    @property
    def started(self) -> bool:
        """Whether the suite really ran. A JVM that dies before the first test leaves only errors and no request."""
        return self.requests > 0 or set(self.outcomes) != {"error"}

    def to_dict(self) -> dict[str, Any]:
        """Turn the run into JSON friendly data.

        Returns:
            The run as a dict.
        """
        return {
            "version": self.version,
            "round": self.round,
            "position": self.position,
            "seconds": round(self.seconds, 3),
            "test_seconds": round(self.test_seconds, 3),
            "outcomes": self.outcomes,
            "requests": self.requests,
            "errors": self.errors,
            "coverage": self.coverage.to_list(),
            "values": [value.to_list() for value in sorted(self.values)],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read a run saved by `to_dict`.

        Returns:
            The run.
        """
        return cls(
            raw["version"],
            raw["round"],
            raw["position"],
            raw["seconds"],
            raw["test_seconds"],
            dict(raw["outcomes"]),
            raw["requests"],
            raw["errors"],
            CoverageSet.from_list(raw["coverage"]),
            frozenset(ValueItem.from_list(value) for value in raw["values"]),
        )


class Benchmark:
    """Runs every version once per round. The order rotates each round, so no version always runs first.

    A run whose suite never started gets repeated, at most `ATTEMPTS` times. Its record moves to `runs/failed`.
    """

    ATTEMPTS = 3

    def __init__(
        self,
        plan: VersionPlan,
        runner: SuiteRunner,
        proxy: AbstractContextManager[Any],
        log: ExchangeSource,
        service: Service,
        measure: tuple[RestCov, ApiDescription],
    ) -> None:
        """Wire the parts. The proxy is entered once for the whole benchmark."""
        self._plan = plan
        self._runner = runner
        self._proxy = proxy
        self._log = log
        self._service = service
        self._restcov, self._api = measure

    def run(
        self, out: Path, rounds: int, warmup: int = 1, progress: Callable[[BenchRun], None] | None = None
    ) -> list[BenchRun]:
        """Run the rounds and write every run to `out/runs`. Runs already there are kept, so a stopped run resumes.

        Returns:
            Every counted run.
        """
        runs: list[BenchRun] = []
        with self._proxy:
            try:
                self._service.start()

                # Warm up runs count for nothing. They take the first JVM and connection costs off the first round.
                for number in range(1, warmup + 1):
                    self._once(self._plan.original, -number, 0, out)
                for number in range(1, rounds + 1):
                    for position, version in enumerate(self._order(number), start=1):
                        saved = out / "runs" / f"round-{number}-{self._slug(version.name)}.json"
                        if saved.exists():
                            run = BenchRun.from_dict(json.loads(saved.read_text(encoding="utf-8")))
                        else:
                            run = self._started(version, number, position, out)
                            saved.parent.mkdir(parents=True, exist_ok=True)
                            saved.write_text(json.dumps(run.to_dict(), indent=1), encoding="utf-8")
                        runs.append(run)
                        if progress is not None:
                            progress(run)
            finally:
                self._service.stop()
        return runs

    @staticmethod
    def repeated(out: Path) -> int:
        """Count the runs that had to be repeated because their suite never started.

        Returns:
            The number of failed attempts on disk.
        """
        return len(list((out / "runs" / "failed").glob("*.json")))

    def _started(self, version: SuiteVersion, number: int, position: int, out: Path) -> BenchRun:
        """Run a version until its suite really starts, keeping every failed attempt.

        Returns:
            The first run that started, or the last attempt when none did.
        """
        run = self._once(version, number, position, out)
        for attempt in range(1, self.ATTEMPTS):
            if run.started:
                break
            name = f"round-{number}-{self._slug(version.name)}"
            failed = out / "runs" / "failed" / f"{name}-attempt-{attempt}.json"
            failed.parent.mkdir(parents=True, exist_ok=True)
            failed.write_text(json.dumps(run.to_dict(), indent=1), encoding="utf-8")
            reports = out / "testng" / name
            if reports.exists():
                reports.rename(out / "testng" / f"{name}-failed-{attempt}")
            run = self._once(version, number, position, out)
        return run

    def _order(self, number: int) -> list[SuiteVersion]:
        versions = self._plan.versions
        shift = (number - 1) % len(versions)
        return versions[shift:] + versions[:shift]

    def _once(self, version: SuiteVersion, number: int, position: int, out: Path) -> BenchRun:
        self._service.reset()

        # A late answer from the previous run must not end up in this one.
        self._log.take()
        name = f"warmup-{-number}" if number < 0 else f"round-{number}-{self._slug(version.name)}"
        result = self._runner.run_suite(version.tests, f"testng/{name}")
        exchanges, errors = self._log.take()
        logs = RestCovLogFolder(out / "logs" / name).write(exchanges)
        coverage = self._restcov.measure(logs, out / "restcov" / name).items()
        return BenchRun(
            version.name,
            number,
            position,
            result.seconds,
            result.test_seconds,
            dict(Counter(outcome.value for outcome in result.outcomes.values())),
            len(exchanges),
            len(errors),
            coverage,
            self._api.values(exchanges),
        )

    @staticmethod
    def _slug(name: str) -> str:
        return name.lower().replace(" + ", "-").replace(" ", "-")


class BenchReport:
    """Turns the runs into the result table: tests, requests, runtime and the coverage each version kept.

    Kept coverage compares a run with the Original run of the same round, so a change of the live API between
    rounds hits both sides.
    """

    def __init__(self, plan: VersionPlan, runs: list[BenchRun], repeated: int = 0) -> None:
        """Keep the plan, the counted runs and how many runs had to be repeated.

        Raises:
            ValueError: If a counted run never started, since kept coverage would compare against nothing.
        """
        broken = [f"round {run.round} {run.version}" for run in runs if not run.started]
        if broken:
            raise ValueError(f"These runs never started: {', '.join(broken)}")
        self._plan = plan
        self._runs = runs
        self._repeated = repeated

    def markdown(self) -> str:
        """Write the report.

        Returns:
            The report as Markdown.
        """
        rounds = sorted({run.round for run in self._runs})
        lines = [
            "# Benchmark",
            "",
            f"{len(rounds)} rounds. Every version ran once per round as one whole suite in one JVM, in an order that "
            "rotates each round. Runtime is the wall clock of that JVM. Kept coverage counts the items of the "
            "Original run of the same round that this run also reached, as a median over the rounds. "
            f"{self._repeated} runs were repeated because their suite never started.",
            "",
            "| Version | Tests | Requests | Runtime median (min to max) | Runtime vs Original | Passed | "
            "RESTCov items kept | Value items kept |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for version in self._plan.versions:
            lines.append(self._row(version, rounds))
        lines += ["", *self._significance(rounds)]
        return "\n".join(lines) + "\n"

    def _row(self, version: SuiteVersion, rounds: list[int]) -> str:
        runs = self._of(version.name)
        seconds = [run.seconds for run in runs]
        requests = statistics.median(run.requests for run in runs)
        passed = statistics.median(run.outcomes.get("passed", 0) for run in runs)
        kept, kept_values = [], []
        change = []
        for number in rounds:
            run, original = self._at(version.name, number), self._at(VersionPlan.ORIGINAL, number)
            if run is None or original is None:
                continue
            kept.append(self._share(run.coverage.items, original.coverage.items))
            kept_values.append(self._share(run.values, original.values))
            change.append((run.seconds - original.seconds) / original.seconds)
        runtime = f"{statistics.median(seconds):.1f} s ({min(seconds):.1f} to {max(seconds):.1f})"
        versus = "" if version.name == VersionPlan.ORIGINAL else f"{statistics.median(change):+.1%}"
        original_items = self._at(VersionPlan.ORIGINAL, rounds[0])
        total = len(original_items.coverage.items) if original_items else 0
        total_values = len(original_items.values) if original_items else 0
        return (
            f"| {version.name} | {len(version.tests)} | {requests:g} | {runtime} | {versus} | {passed:g} | "
            f"{statistics.median(kept):.1%} of {total} | {statistics.median(kept_values):.1%} of {total_values} |"
        )

    def _significance(self, rounds: list[int]) -> list[str]:
        """Test with the paired Wilcoxon signed rank test whether a version is faster than another.

        Every smaller version faces the Original, and Select + Merge also faces Select, since that pair shows what
        merging adds on top of dropping tests.

        Returns:
            One line per pair.
        """
        pairs = [(version.name, VersionPlan.ORIGINAL) for version in self._plan.versions[1:]]
        pairs.append((VersionPlan.SELECT_MERGE, VersionPlan.SELECT))
        return [self._compare(faster, slower, rounds) for faster, slower in pairs]

    def _compare(self, version: str, other: str, rounds: list[int]) -> str:
        """Compare the runtimes of two versions round by round.

        Returns:
            One line with the rounds won, the median change and the p value.
        """
        pairs = [
            (run.seconds, base.seconds)
            for number in rounds
            if (run := self._at(version, number)) and (base := self._at(other, number))
        ]
        if len(pairs) < 5:
            return f"{version} against {other}: too few rounds for a test."
        pvalue = cast(float, wilcoxon([a - b for a, b in pairs], alternative="less")[1])
        change = statistics.median((a - b) / b for a, b in pairs)
        return (
            f"{version} against {other}: faster in {sum(a < b for a, b in pairs)} of {len(pairs)} rounds, "
            f"median change {change:+.1%}, Wilcoxon signed rank p = {float(pvalue):.4f}."
        )

    def _of(self, version: str) -> list[BenchRun]:
        return [run for run in self._runs if run.version == version]

    def _at(self, version: str, number: int) -> BenchRun | None:
        return next((run for run in self._runs if run.version == version and run.round == number), None)

    @staticmethod
    def _share(kept: frozenset[Any], full: frozenset[Any]) -> float:
        return len(kept & full) / len(full) if full else 1.0
