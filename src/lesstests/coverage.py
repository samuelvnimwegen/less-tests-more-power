import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from lesstests.java import JavaTest
from lesstests.recording import Recording


class Criterion(StrEnum):
    """The seven criteria RESTCov counts. Each value is the prefix of its keys in RESTCov's metrics."""

    PATH = "path"
    OPERATION = "operation"
    PARAMETER = "parameter"
    REQUEST_TYPE = "request_type"
    RESPONSE_TYPE = "response_type"
    STATUS = "status"
    STATUS_CLASS = "status_class"

    @property
    def seen_metric(self) -> str:
        """The key of RESTCov's metric that counts the covered items of this criterion."""
        return _METRIC_KEYS[self][0]

    @property
    def total_metric(self) -> str:
        """The key of RESTCov's metric that counts the documented items of this criterion."""
        return _METRIC_KEYS[self][1]


_METRIC_KEYS = {
    Criterion.PATH: ("covered_paths", "total_paths"),
    Criterion.OPERATION: ("covered_ops", "total_ops"),
    Criterion.PARAMETER: ("param_seen", "param_total"),
    Criterion.REQUEST_TYPE: ("req_ct_seen", "req_ct_total"),
    Criterion.RESPONSE_TYPE: ("resp_ct_seen", "resp_ct_total"),
    Criterion.STATUS: ("status_seen", "status_total"),
    Criterion.STATUS_CLASS: ("status_class_seen", "status_class_total"),
}


@dataclass(frozen=True, slots=True, order=True)
class CoverageItem:
    """One documented part of the API that a request reached, such as status 404 of `GET /pet/{petId}`."""

    criterion: Criterion
    key: str


class CoverageSet:
    """A set of coverage items, counted per criterion."""

    def __init__(self, items: Iterable[CoverageItem] = ()) -> None:
        """Keep the items."""
        self.items = frozenset(items)

    def __or__(self, other: "CoverageSet") -> "CoverageSet":
        """Join two sets.

        Returns:
            The items in either set.
        """
        return CoverageSet(self.items | other.items)

    def __sub__(self, other: "CoverageSet") -> "CoverageSet":
        """Remove the items of another set.

        Returns:
            The items in this set only.
        """
        return CoverageSet(self.items - other.items)

    def __eq__(self, other: object) -> bool:
        """Compare the items.

        Returns:
            True when both sets hold the same items.
        """
        return isinstance(other, CoverageSet) and self.items == other.items

    def __hash__(self) -> int:
        """Hash the items.

        Returns:
            The hash.
        """
        return hash(self.items)

    def __len__(self) -> int:
        """Count all items.

        Returns:
            The number of items.
        """
        return len(self.items)

    def count(self, criterion: Criterion) -> int:
        """Count the items of one criterion.

        Returns:
            The number of items.
        """
        return sum(1 for item in self.items if item.criterion is criterion)

    def counts(self) -> dict[str, int]:
        """Count the items of every criterion.

        Returns:
            The count per criterion, in RESTCov's order.
        """
        return {criterion.value: self.count(criterion) for criterion in Criterion}

    @classmethod
    def union(cls, sets: Iterable["CoverageSet"]) -> Self:
        """Join many sets.

        Returns:
            The items in any of them.
        """
        return cls(item for coverage in sets for item in coverage.items)

    def to_list(self) -> list[list[str]]:
        """Turn the items into JSON friendly pairs.

        Returns:
            One `[criterion, key]` list per item, sorted.
        """
        return [[item.criterion.value, item.key] for item in sorted(self.items)]

    @classmethod
    def from_list(cls, pairs: list[list[str]]) -> Self:
        """Read items saved by `to_list`.

        Returns:
            The set.
        """
        return cls(CoverageItem(Criterion(criterion), key) for criterion, key in pairs)


