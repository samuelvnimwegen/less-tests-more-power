import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol

from lesstests.api import ApiDescription
from lesstests.bench import Benchmark, BenchReport, BenchRun
from lesstests.coverage import RestCov, SuiteCoverage
from lesstests.java import JavaSuite, JavaTest, JvmRunner, MavenProject, Overlay
from lesstests.merge import MergedTest, MergePlanner, Merger
from lesstests.recording import RecordedTest, Recorder, Recording
from lesstests.selection import Budget
from lesstests.study import SelectionStudy
from lesstests.subjects import SUBJECTS, Subject, SuiteCatalog
from lesstests.versions import VersionPlan, VersionPlanner


class Command(Protocol):
    """One subcommand of the command line."""

    name: str
    help: str

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the command's arguments."""
        ...

    def run(self, args: argparse.Namespace) -> None:
        """Do the work."""
        ...


class EnvFile:
    """A file of `NAME=value` lines, such as the OAuth credentials a live suite reads. It never lands in a recording."""

    @staticmethod
    def read(path: Path | None) -> dict[str, str]:
        """Read the file. Blank lines and lines that start with `#` count for nothing.

        Returns:
            The variables, or none when no file was given.
        """
        if path is None:
            return {}
        found = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                name, value = stripped.split("=", 1)
                found[name.strip()] = value.strip().strip('"').strip("'")
        return found


class Progress:
    """Console lines for the long running steps."""

    @staticmethod
    def recorded(number: int, total: int, record: RecordedTest) -> None:
        """Print one recorded test."""
        print(f"[{number}/{total}] {record.outcome.value:7} {record.requests:3} requests  {record.test.id}", flush=True)

    @staticmethod
    def benched(run: BenchRun) -> None:
        """Print one benchmark run."""
        print(
            f"round {run.round:2} {run.version:15} {run.seconds:7.1f} s  {run.requests:4} requests  "
            f"{len(run.coverage)} items",
            flush=True,
        )


class RecordCommand:
    """Runs every test of a suite alone through the proxy and stores the exchanges."""

    name = "record"
    help = "record every test of a suite alone, through the proxy"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the project, suite and output folder."""
        parser.add_argument("--subject", default="petstore", choices=sorted(SUBJECTS))
        parser.add_argument("--project", type=Path, required=True, help="the Maven project of the subject")
        parser.add_argument("--suite", required=True, help="suite name, like single-agent or multi-agent")
        parser.add_argument("--out", type=Path, required=True, help="the recording folder")
        parser.add_argument("--env-file", type=Path, help="NAME=value lines the tests read, such as OAuth credentials")

    def run(self, args: argparse.Namespace) -> None:
        """Prepare the project, start the service and record."""
        subject = SUBJECTS[args.subject]
        project = args.project.resolve()
        recording = Recording(args.out.resolve())
        maven = MavenProject(project, recording.folder / "project", subject.overlays(project, args.suite))
        print(f"Compiling a copy of {project} ...", flush=True)
        maven.prepare()
        suite = JavaSuite(maven.workdir, SuiteCatalog.source(subject, args.suite).package)
        connection = subject.connect(EnvFile.read(args.env_file))
        runner = JvmRunner(maven, recording.folder / "testng", settings=connection.settings)
        recorder = Recorder(suite, runner, connection.service, connection.proxy, recording)
        meta = {
            "subject": subject.name,
            "suite": args.suite,
            "package": suite.package,
            "project": str(project),
            "spec": str(project / subject.spec_file),
            "restcov_base_path": subject.restcov_base_path,
            **subject.meta(),
        }
        recorder.run(meta, Progress.recorded)


class RecordingCommand:
    """A command that works on a finished recording and needs RESTCov."""

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording and RESTCov."""
        parser.add_argument("--recording", type=Path, required=True, help="a folder made by `lesstests record`")
        parser.add_argument(
            "--restcov",
            type=Path,
            default=os.environ.get("LESSTESTS_RESTCOV"),
            help="path to RESTCov's restcov.py, or set LESSTESTS_RESTCOV",
        )

    @staticmethod
    def restcov(args: argparse.Namespace, recording: Recording) -> RestCov:
        """Point RESTCov at the API description the recording names.

        Returns:
            RESTCov, ready to measure.

        Raises:
            SystemExit: If no path to RESTCov was given.
        """
        if args.restcov is None:
            raise SystemExit("Give --restcov or set LESSTESTS_RESTCOV to the path of RESTCov's restcov.py")
        meta = recording.meta()
        return RestCov(Path(args.restcov).resolve(), Path(meta["spec"]), base_path=meta.get("restcov_base_path"))

    @staticmethod
    def coverage(recording: Recording, restcov: RestCov) -> SuiteCoverage:
        """Reuse the saved coverage while it still belongs to the recording, or measure it again.

        Returns:
            The coverage.
        """
        saved = recording.folder / "coverage" / "coverage.json"
        coverage = SuiteCoverage.load_if_current(saved, recording)
        if coverage is None:
            coverage = SuiteCoverage.measure(recording, restcov, recording.folder / "coverage")
            coverage.save(saved)
        return coverage


