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
from typing import ClassVar, Protocol, Self


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


class MaskedJava:
    """Java source with every literal and comment blanked out, at the same length, so offsets stay valid."""

    # Literals match before comments, so a "/*" inside a string can't open a comment.
    _LITERALS_AND_COMMENTS = re.compile(
        r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*[\s\S]*?\*/'
    )

    def __init__(self, text: str) -> None:
        """Mask the text once."""
        self.text = text
        self.masked = self._LITERALS_AND_COMMENTS.sub(self._blank, text)

    @staticmethod
    def _blank(match: re.Match[str]) -> str:
        """Keep the quotes of a literal and fill the rest with `x`, so a `(` or `,` inside it can't count.

        Returns:
            The blanked text, as long as the original.
        """
        text = match.group()
        if text.startswith(('"', "'")):
            quote = 3 if text.startswith('"""') else 1
            return text[:quote] + "x" * (len(text) - 2 * quote) + text[-quote:]
        return re.sub(r"[^\n]", " ", text)

    def closing(self, opening: int) -> int:
        """Find the bracket that closes the one at `opening`.

        Returns:
            The offset of the closing bracket.

        Raises:
            ValueError: If it never closes.
        """
        pairs = {"(": ")", "{": "}", "[": "]"}
        stack = []
        for offset in range(opening, len(self.masked)):
            char = self.masked[offset]
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    raise ValueError(f"Unbalanced {char!r} at {offset}")
                if not stack:
                    return offset
        raise ValueError(f"The bracket at {opening} never closes")

    def split_arguments(self, opening: int) -> list[tuple[int, int]]:
        """Split the arguments of the call whose `(` sits at `opening`.

        Returns:
            The start and end offset of every argument, with the spaces around it trimmed.
        """
        end = self.closing(opening)
        spans, start, depth = [], opening + 1, 0
        for offset in range(opening + 1, end):
            char = self.masked[offset]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                spans.append(self._trim(start, offset))
                start = offset + 1
        if self.masked[start:end].strip():
            spans.append(self._trim(start, end))
        return spans

    def _trim(self, start: int, end: int) -> tuple[int, int]:
        while start < end and self.masked[start].isspace():
            start += 1
        while end > start and self.masked[end - 1].isspace():
            end -= 1
        return start, end