class RestCovReport:
    """The `coverage.json` RESTCov writes, read back as coverage items.

    The items follow RESTCov's own counting: only documented items count, and content types with a `/*`
    wildcard never count. `check` makes sure the item counts equal RESTCov's own metrics.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        """Keep the parsed JSON."""
        self._raw = raw

    @classmethod
    def read(cls, path: Path) -> Self:
        """Read a `coverage.json` file.

        Returns:
            The report.
        """
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @property
    def metrics(self) -> dict[str, Any]:
        """RESTCov's own metrics."""
        return self._raw["metrics"]

    @property
    def matched_requests(self) -> int:
        """The requests RESTCov matched to a documented operation."""
        return int(self.metrics["total_requests"])

    @property
    def unmatched(self) -> list[dict[str, str]]:
        """Up to 30 requests RESTCov could not match to the description."""
        return self._raw.get("unmatched_samples", [])

    def totals(self) -> dict[str, int]:
        """Count the documented items of every criterion.

        Returns:
            The total per criterion.
        """
        return {criterion.value: int(self.metrics[criterion.total_metric]) for criterion in Criterion}

    def items(self) -> CoverageSet:
        """Rebuild the covered items from the per operation lists.

        Returns:
            The covered items.
        """
        found = [CoverageItem(Criterion.PATH, path) for path in self._raw["seen_paths"]]
        found += [CoverageItem(Criterion.OPERATION, f"{op['method']} {op['path']}") for op in self._raw["seen_ops"]]
        for operation in self._raw["operations"]:
            found += self._operation_items(operation)
        return CoverageSet(found)

    def check(self) -> None:
        """Make sure the rebuilt items give the same counts as RESTCov's metrics.

        Raises:
            ValueError: If any criterion differs.
        """
        counts = self.items().counts()
        wrong = {
            criterion.value: (counts[criterion.value], self.metrics[criterion.seen_metric])
            for criterion in Criterion
            if counts[criterion.value] != self.metrics[criterion.seen_metric]
        }
        if wrong:
            raise ValueError(f"Rebuilt items differ from RESTCov's metrics (ours, RESTCov's): {wrong}")

    @staticmethod
    def _operation_items(operation: dict[str, Any]) -> list[CoverageItem]:
        name = f"{operation['method']} {operation['path']}"
        found = [
            CoverageItem(Criterion.PARAMETER, f"{name} {loc}:{param}") for loc, param in operation["seen_parameters"]
        ]
        typed = (
            (Criterion.REQUEST_TYPE, "seen_request_content_types", "documented_request_content_types"),
            (Criterion.RESPONSE_TYPE, "seen_response_content_types", "documented_response_content_types"),
        )
        for criterion, seen_key, documented_key in typed:
            documented = {kind for kind in operation[documented_key] if "/*" not in kind}
            found += [CoverageItem(criterion, f"{name} {kind}") for kind in documented & set(operation[seen_key])]
        statuses = set(operation["documented_statuses"]) & set(operation["seen_statuses"])
        found += [CoverageItem(Criterion.STATUS, f"{name} {status}") for status in statuses]
        documented_classes = {RestCovReport._status_class(status) for status in operation["documented_statuses"]}
        seen_classes = {RestCovReport._status_class(status) for status in operation["seen_statuses"]}
        both = filter(None, documented_classes & seen_classes)
        found += [CoverageItem(Criterion.STATUS_CLASS, f"{name} {kind}") for kind in both]
        return found

    @staticmethod
    def _status_class(status: str) -> str | None:
        """Turn `404` into `4xx` the way RESTCov does.

        Returns:
            The class, or None for ranges like `4XX` and for `default`.
        """
        return f"{int(status) // 100}xx" if status.isdigit() else None


class RestCov:
    """Runs RESTCov as an outside program on a folder of logs.

    RESTCov is not shipped with this tool, because its license is CC BY-NC-SA. Pass the path to its `restcov.py`.
    """

    def __init__(self, script: Path, spec: Path, python: str = sys.executable) -> None:
        """Keep the script, the API description and the Python that runs RESTCov, which needs PyYAML."""
        self._script = script
        self._spec = spec
        self._python = python

    def measure(self, logs: Path, out: Path) -> RestCovReport:
        """Run RESTCov on a log folder.

        Returns:
            Its report, already checked against its own metrics.

        Raises:
            RuntimeError: If RESTCov fails.
        """
        command = [
            self._python,
            str(self._script),
            "--openapi",
            str(self._spec),
            "--logs",
            str(logs),
            "--out",
            str(out),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"RESTCov failed on {logs}:\n{result.stderr[-4000:]}")
        report = RestCovReport.read(out / "coverage.json")
        report.check()
        return report


class SuiteCoverage:
    """The coverage items of every test in a recording, measured by RESTCov one test at a time."""

    def __init__(self, per_test: dict[JavaTest, CoverageSet], full: CoverageSet, totals: dict[str, int]) -> None:
        """Keep the items per test, the items of the whole suite and the documented totals."""
        self.per_test = per_test
        self.full = full
        self.totals = totals

    @classmethod
    def measure(cls, recording: Recording, restcov: RestCov, out: Path) -> Self:
        """Run RESTCov on every test alone and on the whole suite.

        Returns:
            The coverage of the suite.

        Raises:
            ValueError: If the tests together do not add up to the whole suite.
        """
        per_test: dict[JavaTest, CoverageSet] = {}
        for record in recording.records():
            if record.requests == 0:
                per_test[record.test] = CoverageSet()
                continue
            report = restcov.measure(recording.logs_of(record.test), out / "tests" / record.test.id)
            per_test[record.test] = report.items()
        whole = restcov.measure(recording.logs_folder, out / "suite")
        coverage = cls(per_test, whole.items(), whole.totals())
        if coverage.of(per_test) != coverage.full:
            raise ValueError("The items of the single tests do not add up to the items of the whole suite")
        return coverage

    def of(self, tests: Iterable[JavaTest]) -> CoverageSet:
        """Join the items of some tests.

        Returns:
            The items any of them reaches.
        """
        return CoverageSet.union(self.per_test[test] for test in tests)

    def save(self, path: Path) -> None:
        """Write the coverage as JSON."""
        raw = {
            "totals": self.totals,
            "full": self.full.to_list(),
            "tests": {test.id: items.to_list() for test, items in sorted(self.per_test.items())},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read coverage written by `save`.

        Returns:
            The coverage.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        per_test = {JavaTest.from_id(test): CoverageSet.from_list(items) for test, items in raw["tests"].items()}
        return cls(per_test, CoverageSet.from_list(raw["full"]), raw["totals"])
