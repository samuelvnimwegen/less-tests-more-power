from pathlib import Path

import pytest

from lesstests.java import JavaSuite, JavaTest, JvmRunner, Outcome

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

