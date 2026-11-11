import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Self


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

    # Literals match before comments, so a "/*" inside a string can't open a comment.
    _LITERALS_AND_COMMENTS = re.compile(
        r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*[\s\S]*?\*/'
    )
    _PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
    _TEST_METHOD = re.compile(
        r"@Test\b(?P<args>\s*\([^)]*\))?\s*(?:@\w+(?:\s*\([^)]*\))?\s*)*"
        r"public\s+(?:final\s+)?void\s+(?P<name>\w+)\s*\("
    )
    _CLASS_TEST = re.compile(
        r"@Test\b(?:\s*\([^)]*\))?\s*(?:@\w+(?:\s*\([^)]*\))?\s*)*(?:public\s+)?(?:(?:final|abstract)\s+)*class\b"
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
        source = self._LITERALS_AND_COMMENTS.sub(self._blank, path.read_text(encoding="utf-8"))
        if self._CLASS_TEST.search(source):
            raise ValueError(f"{path} puts @Test on the class, which lesstests does not support")
        package = self._PACKAGE.search(source)
        class_name = f"{package.group(1)}.{path.stem}" if package else path.stem
        return [
            JavaTest(class_name, match.group("name"))
            for match in self._TEST_METHOD.finditer(source)
            if not self._DISABLED.search(match.group("args") or "")
        ]

    @staticmethod
    def _blank(match: re.Match[str]) -> str:
        """Empty a literal and turn a comment into a space, so a `)` or `/*` in a string can't confuse the patterns.

        Returns:
            The blanked text.
        """
        text = match.group()
        if text.startswith(('"', "'")):
            return text[0] * 2
        return " "


@dataclass(frozen=True, slots=True)
class Overlay:
    """A file or folder put into the project copy before it compiles. A folder replaces the target folder."""

    source: Path
    target: str

    def apply(self, workdir: Path) -> None:
        """Copy the source into the project copy.

        Raises:
            FileNotFoundError: If the source does not exist.
        """
        target = workdir / self.target
        if self.source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(self.source, target)
        elif self.source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source, target)
        else:
            raise FileNotFoundError(f"Nothing to put into the project at {self.source}")


class MavenProject:
    """A private copy of a Maven project, compiled once, so the original folder never changes."""

    _LISTENER = "LesstestsProxyListener.java"

    def __init__(self, source: Path, workdir: Path, overlays: tuple[Overlay, ...] = ()) -> None:
        """Set up the copy without making it yet. The overlays go in after the copy, in order."""
        self.source = source
        self.workdir = workdir
        self.overlays = overlays

    @classmethod
    def support_overlay(cls) -> Overlay:
        """Place the TestNG listener that sends RestAssured through the intercepting proxy.

        Returns:
            The overlay.
        """
        return Overlay(Path(__file__).parent / "support" / cls._LISTENER, f"src/test/java/lesstests/{cls._LISTENER}")

    def prepare(self) -> None:
        """Copy the whole project without its build output, apply the overlays, compile and write the classpath.

        Raises:
            ValueError: If the copy would sit inside the project, or the project inside the copy.
            RuntimeError: If Maven fails.
        """
        source, workdir = self.source.resolve(), self.workdir.resolve()
        if workdir.is_relative_to(source) or source.is_relative_to(workdir):
            raise ValueError(f"The copy at {workdir} can't sit inside {source} or hold it. Pick another --out")
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(source, workdir, ignore=shutil.ignore_patterns("target", ".git"))
        for overlay in self.overlays:
            overlay.apply(self.workdir)
        command = ["mvn", "-q", "-B", "test-compile", "dependency:build-classpath", "-Dmdep.outputFile=classpath.txt"]
        result = subprocess.run(command, cwd=self.workdir, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"Maven failed in {self.workdir}:\n{result.stdout[-4000:]}{result.stderr[-4000:]}")

    @property
    def classpath(self) -> str:
        """Compiled tests, compiled main code and every dependency, in that order."""
        dependencies = (self.workdir / "classpath.txt").read_text(encoding="utf-8").strip()
        target = self.workdir / "target"
        return ":".join([str(target / "test-classes"), str(target / "classes"), dependencies])


