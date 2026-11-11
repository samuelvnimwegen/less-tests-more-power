import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lesstests.java import JavaSuite, JavaTest, JvmRunner, JvmSettings, MavenProject, Outcome, Overlay

SOURCE = """package generations.single_agent;

import org.testng.annotations.Test;

public class Pet extends RestAssuredClient {

    @Test
    public void addPet() {
        // @Test public void insideAComment() {}
    }

    /*
    @Test
    public void commentedOut() {}
    */

    @Test(enabled = false)
    public void disabled() {}

    @Test(description = "x")
    @Deprecated
    public void withArgsAndAnotherAnnotation() {}

    public void helper() {}
}
"""

RESULTS = """<?xml version="1.0" encoding="UTF-8"?>
<testng-results skipped="0" failed="1" total="1" passed="0">
  <suite name="Command line suite">
    <test name="Command line test">
      <class name="generations.single_agent.Pet">
        <test-method is-config="true" status="PASS" name="setUp"/>
        <test-method status="FAIL" name="addPet" duration-ms="12"/>
        <test-method status="PASS" name="other" duration-ms="3"/>
      </class>
    </test>
  </suite>
</testng-results>
"""


def test_suite_finds_enabled_test_methods_only(tmp_path: Path) -> None:
    folder = tmp_path / "src" / "test" / "java" / "generations" / "single_agent"
    folder.mkdir(parents=True)
    (folder / "Pet.java").write_text(SOURCE)
    tests = JavaSuite(tmp_path, "generations.single_agent").tests()
    assert [test.id for test in tests] == [
        "generations.single_agent.Pet.addPet",
        "generations.single_agent.Pet.withArgsAndAnotherAnnotation",
    ]


def test_test_id_round_trip() -> None:
    test = JavaTest("generations.single_agent.Pet", "addPet")
    assert JavaTest.from_id(test.id) == test


def test_outcome_comes_from_the_matching_method(tmp_path: Path) -> None:
    report = tmp_path / "testng-results.xml"
    report.write_text(RESULTS)
    assert JvmRunner._read_outcome(report, JavaTest("generations.single_agent.Pet", "addPet")) is Outcome.FAILED
    assert JvmRunner._read_outcome(report, JavaTest("generations.single_agent.Pet", "other")) is Outcome.PASSED
    assert JvmRunner._read_outcome(report, JavaTest("generations.single_agent.Pet", "setUp")) is Outcome.ERROR
    assert JvmRunner._read_outcome(tmp_path / "missing.xml", JavaTest("a.B", "c")) is Outcome.ERROR


def _report_with(tmp_path: Path, *statuses: str) -> Path:
    entries = "".join(f'<test-method status="{status}" name="addPet"/>' for status in statuses)
    report = tmp_path / "testng-results.xml"
    report.write_text(f'<testng-results><class name="s.Pet">{entries}</class></testng-results>')
    return report


@pytest.mark.parametrize(
    ("statuses", "outcome"),
    [
        (("PASS", "FAIL", "PASS"), Outcome.FAILED),
        (("SKIP", "PASS"), Outcome.SKIPPED),
        (("PASS", "PASS"), Outcome.PASSED),
        (("PASS", "WEIRD"), Outcome.ERROR),
    ],
)
def test_a_test_that_ran_more_than_once_gets_its_worst_run(
    tmp_path: Path, statuses: tuple[str, ...], outcome: Outcome
) -> None:
    assert JvmRunner._read_outcome(_report_with(tmp_path, *statuses), JavaTest("s.Pet", "addPet")) is outcome


TRICKY = """package s;

public class Api {
    @Test public void a() { given().accept("*/*"); }
    @Test public void b() { }
    @Test public void c() { given().accept("*/*").get("http://x"); }
    @Test public void e() { char quote = '"'; }
    @Test(description = "Add a pet (valid data)") public void d() { }
    @Test(enabled = false, description = "a)b") public void off() { }
    @Test public/* a comment */void f() { }
}
"""


