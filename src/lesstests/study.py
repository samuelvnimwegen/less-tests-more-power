import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lesstests.coverage import CoverageSet, Criterion, RestCov, SuiteCoverage
from lesstests.java import JavaTest, Outcome
from lesstests.recording import Recording
from lesstests.selection import Budget, CoverageMatrix, CoverageSelector, RandomSelector, Selector


@dataclass(frozen=True, slots=True)
class SmallerSuite:
    """One smaller version of a suite and what it lost against the full suite."""

    version: str
    budget: Budget
    draw: int
    tests: list[JavaTest]
    coverage: CoverageSet
    requests: int

    def lost(self, full: CoverageSet) -> dict[str, int]:
        """Count the items the full suite reaches and this version misses, per criterion and in total.

        Returns:
            The lost items per criterion, plus `all`.
        """
        missing = full - self.coverage
        return {**missing.counts(), "all": len(missing)}

    def to_dict(self, full: CoverageSet) -> dict[str, Any]:
        """Turn the version into JSON friendly data.

        Returns:
            The version as a dict.
        """
        return {
            "version": self.version,
            "budget": self.budget.share,
            "draw": self.draw,
            "kept": len(self.tests),
            "requests": self.requests,
            "covered": {**self.coverage.counts(), "all": len(self.coverage)},
            "lost": self.lost(full),
            "tests": [test.id for test in self.tests],
        }


class SelectionStudy:
    """The RQ2 comparison without new runs. Random and Selected only drop tests, so the recording is enough.

    Every smaller suite is measured twice: by joining the items of its tests, and by RESTCov on its joined logs.
    The study stops if the two ever differ.
    """

    def __init__(
        self,
        recording: Recording,
        coverage: SuiteCoverage,
        restcov: RestCov,
        budgets: list[Budget],
        draws: int,
        seed: int,
        passing_only: bool = False,
    ) -> None:
        """Set up the study. With `passing_only`, both selectors pick only from tests that passed alone."""
        self._recording = recording
        self._coverage = coverage
        self._restcov = restcov
        self._budgets = budgets
        self._draws = draws
        self._seed = seed
        self._passing_only = passing_only

    def run(self, out: Path) -> list[SmallerSuite]:
        """Build every smaller suite, measure it and write the results to `out`.

        Returns:
            The smaller suites.
        """
        records = self._recording.records()
        requests = {record.test: record.requests for record in records}
        pool = [record.test for record in records if record.outcome is Outcome.PASSED or not self._passing_only]
        matrix = CoverageMatrix(self._coverage, requests, pool)
        suites: list[SmallerSuite] = []
        for budget in self._budgets:
            size = budget.size(len(records))
            suites.append(self._build(CoverageSelector(), matrix, budget, 0, size, requests, out))
            for draw in range(1, self._draws + 1):
                selector = RandomSelector(RandomSelector.seed_for(self._seed, budget, draw))
                suites.append(self._build(selector, matrix, budget, draw, size, requests, out))
        self._write(out, len(records), sum(requests.values()), len(pool), suites)
        return suites

    def _build(
        self,
        selector: Selector,
        matrix: CoverageMatrix,
        budget: Budget,
        draw: int,
        size: int,
        requests: dict[JavaTest, int],
        out: Path,
    ) -> SmallerSuite:
        tests = selector.select(matrix, size)
        joined = self._coverage.of(tests)
        name = f"{selector.name}-{budget.label.rstrip('%')}-{draw}"
        logs = self._recording.gather_logs(tests, out / "logs" / name)
        measured = self._restcov.measure(logs, out / "restcov" / name).items()
        if measured != joined:
            raise ValueError(f"RESTCov and the joined items differ for {name}")
        return SmallerSuite(selector.name, budget, draw, tests, joined, sum(requests[test] for test in tests))

    def _write(self, out: Path, records_count: int, requests: int, pool: int, suites: list[SmallerSuite]) -> None:
        full = self._coverage.full
        result = {
            "tests": records_count,
            "requests": requests,
            "pool": pool,
            "passing_only": self._passing_only,
            "seed": self._seed,
            "draws": self._draws,
            "totals": self._coverage.totals,
            "full": {**full.counts(), "all": len(full)},
            "versions": [suite.to_dict(full) for suite in suites],
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "selection.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        (out / "report.md").write_text(StudyReport(result).markdown(), encoding="utf-8")


class StudyReport:
    """Turns `selection.json` into a short Markdown table of coverage lost per budget."""

    _LABELS = {
        Criterion.PATH: "Paths",
        Criterion.OPERATION: "Operations",
        Criterion.PARAMETER: "Parameters",
        Criterion.REQUEST_TYPE: "Request types",
        Criterion.RESPONSE_TYPE: "Response types",
        Criterion.STATUS: "Status codes",
        Criterion.STATUS_CLASS: "Status classes",
    }

    def __init__(self, result: dict[str, Any]) -> None:
        """Keep the parsed results."""
        self._result = result

    def markdown(self) -> str:
        """Write the report.

        Returns:
            The report as Markdown.
        """
        columns = [*self._LABELS.values(), "All", "Requests"]
        keys = [*(criterion.value for criterion in self._LABELS), "all"]
        full = [str(self._result["full"][key]) for key in keys] + [str(self._result["requests"])]
        lines = [
            "# Coverage lost per budget",
            "",
            f"{self._result['tests']} tests, of which {self._result['pool']} could be picked. "
            "The full suite row counts the items the suite covers. Every other row counts the items that version "
            "loses against the full suite. Requests counts what the kept tests send. "
            f"Random shows the mean over {self._result['draws']} draws, with the lowest and highest draw.",
            "",
            "| Budget | Kept | Version | " + " | ".join(columns) + " |",
            "|---|---:|---|" + "---:|" * len(columns),
            f"| 100% | {self._result['tests']} | Full suite | " + " | ".join(full) + " |",
        ]
        for budget in sorted({version["budget"] for version in self._result["versions"]}, reverse=True):
            versions = [version for version in self._result["versions"] if version["budget"] == budget]
            selected = next(version for version in versions if version["version"] == "selected")
            drawn = [version for version in versions if version["version"] == "random"]
            label = f"{round(budget * 100)}%"
            lost = [str(selected["lost"][key]) for key in keys] + [str(selected["requests"])]
            lines.append(f"| {label} | {selected['kept']} | Selected | " + " | ".join(lost) + " |")
            if drawn:
                spread = [self._spread([version["lost"][key] for version in drawn]) for key in keys]
                spread.append(self._spread([version["requests"] for version in drawn]))
                lines.append(f"| {label} | {selected['kept']} | Random | " + " | ".join(spread) + " |")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _spread(values: list[int]) -> str:
        return f"{statistics.mean(values):.1f} ({min(values)}, {max(values)})"