@dataclass(frozen=True, slots=True)
class JvmSettings:
    """Extra system properties, environment variables and TestNG listeners for a test JVM."""

    SHORTEST_HIDDEN_VALUE: ClassVar[int] = 8
    _BEARER_TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"(?i)(\bbearer\s+)[\w.~+/=-]{8,}")

    properties: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    listeners: tuple[str, ...] = ()

    def merged(self, other: "JvmSettings") -> "JvmSettings":
        """Join two settings. The other one wins where both set the same name.

        Returns:
            The joined settings.
        """
        listeners = self.listeners + tuple(name for name in other.listeners if name not in self.listeners)
        return JvmSettings({**self.properties, **other.properties}, {**self.env, **other.env}, listeners)

    def java_arguments(self) -> list[str]:
        """Turn the properties into `-D` flags, which go before the main class.

        Returns:
            The flags.
        """
        return [f"-D{key}={value}" for key, value in self.properties.items()]

    def testng_arguments(self) -> list[str]:
        """Name the listeners for TestNG, which go after the main class.

        Returns:
            The arguments, empty when there is no listener.
        """
        return ["-listener", ",".join(self.listeners)] if self.listeners else []

    def environment(self) -> dict[str, str] | None:
        """Build the child environment.

        Returns:
            This process's environment plus the extra variables, or None to inherit it unchanged.
        """
        return {**os.environ, **self.env} if self.env else None

    def hide_secrets(self, text: str) -> str:
        """Replace every bearer token and every env value of `SHORTEST_HIDDEN_VALUE` characters or more.

        The env holds credentials. A client that logs its requests prints them, and also the access token it got
        for them, which the env never held. A shorter env value would also wipe out ordinary words and numbers.

        Returns:
            The text with `<redacted>` in their place.
        """
        for value in sorted(self.env.values(), key=len, reverse=True):
            if len(value) >= self.SHORTEST_HIDDEN_VALUE:
                text = text.replace(value, "<redacted>")
        return self._BEARER_TOKEN.sub(r"\1<redacted>", text)


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


@dataclass(frozen=True, slots=True)
class SuiteRun:
    """The outcome of every test of one whole run in a single JVM.

    `seconds` is the wall clock of the whole JVM. `test_seconds` adds up what TestNG timed per test.
    """

    outcomes: dict[JavaTest, Outcome]
    seconds: float
    test_seconds: float


