import argparse
import os
import sys
from pathlib import Path
from typing import Protocol

from lesstests.coverage import RestCov, SuiteCoverage
from lesstests.java import JavaSuite, JvmRunner, MavenProject
from lesstests.proxy import RecordingProxy
from lesstests.recording import RecordedTest, Recorder, Recording
from lesstests.selection import Budget
from lesstests.service import DockerService
from lesstests.study import SelectionStudy
from lesstests.subjects import SUBJECTS


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

    def run(self, args: argparse.Namespace) -> None:
        """Prepare the project, start the service and record."""
        subject = SUBJECTS[args.subject]
        project = args.project.resolve()
        recording = Recording(args.out.resolve())
        maven = MavenProject(project, recording.folder / "project")
        print(f"Compiling a copy of {project} ...", flush=True)
        maven.prepare()
        suite = JavaSuite(maven.workdir, subject.package(args.suite))
        service = DockerService(subject.container, subject.service_port, f"lesstests-{subject.name}")
        proxy = RecordingProxy(subject.listen, service.address)
        recorder = Recorder(suite, JvmRunner(maven, recording.folder / "testng"), service, proxy, recording)
        meta = {
            "subject": subject.name,
            "suite": args.suite,
            "package": suite.package,
            "project": str(project),
            "spec": str(project / subject.spec_file),
            "image": subject.container.image,
        }
        recorder.run(meta, self._progress)

    @staticmethod
    def _progress(number: int, total: int, record: RecordedTest) -> None:
        print(f"[{number}/{total}] {record.outcome.value:7} {record.requests:3} requests  {record.test.id}", flush=True)


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
        return RestCov(Path(args.restcov).resolve(), Path(recording.meta()["spec"]))


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
        saved = recording.folder / "coverage" / "coverage.json"
        if saved.exists():
            coverage = SuiteCoverage.load(saved)
        else:
            coverage = SuiteCoverage.measure(recording, restcov, recording.folder / "coverage")
            coverage.save(saved)
        budgets = [Budget(percent / 100) for percent in args.budgets]
        out = recording.folder / "selection"
        study = SelectionStudy(recording, coverage, restcov, budgets, args.draws, args.seed, args.passing_only)
        study.run(out)
        print((out / "report.md").read_text(encoding="utf-8"))


COMMANDS: tuple[Command, ...] = (RecordCommand(), CoverageCommand(), SelectCommand())


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