def test_discovery_ignores_comment_marks_and_brackets_inside_strings(tmp_path: Path) -> None:
    folder = tmp_path / "src" / "test" / "java" / "s"
    folder.mkdir(parents=True)
    (folder / "Api.java").write_text(TRICKY)
    assert [test.method for test in JavaSuite(tmp_path, "s").tests()] == ["a", "b", "c", "d", "e", "f"]


def test_class_level_test_annotation_stops_discovery(tmp_path: Path) -> None:
    folder = tmp_path / "src" / "test" / "java" / "s"
    folder.mkdir(parents=True)
    (folder / "Api.java").write_text("package s;\n\n@Test(enabled = true)\n@Deprecated\npublic class Api {\n}\n")
    with pytest.raises(ValueError, match="on the class"):
        JavaSuite(tmp_path, "s").tests()


def test_prepare_copies_the_whole_project_without_its_build_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    source = tmp_path / "petstore"
    files = ("pom.xml", "src/test/java/A.java", "config.properties", ".mvn/maven.config", "target/junk", ".git/HEAD")
    for path in files:
        (source / path).parent.mkdir(parents=True, exist_ok=True)
        (source / path).write_text("x")
    copy = MavenProject(source, tmp_path / "rec" / "project")
    copy.prepare()
    assert (copy.workdir / "config.properties").exists()
    assert (copy.workdir / ".mvn" / "maven.config").exists()
    assert (copy.workdir / "src" / "test" / "java" / "A.java").exists()
    assert not (copy.workdir / "target").exists()
    assert not (copy.workdir / ".git").exists()


