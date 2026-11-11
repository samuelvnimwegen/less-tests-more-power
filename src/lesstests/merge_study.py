"""Runs the Merge step on a recording and writes the merge folder that the benchmark reads."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lesstests.api import ApiDescription
from lesstests.coverage import RestCov, SuiteCoverage
from lesstests.java import JavaSuite, JavaTest, JvmRunner, MavenProject, Overlay
from lesstests.merge import MergedTest, MergeOutcome, MergePlanner, Merger
from lesstests.recording import RecordedTest, Recorder, Recording
from lesstests.subjects import SUBJECTS, Subject
from lesstests.versions import VersionPlan, VersionPlanner

Progress = Callable[[int, int, RecordedTest], None]


class MergeRoundRunner:
    """Compiles the project with one round of merged copies and records each copy alone through the proxy."""

    def __init__(self, subject: Subject, meta: dict[str, Any], env: dict[str, str], progress: Progress | None) -> None:
        """Keep what every round needs to rebuild the project and reach the API."""
        self._subject = subject
        self._meta = meta
        self._env = env
        self._progress = progress

    def project(self, edited: dict[Path, str], folder: Path) -> MavenProject:
        """Copy and compile the project with the edited test files on top of the subject's own overlays.

        Returns:
            The compiled copy.
        """
        source = Path(self._meta["project"])
        overlays = list(self._subject.overlays(source, self._meta["suite"]))
        for relative, text in sorted(edited.items()):
            path = folder / "sources" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            overlays.append(Overlay(path, str(Path("src", "test", "java") / relative)))
        maven = MavenProject(source, folder / "project", tuple(overlays))
        maven.prepare()
        return maven

    def record(self, merged: list[MergedTest], edited: dict[Path, str], folder: Path) -> dict[JavaTest, RecordedTest]:
        """Record every merged test of one round alone.

        Returns:
            The record of each merged test.
        """
        print(f"Compiling round {folder.name} with {len(merged)} merged tests ...", flush=True)
        maven = self.project(edited, folder)
        connection = self._subject.connect(self._env)
        recording = Recording(folder / "recording")
        runner = JvmRunner(maven, folder / "testng", settings=connection.settings)
        suite = JavaSuite(maven.workdir, self._meta["package"])
        recorder = Recorder(suite, runner, connection.service, connection.proxy, recording)
        recorder.run(self._meta, self._progress, [merge.test for merge in merged])
        return {record.test: record for record in recording.records()}


class MergeFolder:
    """The folder `lesstests merge` writes and `lesstests bench` reads.

    It holds `meta.json`, `merges.json`, `plan.json`, `plan.md`, the compiled `project/` and the `merged/` recording.
    """

    def __init__(self, folder: Path) -> None:
        """Point at the folder, which may not exist yet."""
        self.folder = folder

    @property
    def project(self) -> Path:
        """The compiled copy of the project with every kept merge. Every benchmark run starts from it."""
        return self.folder / "project"

    def meta(self) -> dict[str, Any]:
        """Read the facts of the recording the merge started from.

        Returns:
            The facts, plus the path of that recording.
        """
        return json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))

    def plan(self) -> VersionPlan:
        """Read the versions the benchmark runs.

        Returns:
            The plan.
        """
        return VersionPlan.load(self.folder / "plan.json")

    def save(self, meta: dict[str, Any], outcome: MergeOutcome, skipped: list[tuple[JavaTest, str]]) -> None:
        """Write the facts and every merge tried, kept, or skipped with its reason."""
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        merges = {
            "accepted": [merge.to_dict() for merge in outcome.accepted],
            "attempts": outcome.attempts,
            "skipped": [{"test": test.id, "reason": reason} for test, reason in skipped],
        }
        (self.folder / "merges.json").write_text(json.dumps(merges, indent=1), encoding="utf-8")

    def save_plan(self, plan: VersionPlan) -> None:
        """Write the plan as JSON for the benchmark and as Markdown for a reader."""
        plan.save(self.folder / "plan.json")
        (self.folder / "plan.md").write_text(plan.markdown(), encoding="utf-8")


class MergeStudy:
    """The Merge step: merge the tests of a recording, check every merge live, and plan the versions to benchmark."""

    def __init__(
        self,
        recording: Recording,
        restcov: RestCov,
        coverage: SuiteCoverage,
        env: dict[str, str],
        rounds: int,
        progress: Progress | None = None,
    ) -> None:
        """Keep the recording with its coverage, the credentials for the live rounds, and the round limit."""
        self._recording = recording
        self._restcov = restcov
        self._coverage = coverage
        self._env = env
        self._rounds = rounds
        self._progress = progress

    def run(self, out: Path) -> VersionPlan:
        """Merge, record the merged tests, compile the final project, measure the kept merges and plan.

        Returns:
            The plan with Original, Select and Select + Merge.
        """
        meta = self._recording.meta()
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        records = self._recording.records()
        MergePlanner.check_matching(records, api, self._coverage)
        runner = MergeRoundRunner(SUBJECTS[meta["subject"]], meta, self._env, self._progress)
        planner = MergePlanner(records, api, self._recording.folder / "project" / "src" / "test" / "java")
        outcome = Merger(planner, runner, api, self._rounds).run(out / "rounds")
        folder = MergeFolder(out)
        folder.save({**meta, "recording": str(self._recording.folder)}, outcome, planner.skipped)

        print(f"Compiling the final project with {len(outcome.accepted)} merged tests ...", flush=True)
        runner.project(planner.edited_files(outcome.accepted), out)
        merged_part = None
        if outcome.accepted:
            merged = Recording(out / "merged")
            for record in outcome.records.values():
                merged.save(record)
            merged.write_meta(meta)
            merged_records = merged.records()
            merged_part = (SuiteCoverage.measure(merged, self._restcov, merged.folder / "coverage"), merged_records)
        plan = VersionPlanner(api, (self._coverage, records), merged_part).plan()
        folder.save_plan(plan)
        return plan
