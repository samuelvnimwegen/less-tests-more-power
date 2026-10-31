from lesstests.coverage import CoverageItem, CoverageSet, Criterion
from lesstests.java import JavaTest
from lesstests.selection import Budget
from lesstests.study import SmallerSuite, StudyReport


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
