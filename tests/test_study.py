from pathlib import Path

import pytest
from fake_restcov import FakeRestCov

from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest, Recording
from lesstests.selection import Budget
from lesstests.study import SelectionStudy, SmallerSuite, StudyReport


def _counts(**values: int) -> dict[str, int]:
    return {**{criterion.value: 0 for criterion in Criterion}, "all": 0, **values}


def test_smaller_suite_counts_what_it_loses() -> None:
    full = CoverageSet([CoverageItem(Criterion.PATH, "/a"), CoverageItem(Criterion.STATUS, "GET /a 200")])
    kept = CoverageSet([CoverageItem(Criterion.PATH, "/a")])
    suite = SmallerSuite("selected", Budget(0.5), 0, [JavaTest("s.A", "x")], kept, 3)
    assert suite.lost(full) == _counts(status=1, all=1)
    assert suite.to_dict(full)["kept"] == 1


def test_report_shows_full_selected_and_random_rows() -> None:
    result = {
        "tests": 4,
        "requests": 9,
        "pool": 4,
        "draws": 2,
        "full": _counts(path=2, all=2),
        "versions": [
            {"version": "selected", "budget": 0.5, "kept": 2, "requests": 3, "lost": _counts()},
            {"version": "random", "budget": 0.5, "kept": 2, "requests": 4, "lost": _counts(path=1, all=1)},
            {"version": "random", "budget": 0.5, "kept": 2, "requests": 6, "lost": _counts(path=2, all=2)},
        ],
    }
    lines = StudyReport(result).markdown().splitlines()
    assert "| 100% | 4 | Full suite | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 9 |" in lines
    assert "| 50% | 2 | Selected | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 |" in lines
    assert any(
        line.startswith("| 50% | 2 | Random | 1.5 (1, 2) |") and line.endswith("| 5.0 (4, 6) |") for line in lines
    )


def test_passing_only_sizes_budgets_from_the_pool(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    outcomes = {"p1": Outcome.PASSED, "p2": Outcome.PASSED, "f1": Outcome.FAILED, "f2": Outcome.FAILED}
    for name, outcome in outcomes.items():
        request = HttpRequest("GET", f"/v2/{name}", Headers(), Body())
        exchange = Exchange(request, HttpResponse(200, "OK", Headers(), Body()))
        recording.save(RecordedTest(JavaTest("suite.Api", name), outcome, 1.0, [exchange]))
    restcov = FakeRestCov()
    coverage = SuiteCoverage.measure(recording, restcov, tmp_path / "coverage")
    study = SelectionStudy(recording, coverage, restcov, [Budget(0.5)], draws=2, seed=1, passing_only=True)

    # Half of the 2 passing tests is 1. Sizing from all 4 records would keep the whole pool.
    suites = study.run(tmp_path / "selection")
    assert [len(suite.tests) for suite in suites] == [1, 1, 1]
    assert all(test.method.startswith("p") for suite in suites for test in suite.tests)


def test_budgets_that_share_a_label_are_refused(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    coverage = SuiteCoverage({}, CoverageSet(), {})
    with pytest.raises(ValueError, match="label"):
        SelectionStudy(recording, coverage, FakeRestCov(), [Budget(0.5), Budget(0.5)], draws=1, seed=1)