class JvmRunner:
    """Runs TestNG tests in a fresh JVM, from the project folder so relative paths in tests resolve."""

    _STATUS = {"PASS": Outcome.PASSED, "FAIL": Outcome.FAILED, "SKIP": Outcome.SKIPPED}
    _WORST_FIRST = (Outcome.FAILED, Outcome.ERROR, Outcome.SKIPPED, Outcome.PASSED)

    def __init__(
        self, project: MavenProject, output: Path, timeout: float = 300.0, settings: JvmSettings | None = None
    ) -> None:
        """Keep the project, the folder for TestNG's reports and console logs, and the JVM settings."""
        self._project = project
        self._output = output
        self._timeout = timeout
        self._settings = settings or JvmSettings()

    @property
    def settings(self) -> JvmSettings:
        """The settings every JVM of this runner gets."""
        return self._settings

    def run(self, test: JavaTest) -> RunResult:
        """Run one test alone.

        Returns:
            Its outcome. A test missing from TestNG's report, or a JVM that hangs, counts as an error.
        """
        reports = self._fresh(self._output / test.id)
        started = time.monotonic()
        if not self._java(["-methods", test.id], reports, self._timeout):
            return RunResult(Outcome.ERROR, time.monotonic() - started)
        return RunResult(self._read_outcome(reports / "testng-results.xml", test), time.monotonic() - started)

    def run_suite(self, tests: list[JavaTest], name: str, timeout: float = 1800.0) -> SuiteRun:
        """Run all these tests together in one JVM, through a TestNG suite file that lists every method.

        A suite file keeps the command line short, since a suite can list hundreds of methods.

        Returns:
            The outcome of every test and the times. Every test counts as an error when the JVM hangs.
        """
        reports = self._fresh(self._output / name)
        suite_file = reports / "suite.xml"
        suite_file.write_text(self.suite_xml(tests, name), encoding="utf-8")
        started = time.monotonic()
        finished = self._java([str(suite_file)], reports, timeout)
        seconds = time.monotonic() - started
        report = reports / "testng-results.xml"
        if not finished:
            return SuiteRun(dict.fromkeys(tests, Outcome.ERROR), seconds, 0.0)
        outcomes = {test: self._read_outcome(report, test) for test in tests}
        return SuiteRun(outcomes, seconds, self._read_seconds(report, tests))

    @staticmethod
    def suite_xml(tests: Iterable[JavaTest], name: str) -> str:
        """Write a TestNG suite that runs exactly these methods, grouped by class in a stable order.

        Returns:
            The suite as XML text.
        """
        suite = ET.Element("suite", name=name)
        block = ET.SubElement(suite, "test", {"name": name, "preserve-order": "true"})
        classes = ET.SubElement(block, "classes")
        by_class: dict[str, list[str]] = {}
        for test in sorted(tests):
            by_class.setdefault(test.class_name, []).append(test.method)
        for class_name, methods in by_class.items():
            methods_node = ET.SubElement(ET.SubElement(classes, "class", name=class_name), "methods")
            for method in methods:
                ET.SubElement(methods_node, "include", name=method)
        doctype = '<!DOCTYPE suite SYSTEM "https://testng.org/testng-1.0.dtd">'
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{doctype}\n{ET.tostring(suite, encoding="unicode")}\n'

    def command(self, selection: list[str], reports: Path) -> list[str]:
        """Build the java command line for a selection, either `-methods a.B.c` or a suite file.

        Returns:
            The command.
        """
        return [
            "java",
            *self._settings.java_arguments(),
            "-cp",
            self._project.classpath,
            "org.testng.TestNG",
            "-d",
            str(reports),
            *self._settings.testng_arguments(),
            *selection,
        ]

    def _java(self, selection: list[str], reports: Path, timeout: float) -> bool:
        """Run TestNG and keep its console output next to its reports.

        Returns:
            False when the JVM hung and was stopped.
        """
        try:
            result = subprocess.run(
                self.command(selection, reports),
                cwd=self._project.workdir,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env=self._settings.environment(),
            )
        except subprocess.TimeoutExpired:
            return False
        console = self._settings.hide_secrets(result.stdout + result.stderr)
        (reports / "console.log").write_text(console, encoding="utf-8")
        return True

    @staticmethod
    def _fresh(folder: Path) -> Path:
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        return folder

    @classmethod
    def _methods(cls, report: Path, test: JavaTest) -> list[ET.Element]:
        return [
            method
            for class_node in ET.parse(report).iter("class")
            if class_node.get("name") == test.class_name
            for method in class_node.iter("test-method")
            if method.get("name") == test.method and method.get("is-config") != "true"
        ]

    @classmethod
    def _read_outcome(cls, report: Path, test: JavaTest) -> Outcome:
        """Read the outcome of a test. A test that ran more than once gets its worst run, so a retry counts as skipped.

        Returns:
            The worst outcome, or an error when the report or the test is missing.
        """
        if not report.exists():
            return Outcome.ERROR
        found = {cls._STATUS.get(method.get("status", ""), Outcome.ERROR) for method in cls._methods(report, test)}
        return next((outcome for outcome in cls._WORST_FIRST if outcome in found), Outcome.ERROR)

    @classmethod
    def _read_seconds(cls, report: Path, tests: Iterable[JavaTest]) -> float:
        """Add up the time TestNG measured for these tests, every run of a test included.

        Returns:
            The seconds, 0 when the report is missing.
        """
        if not report.exists():
            return 0.0
        wanted = set(tests)
        total_ms = 0
        for class_node in ET.parse(report).iter("class"):
            for method in class_node.iter("test-method"):
                test = JavaTest(class_node.get("name", ""), method.get("name", ""))
                if test in wanted and method.get("is-config") != "true":
                    total_ms += int(method.get("duration-ms") or 0)
        return total_ms / 1000
