import json
from pathlib import Path

from lesstests.api import ApiDescription
from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest
from lesstests.versions import VersionPlan, VersionPlanner

SPEC = {"openapi": "3.0.0", "paths": {"/files": {"get": {"parameters": [{"in": "query", "name": "corpora"}]}}}}


def _record(name: str, target: str) -> RecordedTest:
    request = HttpRequest("GET", target, Headers(), Body())
    exchange = Exchange(request, HttpResponse(200, "OK", Headers(), Body()))
    return RecordedTest(JavaTest("g.Files", name), Outcome.PASSED, 1.0, [exchange])


def _coverage(records: list[RecordedTest], reaches: dict[str, set[str]]) -> SuiteCoverage:
    per_test = {
        record.test: CoverageSet(CoverageItem(Criterion.PARAMETER, key) for key in reaches[record.test.method])
        for record in records
    }
    return SuiteCoverage(per_test, CoverageSet.union(per_test.values()), {})


def _planner(tmp_path: Path) -> VersionPlanner:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC))
    originals = [_record("a", "/files?corpora=user"), _record("b", "/files?corpora=drive"), _record("c", "/files")]
    merged = [_record("lesstestsMerged1", "/files?corpora=user")]
    original_coverage = _coverage(originals, {"a": {"1"}, "b": {"2"}, "c": {"3"}})
    merged_coverage = _coverage(merged, {"lesstestsMerged1": {"1", "2"}})
    return VersionPlanner(ApiDescription.load(spec), (original_coverage, originals), (merged_coverage, merged))


def test_select_merge_may_use_a_merged_copy_and_loses_its_values(tmp_path: Path) -> None:
    plan = _planner(tmp_path).plan()
    original, select, select_merge = plan.versions
    assert [version.name for version in plan.versions] == ["Original", "Select", "Select + Merge"]
    assert len(original.tests) == 3
    assert len(select.tests) == 3
    assert [test.method for test in select_merge.tests] == ["c", "lesstestsMerged1"]
    assert select_merge.coverage == original.coverage
    assert len(original.values) == 2
    assert len(select_merge.values) == 1


def test_the_plan_survives_json_and_reports_what_it_keeps(tmp_path: Path) -> None:
    plan = _planner(tmp_path).plan()
    plan.save(tmp_path / "plan.json")
    loaded = VersionPlan.load(tmp_path / "plan.json")
    assert [version.to_dict() for version in loaded.versions] == [version.to_dict() for version in plan.versions]
    assert "| Select + Merge | 2 | 2 | 3 of 3 | 1 of 2 |" in loaded.markdown()


def test_without_merges_both_smaller_versions_are_the_same(tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC))
    originals = [_record("a", "/files"), _record("b", "/files")]
    coverage = _coverage(originals, {"a": {"1"}, "b": {"1"}})
    plan = VersionPlanner(ApiDescription.load(spec), (coverage, originals)).plan()
    assert plan.versions[1].tests == plan.versions[2].tests
    assert len(plan.versions[1].tests) == 1