class JavaSuite:
    """The TestNG tests in one package of a Maven project, such as `generations.single_agent`."""

    _PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
    TEST_METHOD = re.compile(
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
        source = MaskedJava(path.read_text(encoding="utf-8")).masked
        if self._CLASS_TEST.search(source):
            raise ValueError(f"{path} puts @Test on the class, which lesstests does not support")
        package = self._PACKAGE.search(source)
        class_name = f"{package.group(1)}.{path.stem}" if package else path.stem
        return [
            JavaTest(class_name, match.group("name"))
            for match in self.TEST_METHOD.finditer(source)
            if not self._DISABLED.search(match.group("args") or "")
        ]


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


class CompileError(RuntimeError):
    """Maven could not build the copy of a project."""


class BuiltProject(Protocol):
    """A compiled copy of a project that a test JVM runs from."""

    @property
    def workdir(self) -> Path:
        """The copy."""
        ...

    @property
    def run_folder(self) -> Path:
        """Where the test JVM starts, so relative paths in the tests resolve as they do in the build."""
        ...

    @property
    def classpath(self) -> str:
        """Everything the tests need, compiled tests first."""
        ...

    @property
    def test_roots(self) -> list[Path]:
        """The folders that hold the compiled tests."""
        ...

    @property
    def java(self) -> str:
        """The java binary that runs the tests."""
        ...

    @property
    def jvm_arguments(self) -> list[str]:
        """The flags the project's own build gives a test JVM, such as its heap size."""
        ...

    def prepare(self) -> None:
        """Copy the project, apply the overlays and compile it."""
        ...


class ProjectCopy:
    """Copies a project folder without its build output and puts the overlays in, so the source never changes."""

    def __init__(self, source: Path, workdir: Path, overlays: tuple[Overlay, ...], ignored: tuple[str, ...]) -> None:
        """Set up the copy without making it yet. The overlays go in after the copy, in order."""
        self.source = source
        self.workdir = workdir
        self.overlays = overlays
        self._ignored = ignored

    def make(self) -> None:
        """Copy the project and apply the overlays, replacing an older copy.

        Raises:
            ValueError: If the copy would sit inside the project, or the project inside the copy.
        """
        source, workdir = self.source.resolve(), self.workdir.resolve()
        if workdir.is_relative_to(source) or source.is_relative_to(workdir):
            raise ValueError(f"The copy at {workdir} can't sit inside {source} or hold it. Pick another --out")
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(source, workdir, ignore=shutil.ignore_patterns(*self._ignored))
        for overlay in self.overlays:
            overlay.apply(self.workdir)


class MavenProject:
    """A private copy of a Maven project, compiled once, so the original folder never changes."""

    LISTENER = "lesstests.LesstestsProxyListener"

    def __init__(self, source: Path, workdir: Path, overlays: tuple[Overlay, ...] = ()) -> None:
        """Set up the copy without making it yet. The overlays go in after the copy, in order."""
        self.source = source
        self.workdir = workdir
        self.overlays = overlays

    @property
    def run_folder(self) -> Path:
        """The copy itself, where Maven runs the tests."""
        return self.workdir

    @property
    def test_roots(self) -> list[Path]:
        """Maven's folder of compiled tests."""
        return [self.workdir / "target" / "test-classes"]

    @property
    def java(self) -> str:
        """The java on the PATH, since Maven projects of the study build with it."""
        return "java"

    @property
    def jvm_arguments(self) -> list[str]:
        """None, since Maven's Surefire settings play no part in how the tool runs the tests."""
        return []

    @classmethod
    def support_overlay(cls) -> Overlay:
        """Place the TestNG listener that sends RestAssured through the intercepting proxy.

        Returns:
            The overlay.
        """
        package, name = cls.LISTENER.rsplit(".", 1)
        source = Path(__file__).parent / "support" / f"{name}.java"
        return Overlay(source, str(Path("src", "test", "java", *package.split("."), f"{name}.java")))

    def prepare(self) -> None:
        """Copy the whole project without its build output, apply the overlays, compile and write the classpath.

        Raises:
            CompileError: If Maven fails.
        """
        ProjectCopy(self.source, self.workdir, self.overlays, ("target", ".git")).make()
        command = ["mvn", "-q", "-B", "test-compile", "dependency:build-classpath", "-Dmdep.outputFile=classpath.txt"]
        result = subprocess.run(command, cwd=self.workdir, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            raise CompileError(f"Maven failed in {self.workdir}:\n{result.stdout[-4000:]}{result.stderr[-4000:]}")

    @property
    def classpath(self) -> str:
        """Compiled tests, compiled main code and every dependency, in that order."""
        dependencies = (self.workdir / "classpath.txt").read_text(encoding="utf-8").strip()
        target = self.workdir / "target"
        return ":".join([str(target / "test-classes"), str(target / "classes"), dependencies])


class GradleProject:
    """A private copy of one module of a Gradle project, compiled once by the project's own wrapper.

    An init script from `support/` asks Gradle for the test classpath, the compiled test folders, the java binary
    of the module's toolchain and the flags of its test task. Gradle runs on the `JAVA_HOME` of this process, which
    has to be new enough for the project's build plugins. The helpers are Java files of lesstests. They get compiled
    on their own against the test runtime classpath, so the project's compile checks never see them.
    """

    INIT_SCRIPT = Path(__file__).parent / "support" / "lesstests-classpath.gradle"

    def __init__(
        self,
        source: Path,
        workdir: Path,
        module: str,
        overlays: tuple[Overlay, ...] = (),
        env: dict[str, str] | None = None,
        helpers: tuple[Path, ...] = (),
    ) -> None:
        """Set up the copy without making it yet. `module` is a Gradle path like `:integration-tests`."""
        self.source = source
        self.workdir = workdir
        self.module = module
        self.overlays = overlays
        self._env = env or {}
        self._helpers = helpers

    @property
    def _helper_classes(self) -> Path:
        return self.workdir / "lesstests-classes"

    @staticmethod
    def folder_of(module: str) -> str:
        """Turn a Gradle path into the module's folder, like `:integration-tests` into `integration-tests`.

        Returns:
            The folder, relative to the project.
        """
        return str(Path(*module.strip(":").split(":")))

    @property
    def run_folder(self) -> Path:
        """The module's folder, where Gradle starts the test JVM."""
        return self.workdir / self.folder_of(self.module)

    def prepare(self) -> None:
        """Copy the project without its build output, apply the overlays and compile the module's tests.

        Raises:
            CompileError: If Gradle fails.
        """
        ProjectCopy(self.source, self.workdir, self.overlays, ("build", ".gradle", ".git")).make()
        command = [
            "./gradlew",
            "--no-daemon",
            "--console=plain",
            "-q",
            "--init-script",
            str(self.INIT_SCRIPT),
            f"-Plesstests.module={self.module}",
            f"{self.module}:lesstestsClasspath",
        ]
        result = subprocess.run(
            command,
            cwd=self.workdir,
            capture_output=True,
            text=True,
            errors="replace",
            env={**os.environ, **self._env},
        )
        if result.returncode != 0:
            raise CompileError(f"Gradle failed in {self.workdir}:\n{result.stdout[-4000:]}{result.stderr[-4000:]}")
        if self._helpers:
            self._compile_helpers()

    def _compile_helpers(self) -> None:
        """Compile the helpers with the toolchain's javac.

        Raises:
            CompileError: If javac fails.
        """
        javac = str(Path(self.java).with_name("javac"))
        output = self._helper_classes
        command = [javac, "-d", str(output), "-cp", self._written("lesstests-classpath.txt"), *map(str, self._helpers)]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            output = f"{result.stdout[-4000:]}{result.stderr[-4000:]}"
            raise CompileError(f"javac failed on the lesstests helpers:\n{output}")

    @property
    def classpath(self) -> str:
        """The helpers first, then the module's test runtime classpath as Gradle gave it."""
        gradle = self._written("lesstests-classpath.txt")
        return os.pathsep.join([str(self._helper_classes), gradle]) if self._helpers else gradle

    @property
    def test_roots(self) -> list[Path]:
        """The module's compiled test folders."""
        return [Path(root) for root in self._written("lesstests-test-roots.txt").split(os.pathsep) if root]

    @property
    def java(self) -> str:
        """The java binary of the module's toolchain."""
        return self._written("lesstests-java.txt")

    @property
    def jvm_arguments(self) -> list[str]:
        """The flags of the module's test task, one per line of the file."""
        return [line for line in self._written("lesstests-jvm-args.txt").splitlines() if line]

    def _written(self, name: str) -> str:
        """Read a file the init script wrote, with its paths moved to where the copy sits now.

        Returns:
            The text.
        """
        text = (self.workdir / name).read_text(encoding="utf-8").strip()
        root = self.workdir / "lesstests-root.txt"
        if root.exists():
            text = text.replace(root.read_text(encoding="utf-8").strip(), str(self.workdir.resolve()))
        return text


@dataclass(frozen=True, slots=True)
class JvmSettings:
    """Extra system properties, environment variables and TestNG listeners for a test JVM.

    `env` may hold credentials, so the console logs hide its values. `public_env` holds plain settings, like the
    address Fineract's tests read, which stay readable in the logs.
    """

    SHORTEST_HIDDEN_VALUE: ClassVar[int] = 8
    _BEARER_TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"(?i)(\bbearer\s+)[\w.~+/=-]{8,}")

    properties: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    listeners: tuple[str, ...] = ()
    public_env: dict[str, str] = field(default_factory=dict)

    def merged(self, other: "JvmSettings") -> "JvmSettings":
        """Join two settings. The other one wins where both set the same name.

        Returns:
            The joined settings.
        """
        listeners = self.listeners + tuple(name for name in other.listeners if name not in self.listeners)
        return JvmSettings(
            {**self.properties, **other.properties},
            {**self.env, **other.env},
            listeners,
            {**self.public_env, **other.public_env},
        )

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
        extra = {**self.public_env, **self.env}
        return {**os.environ, **extra} if extra else None

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
    """The outcome of every test of one whole run in a single JVM, with the wall clock of that JVM.

    `finished` is False when the JVM hit the suite timeout and got stopped.
    """

    outcomes: dict[JavaTest, Outcome]
    seconds: float
    finished: bool = True


class Launcher(Protocol):
    """A test framework's side of a test JVM: its main class, its arguments and its report."""

    def one(self, test: JavaTest, reports: Path, settings: JvmSettings) -> list[str]:
        """Build the main class and the arguments that run one test.

        Returns:
            The arguments after the classpath.
        """
        ...

    def many(self, tests: list[JavaTest], name: str, reports: Path, settings: JvmSettings) -> list[str]:
        """Build the main class and the arguments that run these tests together.

        Returns:
            The arguments after the classpath.
        """
        ...

    def outcomes(self, reports: Path, tests: list[JavaTest]) -> dict[JavaTest, Outcome]:
        """Read the outcome of every test from the report.

        Returns:
            The outcomes. A test missing from the report counts as an error.
        """
        ...


class TestNgLauncher:
    """Runs TestNG with `-methods` for one test and with a suite file for many."""

    __test__ = False
    MAIN = "org.testng.TestNG"
    _STATUS = {"PASS": Outcome.PASSED, "FAIL": Outcome.FAILED, "SKIP": Outcome.SKIPPED}
    _WORST_FIRST = (Outcome.FAILED, Outcome.ERROR, Outcome.SKIPPED, Outcome.PASSED)

    def one(self, test: JavaTest, reports: Path, settings: JvmSettings) -> list[str]:
        """Name the test with `-methods`.

        Returns:
            The arguments after the classpath.
        """
        return [self.MAIN, "-d", str(reports), *settings.testng_arguments(), "-methods", test.id]

    def many(self, tests: list[JavaTest], name: str, reports: Path, settings: JvmSettings) -> list[str]:
        """Write a suite file that lists every method, since a suite can list hundreds of them.

        Returns:
            The arguments after the classpath.
        """
        suite_file = reports / "suite.xml"
        suite_file.write_text(self.suite_xml(tests, name), encoding="utf-8")
        return [self.MAIN, "-d", str(reports), *settings.testng_arguments(), str(suite_file)]

    def outcomes(self, reports: Path, tests: list[JavaTest]) -> dict[JavaTest, Outcome]:
        """Read `testng-results.xml`.

        Returns:
            The outcome of every test.
        """
        return {test: self._read_outcome(reports / "testng-results.xml", test) for test in tests}

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


class JUnitLauncher:
    """Runs JUnit 5 tests through `LesstestsJUnit`, which the toolchain compiles next to the copy of the project."""

    MAIN = "lesstests.LesstestsJUnit"
    SOURCE = Path(__file__).parent / "support" / "LesstestsJUnit.java"

    # `LesstestsJUnit` writes TestNG's status words on purpose.
    _STATUS = TestNgLauncher._STATUS

    def one(self, test: JavaTest, reports: Path, settings: JvmSettings) -> list[str]:
        """Run one test the same way as many.

        Returns:
            The arguments after the classpath.
        """
        return self.many([test], test.id, reports, settings)

    def many(self, tests: list[JavaTest], name: str, reports: Path, settings: JvmSettings) -> list[str]:
        """List the tests in a file, one class and method per line.

        Returns:
            The arguments after the classpath.
        """
        selection = reports / "selection.tsv"
        selection.write_text("".join(f"{test.class_name}\t{test.method}\n" for test in tests), encoding="utf-8")
        return [self.MAIN, "run", str(reports), str(selection)]

    def outcomes(self, reports: Path, tests: list[JavaTest]) -> dict[JavaTest, Outcome]:
        """Read `results.tsv`. `LesstestsJUnit` writes `MISSING` for a method it never saw finish.

        Returns:
            The outcome of every test.
        """
        path = reports / "results.tsv"
        found: dict[JavaTest, Outcome] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                class_name, method, status = line.split("\t")
                found[JavaTest(class_name, method)] = self._STATUS.get(status, Outcome.ERROR)
        return {test: found.get(test, Outcome.ERROR) for test in tests}


class SuiteFinder(Protocol):
    """Finds the tests of a suite."""

    def tests(self) -> list[JavaTest]:
        """Find the tests.

        Returns:
            The tests, sorted by class and method.
        """
        ...


class JUnitSuite:
    """The JUnit 5 tests of a compiled project, as the project's own JUnit Platform finds them.

    This catches package private methods, inherited tests, nested classes and parameterized tests. `@Disabled`
    ones stay out. An empty package means every test.
    """

    def __init__(self, project: BuiltProject, package: str, settings: JvmSettings | None = None) -> None:
        """Point at a compiled project and the package whose tests make up the suite."""
        self.project = project
        self.package = package
        self._settings = settings or JvmSettings()

    def tests(self) -> list[JavaTest]:
        """Ask `LesstestsJUnit discover` for every test method.

        Returns:
            The tests in the order JUnit runs them, which keeps the tests of a class together.

        Raises:
            RuntimeError: If the discovery JVM fails.
        """
        out = self.project.workdir / "lesstests-tests.tsv"
        roots = [str(root) for root in self.project.test_roots]
        command = JvmRunner.java_command(
            self.project, self._settings, [JUnitLauncher.MAIN, "discover", str(out), *roots]
        )
        result = subprocess.run(
            command,
            cwd=self.project.run_folder,
            capture_output=True,
            text=True,
            errors="replace",
            env=self._settings.environment(),
        )
        if result.returncode != 0:
            output = self._settings.hide_secrets(f"{result.stdout[-4000:]}{result.stderr[-4000:]}")
            raise RuntimeError(f"JUnit could not list the tests:\n{output}")
        found = [JavaTest(*line.split("\t")) for line in out.read_text(encoding="utf-8").splitlines() if line]
        prefix = f"{self.package}." if self.package else ""
        return [test for test in found if test.class_name.startswith(prefix)]


class JvmRunner:
    """Runs tests in a fresh JVM, from the project's run folder so relative paths in tests resolve."""

    def __init__(
        self,
        project: BuiltProject,
        output: Path,
        timeout: float = 300.0,
        settings: JvmSettings | None = None,
        launcher: Launcher | None = None,
        suite_timeout: float = 1800.0,
    ) -> None:
        """Keep the project, the folder for reports and console logs, the JVM settings and the test framework."""
        self._project = project
        self._output = output
        self._timeout = timeout
        self._settings = settings or JvmSettings()
        self._launcher = launcher or TestNgLauncher()
        self._suite_timeout = suite_timeout

    def run(self, test: JavaTest) -> RunResult:
        """Run one test alone.

        Returns:
            Its outcome. A test missing from the report, or a JVM that hangs, counts as an error.
        """
        reports = self._fresh(self._output / test.id)
        started = time.monotonic()
        if not self._java(self._launcher.one(test, reports, self._settings), reports, self._timeout):
            return RunResult(Outcome.ERROR, time.monotonic() - started)
        return RunResult(self._launcher.outcomes(reports, [test])[test], time.monotonic() - started)

    def run_suite(self, tests: list[JavaTest], name: str) -> SuiteRun:
        """Run all these tests together in one JVM.

        Returns:
            The outcome of every test and the times. Every test counts as an error when the JVM hangs.
        """
        reports = self._fresh(self._output / name)
        arguments = self._launcher.many(tests, name, reports, self._settings)
        started = time.monotonic()
        finished = self._java(arguments, reports, self._suite_timeout)
        seconds = time.monotonic() - started
        if not finished:
            return SuiteRun(dict.fromkeys(tests, Outcome.ERROR), seconds, finished=False)
        return SuiteRun(self._launcher.outcomes(reports, tests), seconds)

    def command(self, arguments: list[str]) -> list[str]:
        """Put the JVM flags and the classpath in front of the launcher's arguments.

        Returns:
            The command.
        """
        return self.java_command(self._project, self._settings, arguments)

    @staticmethod
    def java_command(project: BuiltProject, settings: JvmSettings, arguments: list[str]) -> list[str]:
        """Build a java command: the project's flags, then the extra properties, then the classpath.

        Returns:
            The command.
        """
        flags = [*project.jvm_arguments, *settings.java_arguments()]
        return [project.java, *flags, "-cp", project.classpath, *arguments]

    def _java(self, arguments: list[str], reports: Path, timeout: float) -> bool:
        """Run the JVM and keep its console output next to its reports.

        Returns:
            False when the JVM hung and was stopped.
        """
        try:
            result = subprocess.run(
                self.command(arguments),
                cwd=self._project.run_folder,
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
