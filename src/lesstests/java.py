import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self


@dataclass(frozen=True, slots=True, order=True)
class JavaTest:
    """One TestNG test method, named by its class and method."""

    class_name: str
    method: str

    @property
    def id(self) -> str:
        """The name TestNG takes on its command line, like `generations.single_agent.Pet.addPetWithValidData`."""
        return f"{self.class_name}.{self.method}"

    @classmethod
    def from_id(cls, test_id: str) -> Self:
        """Split a test id back into class and method.

        Returns:
            The test.
        """
        class_name, method = test_id.rsplit(".", 1)
        return cls(class_name, method)


class JavaSuite:
    """The TestNG tests in one package of a Maven project, such as `generations.single_agent`."""

    _COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
    _PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
    _TEST_METHOD = re.compile(
        r"@Test\b(?P<args>\s*\([^)]*\))?\s*(?:@\w+(?:\s*\([^)]*\))?\s*)*"
        r"public\s+(?:final\s+)?void\s+(?P<name>\w+)\s*\("
    )
    _DISABLED = re.compile(r"enabled\s*=\s*false")

    def __init__(self, project: Path, package: str) -> None:
        """Point at a package inside the project's `src/test/java`."""
        self.project = project
        self.package = package

    @property
    def folder(self) -> Path:
        """The source folder of the package."""
        return self.project / "src" / "test" / "java" / Path(*self.package.split("."))

    def tests(self) -> list[JavaTest]:
        """Find every enabled `@Test` method by reading the sources.

        Returns:
            The tests, sorted by class and method.

        Raises:
            FileNotFoundError: If the package folder does not exist.
        """
        if not self.folder.is_dir():
            raise FileNotFoundError(f"No test package at {self.folder}")
        found = [test for path in sorted(self.folder.rglob("*.java")) for test in self._tests_in(path)]
        return sorted(found)

    def _tests_in(self, path: Path) -> list[JavaTest]:
        source = self._COMMENTS.sub("", path.read_text(encoding="utf-8"))
        package = self._PACKAGE.search(source)
        class_name = f"{package.group(1)}.{path.stem}" if package else path.stem
        return [
            JavaTest(class_name, match.group("name"))
            for match in self._TEST_METHOD.finditer(source)
            if not self._DISABLED.search(match.group("args") or "")
        ]


class MavenProject:
    """A private copy of a Maven project, compiled once, so the original folder never changes."""

    def __init__(self, source: Path, workdir: Path) -> None:
        """Set up the copy without making it yet."""
        self.source = source
        self.workdir = workdir

    def prepare(self) -> None:
        """Copy the project, compile its tests and write the dependency classpath.

        Raises:
            RuntimeError: If Maven fails.
        """
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        self.workdir.mkdir(parents=True)
        shutil.copy2(self.source / "pom.xml", self.workdir / "pom.xml")
        shutil.copytree(self.source / "src", self.workdir / "src")
        command = ["mvn", "-q", "-B", "test-compile", "dependency:build-classpath", "-Dmdep.outputFile=classpath.txt"]
        result = subprocess.run(command, cwd=self.workdir, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Maven failed in {self.workdir}:\n{result.stdout[-4000:]}{result.stderr[-4000:]}")

    @property
    def classpath(self) -> str:
        """Compiled tests, compiled main code and every dependency, in that order."""
        dependencies = (self.workdir / "classpath.txt").read_text(encoding="utf-8").strip()
        target = self.workdir / "target"
        return ":".join([str(target / "test-classes"), str(target / "classes"), dependencies])


class Outcome(StrEnum):
    """How one test ended."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of running one test alone, with the time the JVM took."""

    outcome: Outcome
    seconds: float


class JvmRunner:
    """Runs one TestNG test at a time in a fresh JVM, from the project folder so relative paths in tests resolve."""

    _STATUS = {"PASS": Outcome.PASSED, "FAIL": Outcome.FAILED, "SKIP": Outcome.SKIPPED}

    def __init__(self, project: MavenProject, output: Path, timeout: float = 300.0) -> None:
        """Keep the project and the folder for TestNG's reports and console logs."""
        self._project = project
        self._output = output
        self._timeout = timeout

    def run(self, test: JavaTest) -> RunResult:
        """Run one test alone.

        Returns:
            Its outcome. A test missing from TestNG's report, or a JVM that hangs, counts as an error.
        """
        reports = self._output / test.id
        if reports.exists():
            shutil.rmtree(reports)
        reports.mkdir(parents=True)
        command = ["java", "-cp", self._project.classpath, "org.testng.TestNG", "-d", str(reports), "-methods", test.id]
        started = time.monotonic()
        try:
            result = subprocess.run(
                command, cwd=self._project.workdir, capture_output=True, text=True, timeout=self._timeout
            )
            (reports / "console.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        except subprocess.TimeoutExpired:
            return RunResult(Outcome.ERROR, time.monotonic() - started)
        return RunResult(self._read_outcome(reports / "testng-results.xml", test), time.monotonic() - started)

    @classmethod
    def _read_outcome(cls, report: Path, test: JavaTest) -> Outcome:
        if not report.exists():
            return Outcome.ERROR
        for class_node in ET.parse(report).iter("class"):
            if class_node.get("name") != test.class_name:
                continue
            for method in class_node.iter("test-method"):
                if method.get("name") == test.method and method.get("is-config") != "true":
                    return cls._STATUS.get(method.get("status", ""), Outcome.ERROR)
        return Outcome.ERROR
