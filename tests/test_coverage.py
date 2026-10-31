import os
from pathlib import Path
from typing import Any

import pytest

from lesstests.coverage import CoverageItem, CoverageSet, Criterion, RestCov, RestCovReport, SuiteCoverage
from lesstests.java import JavaTest


def _report(**metrics: int) -> dict[str, Any]:
    """A small coverage.json in RESTCov's shape, with one covered and one untouched operation."""
    defaults = {
        "covered_paths": 1,
        "total_paths": 2,
        "covered_ops": 1,
        "total_ops": 2,
        "param_seen": 1,
        "param_total": 2,
        "req_ct_seen": 1,
        "req_ct_total": 3,
        "resp_ct_seen": 1,
        "resp_ct_total": 2,
        "status_seen": 2,
        "status_total": 4,
        "status_class_seen": 2,
        "status_class_total": 3,
        "total_requests": 3,
    }
    return {
        "metrics": {**defaults, **metrics},
        "seen_paths": ["/pet"],
        "seen_ops": [{"path": "/pet", "method": "POST"}],
        "operations": [
            {
                "path": "/pet",
                "method": "POST",
                "documented_statuses": ["200", "405", "default"],
                "documented_request_content_types": ["application/json", "application/xml", "*/*"],
                "documented_response_content_types": ["application/json"],
                "documented_parameters": [{"name": "body", "in": "body"}],
                "requests": 3,
                "seen_statuses": ["200", "405", "500"],
                "seen_request_content_types": ["application/json", "text/plain"],
                "seen_response_content_types": ["application/json"],
                "seen_parameters": [["body", "body"]],
            },
            {
                "path": "/pet/{petId}",
                "method": "GET",
                "documented_statuses": ["200"],
                "documented_request_content_types": [],
                "documented_response_content_types": ["application/json"],
                "documented_parameters": [{"name": "petId", "in": "path"}],
                "requests": 0,
                "seen_statuses": [],
                "seen_request_content_types": [],
                "seen_response_content_types": [],
                "seen_parameters": [],
            },
        ],
    }


def test_report_items_follow_restcov_counting() -> None:
    report = RestCovReport(_report())
    report.check()
    assert set(report.items().items) == {
        CoverageItem(Criterion.PATH, "/pet"),
        CoverageItem(Criterion.OPERATION, "POST /pet"),
        CoverageItem(Criterion.PARAMETER, "POST /pet body:body"),
        CoverageItem(Criterion.REQUEST_TYPE, "POST /pet application/json"),
        CoverageItem(Criterion.RESPONSE_TYPE, "POST /pet application/json"),
        CoverageItem(Criterion.STATUS, "POST /pet 200"),
        CoverageItem(Criterion.STATUS, "POST /pet 405"),
        CoverageItem(Criterion.STATUS_CLASS, "POST /pet 2xx"),
        CoverageItem(Criterion.STATUS_CLASS, "POST /pet 4xx"),
    }
    assert report.totals()["status"] == 4


def test_check_fails_when_counts_differ() -> None:
    with pytest.raises(ValueError, match="status"):
        RestCovReport(_report(status_seen=3)).check()


def test_coverage_set_counts_and_differences() -> None:
    a = CoverageSet([CoverageItem(Criterion.PATH, "/a"), CoverageItem(Criterion.STATUS, "GET /a 200")])
    b = CoverageSet([CoverageItem(Criterion.PATH, "/a")])
    assert len(a | b) == 2
    assert (a - b).counts()["status"] == 1
    assert CoverageSet.union([a, b]) == a
    assert CoverageSet.from_list(a.to_list()) == a


def test_suite_coverage_survives_a_round_trip(tmp_path: Path) -> None:
    item = CoverageItem(Criterion.OPERATION, "GET /a")
    coverage = SuiteCoverage({JavaTest("a.B", "c"): CoverageSet([item])}, CoverageSet([item]), {"path": 2})
    coverage.save(tmp_path / "coverage.json")
    loaded = SuiteCoverage.load(tmp_path / "coverage.json")
    assert loaded.per_test == coverage.per_test
    assert loaded.full == coverage.full
    assert loaded.of([JavaTest("a.B", "c")]) == coverage.full


@pytest.mark.skipif("LESSTESTS_RESTCOV" not in os.environ, reason="needs RESTCov, set LESSTESTS_RESTCOV")
def test_real_restcov_example_matches_its_own_metrics(tmp_path: Path) -> None:
    script = Path(os.environ["LESSTESTS_RESTCOV"])
    example = script.parent / "examples" / "petstore"
    report = RestCov(script, example / "openapi.json").measure(example / "logs", tmp_path)
    assert report.items().count(Criterion.OPERATION) == report.metrics["covered_ops"]