def test_prepare_refuses_a_copy_inside_the_project(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("x")
    with pytest.raises(ValueError, match="inside"):
        MavenProject(tmp_path, tmp_path / "runs" / "project").prepare()
    assert (tmp_path / "pom.xml").exists()


def test_settings_merge_with_the_other_side_winning() -> None:
    first = JvmSettings({"a": "1", "b": "1"}, {"X": "1"}, ("one",))
    second = JvmSettings({"b": "2"}, {"Y": "2"}, ("one", "two"))
    assert first.merged(second) == JvmSettings({"a": "1", "b": "2"}, {"X": "1", "Y": "2"}, ("one", "two"))


def test_settings_turn_into_arguments_and_an_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEEP", "yes")
    settings = JvmSettings({"lesstests.proxy.port": "9"}, {"GOOGLE_CLIENT_ID": "id"}, ("a.B", "c.D"))
    assert settings.java_arguments() == ["-Dlesstests.proxy.port=9"]
    assert settings.testng_arguments() == ["-listener", "a.B,c.D"]
    environment = settings.environment()
    assert environment is not None
    assert environment["KEEP"] == "yes"
    assert environment["GOOGLE_CLIENT_ID"] == "id"
    assert JvmSettings().environment() is None
    assert JvmSettings().testng_arguments() == []


def _project(tmp_path: Path) -> MavenProject:
    project = MavenProject(tmp_path / "source", tmp_path / "copy")
    project.workdir.mkdir(parents=True)
    (project.workdir / "classpath.txt").write_text("dep.jar\n")
    return project


def test_command_puts_properties_before_the_main_class_and_listeners_after(tmp_path: Path) -> None:
    settings = JvmSettings({"k": "v"}, {}, ("lesstests.LesstestsProxyListener",))
    runner = JvmRunner(_project(tmp_path), tmp_path / "out", settings=settings)
    command = runner.command(["-methods", "a.B.c"], tmp_path / "reports")
    assert command[:2] == ["java", "-Dk=v"]
    assert command.index("org.testng.TestNG") < command.index("-listener")
    assert command[-4:] == ["-listener", "lesstests.LesstestsProxyListener", "-methods", "a.B.c"]


SUITE_RESULTS = """<?xml version="1.0" encoding="UTF-8"?>
<testng-results>
  <suite name="run">
    <test name="run">
      <class name="generations.Files">
        <test-method is-config="true" status="PASS" name="setUp" duration-ms="500"/>
        <test-method status="PASS" name="list" duration-ms="250"/>
        <test-method status="FAIL" name="create" duration-ms="750"/>
      </class>
    </test>
  </suite>
</testng-results>
"""


def test_run_suite_runs_every_test_in_one_jvm_from_a_suite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs.get("env")))  # type: ignore[arg-type]
        reports = Path(command[command.index("-d") + 1])
        (reports / "testng-results.xml").write_text(SUITE_RESULTS)
        return subprocess.CompletedProcess(command, 0, "out", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tests = [JavaTest("generations.Files", "list"), JavaTest("generations.Files", "create"), JavaTest("g.Gone", "x")]
    runner = JvmRunner(_project(tmp_path), tmp_path / "out", settings=JvmSettings(env={"T": "1"}))
    run = runner.run_suite(tests, "round-1-original")
    assert len(calls) == 1
    command, env = calls[0]
    assert command[-1] == str(tmp_path / "out" / "round-1-original" / "suite.xml")
    assert env is not None and env["T"] == "1"
    assert run.outcomes == {tests[0]: Outcome.PASSED, tests[1]: Outcome.FAILED, tests[2]: Outcome.ERROR}
    assert run.seconds >= 0
    assert (tmp_path / "out" / "round-1-original" / "console.log").read_text() == "out"


def test_the_console_log_hides_long_env_values_and_bearer_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "1//0refresh-token"
    printed = f"refresh_token={token}&id=short\nHeaders:\t\tAuthorization=Bearer ya29.a0Af-x_1/2+3=\n"
    printed += 'WWW-Authenticate: Bearer realm="x"\n'

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        reports = Path(command[command.index("-d") + 1])
        (reports / "testng-results.xml").write_text(SUITE_RESULTS)
        return subprocess.CompletedProcess(command, 0, printed, f"{token}\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = JvmSettings(env={"GOOGLE_REFRESH_TOKEN": token, "GOOGLE_CLIENT_ID": "short"})
    runner = JvmRunner(_project(tmp_path), tmp_path / "out", settings=settings)
    runner.run(JavaTest("generations.Files", "list"))
    console = (tmp_path / "out" / "generations.Files.list" / "console.log").read_text()

    # A value shorter than 8 characters stays, since hiding it would also hit ordinary words.
    expected = "refresh_token=<redacted>&id=short\nHeaders:\t\tAuthorization=Bearer <redacted>\n"
    assert console == expected + 'WWW-Authenticate: Bearer realm="x"\n<redacted>\n'


def test_run_suite_counts_every_test_as_an_error_when_the_jvm_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", hang)
    tests = [JavaTest("a.B", "c")]
    run = JvmRunner(_project(tmp_path), tmp_path / "out").run_suite(tests, "r", timeout=1)
    assert run.outcomes == {tests[0]: Outcome.ERROR}


def test_suite_xml_lists_each_method_under_its_class() -> None:
    tests = [JavaTest("g.B", "two"), JavaTest("g.A", "one"), JavaTest("g.B", "one")]
    root = ET.fromstring(JvmRunner.suite_xml(tests, "r").split("\n", 2)[2])
    found = {node.get("name"): [include.get("name") for include in node.iter("include")] for node in root.iter("class")}
    assert found == {"g.A": ["one"], "g.B": ["one", "two"]}


def test_overlays_replace_folders_and_add_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    source = tmp_path / "drive"
    (source / "src/test/java/generations").mkdir(parents=True)
    (source / "src/test/java/generations/Multi.java").write_text("multi")
    (source / "pom.xml").write_text("x")
    single = tmp_path / "single-agent"
    single.mkdir()
    (single / "Single.java").write_text("single")
    overlays = (Overlay(single, "src/test/java/generations"), MavenProject.support_overlay())
    project = MavenProject(source, tmp_path / "rec" / "project", overlays)
    project.prepare()
    generations = project.workdir / "src/test/java/generations"
    assert sorted(path.name for path in generations.iterdir()) == ["Single.java"]
    listener = project.workdir / "src/test/java/lesstests/LesstestsProxyListener.java"
    assert "RestAssured.proxy(host, port)" in listener.read_text()


def test_an_overlay_without_a_source_stops_the_copy(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Overlay(tmp_path / "missing", "x").apply(tmp_path)
