import argparse
import os
import sys
from pathlib import Path
from typing import Any, Protocol

from lesstests.api import ApiDescription
from lesstests.bench import Benchmark, BenchReport, BenchRun
from lesstests.coverage import RestCov, SuiteCoverage
from lesstests.java import JavaSuite, JvmRunner, MavenProject
from lesstests.merge_study import MergeFolder, MergeStudy
from lesstests.recording import RecordedTest, Recorder, Recording
from lesstests.selection import Budget
from lesstests.study import SelectionStudy
from lesstests.subjects import SUBJECTS, SuiteCatalog


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
    def add_argument(parser: argparse.ArgumentParser) -> None:
        """Declare `--env-file`."""
        parser.add_argument("--env-file", type=Path, help="NAME=value lines the tests read, such as OAuth credentials")

    @staticmethod
    def read(path: Path | None) -> dict[str, str]:
        """Read the file. Blank lines and lines that start with `#` count for nothing.

        Returns:
            The variables, or none when no file was given.
        """
        if path is None:
            return {}
        found = {}
        # An editor may save a byte order mark, and a shell file may write `export NAME=value`.
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip().removeprefix("export ")
            if stripped and not stripped.startswith("#") and "=" in stripped:
                name, value = stripped.split("=", 1)
                found[name.strip()] = value.strip().strip('"').strip("'")
        return found


class RestCovOption:
    """The `--restcov` argument, and RESTCov pointed at the API description a recording's facts name."""

    @staticmethod
    def add_argument(parser: argparse.ArgumentParser) -> None:
        """Declare `--restcov`, which falls back to `LESSTESTS_RESTCOV`."""
        parser.add_argument(
            "--restcov",
            type=Path,
            default=os.environ.get("LESSTESTS_RESTCOV"),
            help="path to RESTCov's restcov.py, or set LESSTESTS_RESTCOV",
        )

    @staticmethod
    def restcov(args: argparse.Namespace, meta: dict[str, Any]) -> RestCov:
        """Build RESTCov for the description and base path in the facts.

        Returns:
            RESTCov, ready to measure.

        Raises:
            SystemExit: If no path to RESTCov was given.
        """
        if args.restcov is None:
            raise SystemExit("Give --restcov or set LESSTESTS_RESTCOV to the path of RESTCov's restcov.py")
        return RestCov(Path(args.restcov).resolve(), Path(meta["spec"]), base_path=meta.get("restcov_base_path"))


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
        EnvFile.add_argument(parser)

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
        RestCovOption.add_argument(parser)

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
        restcov = RestCovOption.restcov(args, recording.meta())
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
        restcov = RestCovOption.restcov(args, recording.meta())
        coverage = self.coverage(recording, restcov)
        budgets = [Budget(percent / 100) for percent in args.budgets]
        out = recording.folder / "selection"
        study = SelectionStudy(recording, coverage, restcov, budgets, args.draws, args.seed, args.passing_only)
        study.run(out)
        print((out / "report.md").read_text(encoding="utf-8"))


class MergeCommand(RecordingCommand):
    """Merges tests that hit the same operation and status, then plans the Select and Select + Merge versions."""

    name = "merge"
    help = "merge tests with the same operation and status, check each merge live, and plan the versions"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the recording, RESTCov, the output folder and the credentials."""
        super().add_arguments(parser)
        parser.add_argument("--out", type=Path, required=True, help="the merge folder")
        parser.add_argument("--rounds", type=int, default=4, help="how often a failed merge may be split")
        EnvFile.add_argument(parser)

    def run(self, args: argparse.Namespace) -> None:
        """Run the Merge step and print the plan."""
        recording = Recording(args.recording.resolve())
        restcov = RestCovOption.restcov(args, recording.meta())
        coverage = self.coverage(recording, restcov)
        study = MergeStudy(recording, restcov, coverage, EnvFile.read(args.env_file), args.rounds, Progress.recorded)
        print(study.run(args.out.resolve()).markdown())


class BenchCommand:
    """Runs Original, Select and Select + Merge as whole suites in rounds and reports runtime and kept coverage."""

    name = "bench"
    help = "run every planned version as a whole suite, in rounds, and report runtime and kept coverage"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the merge folder, the rounds, RESTCov and the credentials."""
        parser.add_argument("--merge", type=Path, required=True, help="a folder made by `lesstests merge`")
        parser.add_argument("--rounds", type=self.at_least_one, default=10)
        parser.add_argument("--warmup", type=int, default=1, help="Original runs before the counted rounds")
        parser.add_argument("--out", type=Path, help="the benchmark folder, by default `bench` in the merge folder")
        RestCovOption.add_argument(parser)
        EnvFile.add_argument(parser)

    @staticmethod
    def at_least_one(text: str) -> int:
        """Read a number of rounds, since a report needs at least one.

        Returns:
            The number.

        Raises:
            argparse.ArgumentTypeError: If it's below 1.
        """
        number = int(text)
        if number < 1:
            raise argparse.ArgumentTypeError("needs at least 1")
        return number

    def run(self, args: argparse.Namespace) -> None:
        """Run the benchmark and print the report."""
        folder = MergeFolder(args.merge.resolve())
        out = (args.out or folder.folder / "bench").resolve()
        meta = folder.meta()
        restcov = RestCovOption.restcov(args, meta)
        api = ApiDescription.load(Path(meta["spec"]), meta.get("restcov_base_path"))
        connection = SUBJECTS[meta["subject"]].connect(EnvFile.read(args.env_file))

        # The merge step already compiled this copy, and every version runs from it.
        runner = JvmRunner(MavenProject(Path(meta["project"]), folder.project), out, settings=connection.settings)
        plan = folder.plan()
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
