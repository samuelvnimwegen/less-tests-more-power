import os
import stat
from pathlib import Path

import pytest

from lesstests.java import (
    CompileError,
    GradleProject,
    JavaTest,
    JUnitLauncher,
    JUnitSuite,
    JvmRunner,
    JvmSettings,
    Outcome,
    Overlay,
)

# A stand in for a Gradle wrapper. It writes what the init script would, plus its arguments and one env value.
GRADLEW = """#!/bin/sh
echo "$@" > lesstests-gradle-args.txt
echo "$VERSIONING_DISABLE" > lesstests-gradle-env.txt
printf 'a.jar:b.jar' > lesstests-classpath.txt
printf '{roots}' > lesstests-test-roots.txt
printf '{java}' > lesstests-java.txt
printf -- '-Xmx4g\\n-Duser.language=en\\n' > lesstests-jvm-args.txt
"""


def _script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _gradle_source(tmp_path: Path, javac: str = "#!/bin/sh\nexit 0\n") -> Path:
    source = tmp_path / "fineract"
    bin_folder = tmp_path / "jdk" / "bin"
    _script(bin_folder / "javac", javac)
    roots = f"{tmp_path}/copy/integration-tests/build/classes/java/test"
    _script(source / "gradlew", GRADLEW.format(roots=roots, java=bin_folder / "java"))
    (source / "integration-tests" / "build" / "old").mkdir(parents=True)
    (source / "integration-tests" / "src").mkdir(parents=True)
    (source / ".gradle").mkdir()
    return source


def test_a_gradle_copy_leaves_build_output_out_and_reads_what_the_init_script_wrote(tmp_path: Path) -> None:
    source = _gradle_source(tmp_path)
    extra = tmp_path / "Extra.java"
    extra.write_text("class Extra {}")
    overlay = Overlay(extra, "integration-tests/src/Extra.java")
    project = GradleProject(source, tmp_path / "copy", ":integration-tests", (overlay,), {"VERSIONING_DISABLE": "true"})
    project.prepare()

    assert not (project.workdir / "integration-tests" / "build").exists()
    assert not (project.workdir / ".gradle").exists()
    assert (project.workdir / "integration-tests" / "src" / "Extra.java").read_text() == "class Extra {}"
    arguments = (project.workdir / "lesstests-gradle-args.txt").read_text().split()
    assert arguments[-1] == ":integration-tests:lesstestsClasspath"
    assert f"--init-script {GradleProject.INIT_SCRIPT}" in " ".join(arguments)
    assert (project.workdir / "lesstests-gradle-env.txt").read_text().strip() == "true"
    assert project.run_folder == project.workdir / "integration-tests"
    assert project.classpath == "a.jar:b.jar"
    assert project.java == str(tmp_path / "jdk" / "bin" / "java")
    assert project.jvm_arguments == ["-Xmx4g", "-Duser.language=en"]
    assert project.test_roots == [tmp_path / "copy" / "integration-tests" / "build" / "classes" / "java" / "test"]


def test_helpers_get_compiled_with_the_toolchains_javac_and_go_first_on_the_classpath(tmp_path: Path) -> None:
    javac = '#!/bin/sh\necho "$@" > "$(dirname "$0")/javac-args.txt"\n'
    source = _gradle_source(tmp_path, javac)
    helper = tmp_path / "LesstestsJUnit.java"
    helper.write_text("class LesstestsJUnit {}")
    project = GradleProject(source, tmp_path / "copy", ":integration-tests", helpers=(helper,))
    project.prepare()

    arguments = (tmp_path / "jdk" / "bin" / "javac-args.txt").read_text().split()
    assert arguments == ["-d", str(project.workdir / "lesstests-classes"), "-cp", "a.jar:b.jar", str(helper)]
    assert project.classpath == f"{project.workdir / 'lesstests-classes'}{os.pathsep}a.jar:b.jar"


def test_a_helper_that_does_not_compile_stops_the_build(tmp_path: Path) -> None:
    source = _gradle_source(tmp_path, "#!/bin/sh\necho 'error: no launcher' >&2\nexit 1\n")
    project = GradleProject(source, tmp_path / "copy", ":integration-tests", helpers=(tmp_path / "X.java",))
    with pytest.raises(CompileError, match="no launcher"):
        project.prepare()


def test_a_failing_gradle_build_stops_with_its_output(tmp_path: Path) -> None:
    source = tmp_path / "fineract"
    _script(source / "gradlew", "#!/bin/sh\necho 'class file version 69.0' >&2\nexit 1\n")
    with pytest.raises(CompileError, match="class file version 69.0"):
        GradleProject(source, tmp_path / "copy", ":integration-tests").prepare()


def test_a_nested_gradle_module_runs_from_its_own_folder(tmp_path: Path) -> None:
    assert GradleProject.folder_of(":a:b") == str(Path("a", "b"))
    assert GradleProject(tmp_path, tmp_path / "copy", ":a:b").run_folder == tmp_path / "copy" / "a" / "b"


class BuiltFake:
    def __init__(self, folder: Path, java: str = "java") -> None:
        self.workdir = folder
        self.run_folder = folder / "module"
        self.classpath = "cp"
        self.test_roots = [folder / "roots"]
        self.java = java
        self.jvm_arguments = ["-Xmx4g"]

    def prepare(self) -> None:
        pass