class CoverageCommand(RecordingCommand):
    """Measures the coverage of every recorded test with RESTCov."""

    name = "coverage"
    help = "measure the coverage of every recorded test with RESTCov"

    def run(self, args: argparse.Namespace) -> None:
        """Measure and save the coverage."""
        recording = Recording(args.recording.resolve())
        restcov = self.restcov(args, recording)
        coverage = SuiteCoverage.measure(recording, restcov, recording.folder / "coverage")
        coverage.save(recording.folder / "coverage" / "coverage.json")
        print(f"Full suite: {len(coverage.full)} items, {coverage.full.counts()}")


class SelectCommand(RecordingCommand):
    """Builds the Selected and Random versions of the suite at each budget and reports the coverage they lose."""

    name = "select"
    help = "compare Selected with Random at each budget, from the recording alone"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording, RESTCov, the budgets and the draws."""
        super().add_arguments(parser)
        parser.add_argument("--budgets", type=float, nargs="+", default=[75, 50, 25], help="percentages to keep")
        parser.add_argument("--draws", type=int, default=30, help="random draws per budget")
        parser.add_argument("--seed", type=int, default=1)
        parser.add_argument("--passing-only", action="store_true", help="only pick tests that passed alone")

    def run(self, args: argparse.Namespace) -> None:
        """Run the study and print the report."""
        recording = Recording(args.recording.resolve())
        restcov = self.restcov(args, recording)
        coverage = self.coverage(recording, restcov)
        budgets = [Budget(percent / 100) for percent in args.budgets]
        out = recording.folder / "selection"
        study = SelectionStudy(recording, coverage, restcov, budgets, args.draws, args.seed, args.passing_only)
        study.run(out)
        print((out / "report.md").read_text(encoding="utf-8"))


class MergeRoundRunner:
    """Compiles the project with one round of merged copies and records each copy alone through the proxy."""

    def __init__(self, subject: Subject, meta: dict[str, Any], env: dict[str, str]) -> None:
        """Keep what every round needs to rebuild the project and reach the API."""
        self._subject = subject
        self._meta = meta
        self._env = env

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
        recorder.run(self._meta, Progress.recorded, [merge.test for merge in merged])
        return {record.test: record for record in recording.records()}


class MergeCommand(RecordingCommand):
    """Merges tests that hit the same operation and status, then plans the Select and Select + Merge versions."""

    name = "merge"
    help = "merge tests with the same operation and status, check each merge live, and plan the versions"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording, RESTCov, the output folder and the credentials."""
        super().add_arguments(parser)
        parser.add_argument("--out", type=Path, required=True, help="the merge folder")
        parser.add_argument("--rounds", type=int, default=4, help="how often a failed merge may be split")
        parser.add_argument("--env-file", type=Path, help="NAME=value lines the tests read, such as OAuth credentials")

    def run(self, args: argparse.Namespace) -> None:
        """Merge, record the merged tests, measure them and write the plan."""
        recording = Recording(args.recording.resolve())
        out = args.out.resolve()
        meta = recording.meta()
        subject = SUBJECTS[meta["subject"]]
        restcov = self.restcov(args, recording)
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        coverage = self.coverage(recording, restcov)
        records = recording.records()
        runner = MergeRoundRunner(subject, meta, EnvFile.read(args.env_file))
        planner = MergePlanner(records, api, recording.folder / "project" / "src" / "test" / "java")
        outcome = Merger(planner, runner, api, args.rounds).run(out / "rounds")
        out.mkdir(parents=True, exist_ok=True)
        (out / "meta.json").write_text(json.dumps({**meta, "recording": str(recording.folder)}, indent=2))
        merges = {"accepted": [merge.to_dict() for merge in outcome.accepted], "attempts": outcome.attempts}
        (out / "merges.json").write_text(json.dumps(merges, indent=1), encoding="utf-8")

        print(f"Compiling the final project with {len(outcome.accepted)} merged tests ...", flush=True)
        runner.project(planner.edited_files(outcome.accepted), out)
        merged_part = None
        if outcome.accepted:
            merged = Recording(out / "merged")
            for record in outcome.records.values():
                merged.save(record)
            merged.write_meta(meta)
            merged_records = merged.records()
            merged_part = (SuiteCoverage.measure(merged, restcov, merged.folder / "coverage"), merged_records)
        plan = VersionPlanner(api, (coverage, records), merged_part).plan()
        plan.save(out / "plan.json")
        (out / "plan.md").write_text(plan.markdown(), encoding="utf-8")
        print(plan.markdown())


