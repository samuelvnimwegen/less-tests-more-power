"""The versions of a suite the benchmark compares: the original, Select only, and Select plus Merge."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from lesstests.api import ApiDescription, ValueItem
from lesstests.coverage import CoverageSet, SuiteCoverage
from lesstests.java import JavaTest
from lesstests.recording import RecordedTest
from lesstests.selection import SmallestCover


@dataclass(slots=True)
class SuiteVersion:
    """One version of the suite with the coverage, values and requests its recorded tests predict."""

    name: str
    tests: list[JavaTest]
    coverage: CoverageSet
    values: frozenset[ValueItem]
    requests: int

    def to_dict(self) -> dict[str, Any]:
        """Turn the version into JSON friendly data.

        Returns:
            The version as a dict.
        """
        return {
            "name": self.name,
            "tests": [test.id for test in self.tests],
            "coverage": self.coverage.to_list(),
            "values": [value.to_list() for value in sorted(self.values)],
            "requests": self.requests,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read a version saved by `to_dict`.

        Returns:
            The version.
        """
        return cls(
            raw["name"],
            [JavaTest.from_id(test) for test in raw["tests"]],
            CoverageSet.from_list(raw["coverage"]),
            frozenset(ValueItem.from_list(value) for value in raw["values"]),
            raw["requests"],
        )


class VersionPlan:
    """The versions to benchmark, saved as `plan.json` next to a short report of what the recordings predict."""

    ORIGINAL = "Original"
    SELECT = "Select"
    SELECT_MERGE = "Select + Merge"

    def __init__(self, versions: list[SuiteVersion]) -> None:
        """Keep the versions, the original first."""
        self.versions = versions

    @property
    def original(self) -> SuiteVersion:
        """The full suite."""
        return self.versions[0]

    def save(self, path: Path) -> None:
        """Write the plan as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = {"versions": [version.to_dict() for version in self.versions]}
        path.write_text(json.dumps(raw, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read a plan written by `save`.

        Returns:
            The plan.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([SuiteVersion.from_dict(version) for version in raw["versions"]])

    def markdown(self) -> str:
        """Write what the recordings predict for each version, before any benchmark run.

        Returns:
            The report as Markdown.
        """
        full, values = self.original.coverage, self.original.values
        lines = [
            "# Predicted from the recordings",
            "",
            "Each test ran alone. The benchmark measures every version again as a whole suite.",
            "",
            "| Version | Tests | Requests | RESTCov items kept | Value items kept |",
            "|---|---:|---:|---:|---:|",
        ]
        for version in self.versions:
            kept = len(version.coverage.items & full.items)
            kept_values = len(version.values & values)
            lines.append(
                f"| {version.name} | {len(version.tests)} | {version.requests} | {kept} of {len(full)} | "
                f"{kept_values} of {len(values)} |"
            )
        return "\n".join(lines) + "\n"


class VersionPlanner:
    """Builds the versions with the smallest cover program.

    Select keeps the fewest original tests that reach every item of the full suite. Select plus Merge may also pick
    merged copies, which need fewer tests when one copy reaches the names of several originals. Without the Merge
    step there is no Select + Merge, since it would only run Select twice.
    """

    def __init__(
        self,
        api: ApiDescription,
        original: tuple[SuiteCoverage, list[RecordedTest]],
        merged: tuple[SuiteCoverage, list[RecordedTest]] | None = None,
        merge_step: bool = True,
    ) -> None:
        """Keep the coverage and records of the original suite and of the accepted merged copies."""
        self._api = api
        self._original_coverage, self._original_records = original
        self._merged = merged
        self._merge_step = merge_step

    def plan(self) -> VersionPlan:
        """Pick the tests of every version.

        Returns:
            The plan with Original, Select and, after the Merge step, Select + Merge.
        """
        coverage = dict(self._original_coverage.per_test)
        records = {record.test: record for record in self._original_records}
        target = self._original_coverage.full
        original = sorted(records)
        select = SmallestCover().select(coverage, self._requests(records), target)
        versions = [
            self._version(VersionPlan.ORIGINAL, original, coverage, records),
            self._version(VersionPlan.SELECT, select, coverage, records),
        ]
        if not self._merge_step:
            return VersionPlan(versions)
        if self._merged is not None:
            merged_coverage, merged_records = self._merged
            coverage |= merged_coverage.per_test
            records |= {record.test: record for record in merged_records}
        select_merge = SmallestCover().select(coverage, self._requests(records), target)
        return VersionPlan([*versions, self._version(VersionPlan.SELECT_MERGE, select_merge, coverage, records)])

    @staticmethod
    def _requests(records: dict[JavaTest, RecordedTest]) -> dict[JavaTest, int]:
        return {test: record.requests for test, record in records.items()}

    def _version(
        self,
        name: str,
        tests: list[JavaTest],
        coverage: dict[JavaTest, CoverageSet],
        records: dict[JavaTest, RecordedTest],
    ) -> SuiteVersion:
        exchanges = [exchange for test in tests for exchange in records[test].exchanges]
        return SuiteVersion(
            name,
            tests,
            CoverageSet.union(coverage[test] for test in tests),
            self._api.values(exchanges),
            sum(records[test].requests for test in tests),
        )