def test_junit_selection_lists_class_and_method_and_reads_every_outcome(tmp_path: Path) -> None:
    launcher = JUnitLauncher()
    tests = [JavaTest("a.B", "one"), JavaTest("a.B$Inner", "two"), JavaTest("a.C", "three"), JavaTest("a.C", "four")]
    arguments = launcher.many(tests, "round-1", tmp_path, JvmSettings())
    assert arguments == ["lesstests.LesstestsJUnit", "run", str(tmp_path), str(tmp_path / "selection.tsv")]
    assert (tmp_path / "selection.tsv").read_text() == "a.B\tone\na.B$Inner\ttwo\na.C\tthree\na.C\tfour\n"

    # The helper writes MISSING for a method it never saw finish. The last method has no line at all.
    (tmp_path / "results.tsv").write_text("a.B\tone\tPASS\na.B$Inner\ttwo\tFAIL\na.C\tthree\tMISSING\n")
    assert launcher.outcomes(tmp_path, tests) == {
        tests[0]: Outcome.PASSED,
        tests[1]: Outcome.FAILED,
        tests[2]: Outcome.ERROR,
        tests[3]: Outcome.ERROR,
    }


def test_junit_outcomes_without_a_report_are_errors(tmp_path: Path) -> None:
    assert JUnitLauncher().outcomes(tmp_path, [JavaTest("a.B", "c")]) == {JavaTest("a.B", "c"): Outcome.ERROR}


def test_a_junit_test_runs_on_the_projects_java_with_its_flags(tmp_path: Path) -> None:
    java = '#!/bin/sh\necho "$@" > "$(dirname "$0")/java-args.txt"\nfor last; do :; done\n'
    java += 'printf "a.B\\tc\\tSKIP\\n" > "$(dirname "$last")/results.tsv"\npwd > "$(dirname "$0")/pwd.txt"\n'
    project = BuiltFake(tmp_path, str(_script(tmp_path / "jdk" / "java", java)))
    project.run_folder.mkdir()
    settings = JvmSettings({"k": "v"})
    runner = JvmRunner(project, tmp_path / "out", settings=settings, launcher=JUnitLauncher())
    result = runner.run(JavaTest("a.B", "c"))

    assert result.outcome is Outcome.SKIPPED
    arguments = (tmp_path / "jdk" / "java-args.txt").read_text().split()
    assert arguments[:4] == ["-Xmx4g", "-Dk=v", "-cp", "cp"]
    assert arguments[4:6] == ["lesstests.LesstestsJUnit", "run"]
    assert (tmp_path / "jdk" / "pwd.txt").read_text().strip() == str(project.run_folder)


def test_junit_discovery_keeps_the_suites_package_only(tmp_path: Path) -> None:
    java = '#!/bin/sh\nprintf "a.b.T\\tx\\na.bc.U\\ty\\na.b.inner.V\\tz\\n" > "$6"\n'
    project = BuiltFake(tmp_path, str(_script(tmp_path / "java", java)))
    project.run_folder.mkdir()
    tests = JUnitSuite(project, "a.b").tests()
    assert tests == [JavaTest("a.b.T", "x"), JavaTest("a.b.inner.V", "z")]
    assert len(JUnitSuite(project, "").tests()) == 3


def test_failed_junit_discovery_stops_with_its_output(tmp_path: Path) -> None:
    project = BuiltFake(tmp_path, str(_script(tmp_path / "java", "#!/bin/sh\necho 'NoClassDefFound' >&2\nexit 1\n")))
    project.run_folder.mkdir()
    with pytest.raises(RuntimeError, match="NoClassDefFound"):
        JUnitSuite(project, "a").tests()


def test_the_init_script_names_the_files_gradle_project_reads() -> None:
    script = GradleProject.INIT_SCRIPT.read_text()
    for name in ("lesstests-classpath.txt", "lesstests-test-roots.txt", "lesstests-java.txt", "lesstests-jvm-args.txt"):
        assert name in script

    # Without the classpath as a dependency, Gradle never builds the jars of the other modules.
    assert "dependsOn 'testClasses', classpath" in script


def test_a_moved_gradle_copy_still_finds_its_own_files(tmp_path: Path) -> None:
    project = GradleProject(tmp_path / "source", tmp_path / "old", ":integration-tests")
    project.workdir.mkdir()
    old = str(project.workdir.resolve())
    (project.workdir / "lesstests-root.txt").write_text(old)
    (project.workdir / "lesstests-classpath.txt").write_text(f"{old}/integration-tests/build/classes:/m2/x.jar")
    (project.workdir / "lesstests-test-roots.txt").write_text(f"{old}/integration-tests/build/classes")
    project.workdir.rename(tmp_path / "new")

    moved = GradleProject(tmp_path / "source", tmp_path / "new", ":integration-tests")
    new = (tmp_path / "new").resolve()
    assert moved.classpath == f"{new}/integration-tests/build/classes:/m2/x.jar"
    assert moved.test_roots == [new / "integration-tests" / "build" / "classes"]


def test_junit_discovery_keeps_the_order_junit_runs_the_tests_in(tmp_path: Path) -> None:
    java = '#!/bin/sh\nprintf "a.Z\\tsecond\\na.Z\\tfirst\\na.A\\tx\\n" > "$6"\n'
    project = BuiltFake(tmp_path, str(_script(tmp_path / "java", java)))
    project.run_folder.mkdir()
    assert [test.method for test in JUnitSuite(project, "a").tests()] == ["second", "first", "x"]