class BenchCommand:
    """Runs Original, Select and Select + Merge as whole suites in rounds and reports runtime and kept coverage."""

    name = "bench"
    help = "run every planned version as a whole suite, in rounds, and report runtime and kept coverage"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the merge folder, the rounds, RESTCov and the credentials."""
        parser.add_argument("--merge", type=Path, required=True, help="a folder made by `lesstests merge`")
        parser.add_argument("--rounds", type=int, default=10)
        parser.add_argument("--warmup", type=int, default=1, help="Original runs before the counted rounds")
        parser.add_argument("--out", type=Path, help="the benchmark folder, by default `bench` in the merge folder")
        parser.add_argument(
            "--restcov",
            type=Path,
            default=os.environ.get("LESSTESTS_RESTCOV"),
            help="path to RESTCov's restcov.py, or set LESSTESTS_RESTCOV",
        )
        parser.add_argument("--env-file", type=Path, help="NAME=value lines the tests read, such as OAuth credentials")

    def run(self, args: argparse.Namespace) -> None:
        """Run the benchmark and print the report.

        Raises:
            SystemExit: If no path to RESTCov was given.
        """
        if args.restcov is None:
            raise SystemExit("Give --restcov or set LESSTESTS_RESTCOV to the path of RESTCov's restcov.py")
        folder = args.merge.resolve()
        out = (args.out or folder / "bench").resolve()
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        subject = SUBJECTS[meta["subject"]]
        plan = VersionPlan.load(folder / "plan.json")
        base_path = meta.get("restcov_base_path")
        restcov = RestCov(Path(args.restcov).resolve(), Path(meta["spec"]), base_path=base_path)
        api = ApiDescription.load(Path(meta["spec"]), base_path)
        connection = subject.connect(EnvFile.read(args.env_file))

        # The merge step already compiled this copy, and every version runs from it.
        runner = JvmRunner(MavenProject(Path(meta["project"]), folder / "project"), out, settings=connection.settings)
        benchmark = Benchmark(plan, runner, connection.proxy, connection.proxy.log, connection.service, (restcov, api))
        runs = benchmark.run(out, args.rounds, args.warmup, Progress.benched)
        report = BenchReport(plan, runs, Benchmark.repeated(out)).markdown()
        (out / "report.md").write_text(report, encoding="utf-8")
        print(report)


COMMANDS: tuple[Command, ...] = (
    RecordCommand(),
    CoverageCommand(),
    SelectCommand(),
    MergeCommand(),
    BenchCommand(),
)


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and run the chosen command."""
    parser = argparse.ArgumentParser(prog="lesstests", description="Record REST API test suites and cut them down.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        command.add_arguments(commands.add_parser(command.name, help=command.help))
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    next(command for command in COMMANDS if command.name == args.command).run(args)


if __name__ == "__main__":
    main()
