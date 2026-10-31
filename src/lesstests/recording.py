import json
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from lesstests.exchange import Exchange
from lesstests.java import JavaSuite, JavaTest, JvmRunner, Outcome
from lesstests.proxy import RecordingProxy
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


class Recording:
    """A folder that holds one recorded suite.

    `tests/` has one JSON file per test. `restcov-logs/` has the same exchanges as RESTCov log files, one folder per
    test, so any group of tests can be measured by copying its folders together.
    """

    def __init__(self, folder: Path) -> None:
        """Point at the folder, which may not exist yet."""
        self.folder = folder

    @property
    def tests_folder(self) -> Path:
        """Where the JSON records live."""
        return self.folder / "tests"

    @property
    def logs_folder(self) -> Path:
        """Where the RESTCov log folders live."""
        return self.folder / "restcov-logs"

    def has(self, test: JavaTest) -> bool:
        """Check whether a test is already recorded.

        Returns:
            True when its record exists.
        """
        return self._record_path(test).exists()

    def save(self, record: RecordedTest) -> None:
        """Write the record and its RESTCov logs, replacing any older ones."""
        self.tests_folder.mkdir(parents=True, exist_ok=True)
        logs = self.logs_of(record.test)
        if logs.exists():
            shutil.rmtree(logs)
        logs.mkdir(parents=True)
        for number, exchange in enumerate(record.exchanges, start=1):
            (logs / f"{number}-request.txt").write_text(exchange.request.to_restcov_text(), encoding="utf-8")
            (logs / f"{number}-response.txt").write_text(exchange.response.to_restcov_text(), encoding="utf-8")
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

    def meta(self) -> dict[str, Any]:
        """Read the facts stored by `write_meta`.

        Returns:
            The stored facts.
        """
        return json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))

    def _record_path(self, test: JavaTest) -> Path:
        return self.tests_folder / f"{test.id}.json"


class Recorder:
    """The Record step. Runs every test alone after a reset, with the proxy writing down each exchange."""

    def __init__(
        self, suite: JavaSuite, runner: JvmRunner, service: Service, proxy: RecordingProxy, recording: Recording
    ) -> None:
        """Wire the parts together."""
        self._suite = suite
        self._runner = runner
        self._service = service
        self._proxy = proxy
        self._recording = recording

    def run(self, meta: dict[str, Any], progress: Callable[[int, int, RecordedTest], None] | None = None) -> None:
        """Record every test that has no record yet, so an interrupted run picks up where it stopped."""
        tests = self._suite.tests()
        started = datetime.now(UTC).isoformat(timespec="seconds")
        self._recording.write_meta({**meta, "tests": len(tests), "started": started})
        with self._proxy:
            self._service.start()
            try:
                for number, test in enumerate(tests, start=1):
                    if self._recording.has(test):
                        continue
                    record = self._record_one(test)
                    self._recording.save(record)
                    if progress is not None:
                        progress(number, len(tests), record)
            finally:
                self._service.stop()
        finished = datetime.now(UTC).isoformat(timespec="seconds")
        self._recording.write_meta({**meta, "tests": len(tests), "started": started, "finished": finished})

    def _record_one(self, test: JavaTest) -> RecordedTest:
        self._service.reset()

        # A late answer from the previous test must not end up in this one.
        self._proxy.log.take()
        result = self._runner.run(test)
        exchanges, errors = self._proxy.log.take()
        return RecordedTest(test, result.outcome, result.seconds, exchanges, errors)
