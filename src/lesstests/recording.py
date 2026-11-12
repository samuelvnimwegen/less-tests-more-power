import json
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol, Self

from lesstests.exchange import Exchange
from lesstests.java import JavaTest, JvmRunner, Outcome, SuiteFinder
from lesstests.proxy import Proxy
from lesstests.service import Service


@dataclass(slots=True)
class RecordedTest:
    """What one test did when it ran alone: its outcome and every exchange it caused."""

    test: JavaTest
    outcome: Outcome
    seconds: float
    exchanges: list[Exchange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def requests(self) -> int:
        """The number of requests that reached the service."""
        return len(self.exchanges)

    def to_dict(self) -> dict[str, Any]:
        """Turn the record into JSON friendly data.

        Returns:
            The record as a dict.
        """
        return {
            "test": self.test.id,
            "outcome": self.outcome.value,
            "seconds": round(self.seconds, 3),
            "exchanges": [exchange.to_dict() for exchange in self.exchanges],
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read a record saved by `to_dict`.

        Returns:
            The record.
        """
        return cls(
            JavaTest.from_id(raw["test"]),
            Outcome(raw["outcome"]),
            raw["seconds"],
            [Exchange.from_dict(exchange) for exchange in raw["exchanges"]],
            list(raw["errors"]),
        )


class RestCovLogFolder:
    """A folder of numbered `-request.txt` and `-response.txt` files, the log format RESTCov reads."""

    def __init__(self, folder: Path) -> None:
        """Point at the folder, which may not exist yet."""
        self.folder = folder

    def write(self, exchanges: Iterable[Exchange]) -> Path:
        """Write the exchanges, replacing whatever the folder held.

        Returns:
            The folder.
        """
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.folder.mkdir(parents=True)
        for number, exchange in enumerate(exchanges, start=1):
            (self.folder / f"{number}-request.txt").write_text(exchange.request.to_restcov_text(), encoding="utf-8")
            (self.folder / f"{number}-response.txt").write_text(exchange.response.to_restcov_text(), encoding="utf-8")
        return self.folder


class Recording:
    """A folder that holds one recorded suite.

    `tests/` has one JSON file per test. `restcov-logs/` has the same exchanges as RESTCov log files, one folder per
    test, so any group of tests can be measured by copying its folders together.
    """

    RUN_FACTS = frozenset({"tests", "started", "finished"})

    def __init__(self, folder: Path) -> None:
        """Point at the folder, which may not exist yet."""
        self.folder = folder

    @property
    def project_folder(self) -> Path:
        """Where Record keeps its compiled copy of the project."""
        return self.folder / "project"

    @property
    def tests_folder(self) -> Path:
        """Where the JSON records live."""
        return self.folder / "tests"

    @property
    def logs_folder(self) -> Path:
        """Where the RESTCov log folders live."""
        return self.folder / "restcov-logs"

    @property
    def last_change(self) -> float:
        """The time the newest record was saved, or 0 when there is none."""
        return max((path.stat().st_mtime for path in self.tests_folder.glob("*.json")), default=0.0)

    def has(self, test: JavaTest) -> bool:
        """Check whether a test is already recorded.

        Returns:
            True when its record exists.
        """
        return self._record_path(test).exists()

    def save(self, record: RecordedTest) -> None:
        """Write the record and its RESTCov logs, replacing any older ones."""
        self.tests_folder.mkdir(parents=True, exist_ok=True)
        RestCovLogFolder(self.logs_of(record.test)).write(record.exchanges)
        self._record_path(record.test).write_text(json.dumps(record.to_dict(), indent=1), encoding="utf-8")

    def records(self) -> list[RecordedTest]:
        """Read every record in the folder.

        Returns:
            The records, sorted by test.
        """
        paths = sorted(self.tests_folder.glob("*.json"))
        records = [RecordedTest.from_dict(json.loads(path.read_text(encoding="utf-8"))) for path in paths]
        return sorted(records, key=lambda record: record.test)

    def logs_of(self, test: JavaTest) -> Path:
        """Find the RESTCov log folder of one test.

        Returns:
            The folder path.
        """
        return self.logs_folder / test.id

    def gather_logs(self, tests: Iterable[JavaTest], target: Path) -> Path:
        """Copy the log folders of some tests into one folder, so RESTCov measures them together.

        Returns:
            The target folder.
        """
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for test in tests:
            shutil.copytree(self.logs_of(test), target / test.id)
        return target

    def write_meta(self, meta: dict[str, Any]) -> None:
        """Store facts about the run next to the records, such as the suite and the image."""
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    @property
    def has_meta(self) -> bool:
        """Whether a run already wrote its facts here."""
        return (self.folder / "meta.json").exists()

    def check_resume(self, meta: dict[str, Any], tests: list[JavaTest]) -> None:
        """Make sure a run that goes on in this folder records the same suite as before.

        The `RUN_FACTS` are left out, since every run writes its own, and a merge round gets them from the recording.

        Raises:
            ValueError: If a stored fact differs from the new one, or a recorded test is no longer in the suite.
        """
        if self.has_meta:
            stored = self.meta()
            changed = sorted(
                key for key, value in meta.items() if key not in self.RUN_FACTS and stored.get(key) != value
            )
            if changed:
                raise ValueError(f"{self.folder} was recorded with another {', '.join(changed)}. Pick a new --out")
        gone = sorted({path.stem for path in self.tests_folder.glob("*.json")} - {test.id for test in tests})
        if gone:
            raise ValueError(f"{self.folder} holds tests the suite no longer has: {', '.join(gone)}")

    def meta(self) -> dict[str, Any]:
        """Read the facts stored by `write_meta`.

        Returns:
            The stored facts.
        """
        return json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))

    def _record_path(self, test: JavaTest) -> Path:
        return self.tests_folder / f"{test.id}.json"


class ResetPolicy(Protocol):
    """Says before which tests the recorder resets the service."""

    def due(self, previous: JavaTest | None, test: JavaTest) -> bool:
        """Decide whether the service gets reset before `test`. `previous` is the test this run recorded last.

        Returns:
            True for a reset.
        """
        ...

    def facts(self) -> dict[str, str]:
        """Name the policy for the recording's facts, so a resumed run can't mix two policies.

        Returns:
            The facts, empty for the default, so older recordings still resume.
        """
        ...

    def redo(self, recorded: set[JavaTest], tests: list[JavaTest]) -> set[JavaTest]:
        """Pick the recorded tests a resumed run has to record again, so they see the same data as before.

        Returns:
            The tests to record again.
        """
        ...


class EveryTest:
    """Reset before every test, so each one starts from the same data."""

    def due(self, previous: JavaTest | None, test: JavaTest) -> bool:
        """Always reset.

        Returns:
            True.
        """
        return True

    def facts(self) -> dict[str, str]:
        """The default, so nothing to store.

        Returns:
            No facts.
        """
        return {}

    def redo(self, recorded: set[JavaTest], tests: list[JavaTest]) -> set[JavaTest]:
        """Nothing, since every test starts from a reset anyway.

        Returns:
            No tests.
        """
        return set()


class EveryClass:
    """Reset before the first test of each class, for a service whose reset takes too long to do per test.

    The tests of one class still run alone, each in its own JVM, but they see what the earlier ones left behind.
    """

    NAME: ClassVar[str] = "every class"

    def due(self, previous: JavaTest | None, test: JavaTest) -> bool:
        """Reset when the class changes, and before the first test of a run.

        Returns:
            True for a reset.
        """
        return previous is None or previous.class_name != test.class_name

    def facts(self) -> dict[str, str]:
        """Store the policy.

        Returns:
            The facts.
        """
        return {"reset": self.NAME}

    def redo(self, recorded: set[JavaTest], tests: list[JavaTest]) -> set[JavaTest]:
        """Every recorded test of a class that a stopped run left half done.

        Its remaining tests would start from a reset, without what the earlier ones left behind.

        Returns:
            The tests to record again.
        """
        unfinished = {test.class_name for test in tests if test not in recorded}
        return {test for test in recorded if test.class_name in unfinished}


class Recorder:
    """The Record step. Runs every test alone, with the proxy writing down each exchange.

    The reset policy says before which tests the service gets reset.
    """

    def __init__(
        self,
        suite: SuiteFinder,
        runner: JvmRunner,
        service: Service,
        proxy: Proxy,
        recording: Recording,
        reset: ResetPolicy | None = None,
    ) -> None:
        """Wire the parts together. The default policy resets before every test."""
        self._suite = suite
        self._runner = runner
        self._service = service
        self._proxy = proxy
        self._recording = recording
        self._reset = reset or EveryTest()

    def run(
        self,
        meta: dict[str, Any],
        progress: Callable[[int, int, RecordedTest], None] | None = None,
        tests: list[JavaTest] | None = None,
    ) -> None:
        """Record every test that has no record yet, so an interrupted run picks up where it stopped.

        `tests` limits the run to some tests, such as the merged copies of one merge round.
        """
        # The suite's order stays, since JUnit's order keeps @Order and the tests of a class together.
        tests = list(self._suite.tests() if tests is None else tests)
        meta = {**meta, **self._reset.facts()}
        self._recording.check_resume(meta, tests)
        started = datetime.now(UTC).isoformat(timespec="seconds")
        if self._recording.has_meta:
            started = self._recording.meta().get("started", started)
        self._recording.write_meta({**meta, "tests": len(tests), "started": started})
        with self._proxy:
            try:
                self._service.start()
                recorded = {test for test in tests if self._recording.has(test)}
                pending = (set(tests) - recorded) | self._reset.redo(recorded, tests)
                previous: JavaTest | None = None
                for number, test in enumerate(tests, start=1):
                    if test not in pending:
                        continue
                    record = self._record_one(test, self._reset.due(previous, test))
                    previous = test
                    self._recording.save(record)
                    if progress is not None:
                        progress(number, len(tests), record)
            finally:
                self._service.stop()
        finished = datetime.now(UTC).isoformat(timespec="seconds")
        self._recording.write_meta({**meta, "tests": len(tests), "started": started, "finished": finished})

    def _record_one(self, test: JavaTest, reset: bool) -> RecordedTest:
        if reset:
            self._service.reset()

        # A late answer from the previous test must not end up in this one.
        self._proxy.log.take()
        result = self._runner.run(test)
        exchanges, errors = self._proxy.log.take()
        return RecordedTest(test, result.outcome, result.seconds, exchanges, errors)
