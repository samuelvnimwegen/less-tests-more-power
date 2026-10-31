from pathlib import Path

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
