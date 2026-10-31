import pytest

from lesstests.coverage import CoverageItem, CoverageSet, Criterion, SuiteCoverage
from lesstests.java import JavaTest
from lesstests.selection import Budget, CoverageMatrix, CoverageSelector, RandomSelector


def _test(name: str) -> JavaTest:
    return JavaTest("suite.Api", name)


def _matrix(reaches: dict[str, set[str]], requests: dict[str, int]) -> CoverageMatrix:
    per_test = {
        _test(name): CoverageSet(CoverageItem(Criterion.STATUS, key) for key in keys) for name, keys in reaches.items()
    }
    full = CoverageSet.union(per_test.values())
    coverage = SuiteCoverage(per_test, full, {})
    return CoverageMatrix(coverage, {_test(name): count for name, count in requests.items()}, list(per_test))


@pytest.mark.parametrize(("share", "tests", "size"), [(0.75, 108, 81), (0.5, 161, 81), (0.25, 161, 40), (0.01, 10, 1)])
def test_budget_rounds_halves_up_and_keeps_one(share: float, tests: int, size: int) -> None:
    assert Budget(share).size(tests) == size


def test_selector_keeps_the_most_items() -> None:
    matrix = _matrix(
        {"wide": {"a", "b", "c"}, "left": {"a", "b"}, "right": {"d", "e"}, "single": {"d"}},
        {"wide": 1, "left": 1, "right": 1, "single": 1},
    )
    assert CoverageSelector().select(matrix, 2) == [_test("right"), _test("wide")]


def test_selector_breaks_coverage_ties_by_fewest_requests() -> None:
    matrix = _matrix({"costly": {"a", "b"}, "cheap": {"a", "b"}, "other": {"c"}}, {"costly": 5, "cheap": 1, "other": 1})
    assert CoverageSelector().select(matrix, 2) == [_test("cheap"), _test("other")]


def test_selector_without_items_keeps_the_cheapest_tests() -> None:
    matrix = _matrix({"a": set(), "b": set(), "c": set()}, {"a": 3, "b": 1, "c": 2})
    assert CoverageSelector().select(matrix, 2) == [_test("b"), _test("c")]


def test_random_draws_repeat_with_the_same_seed_and_differ_between_draws() -> None:
    matrix = _matrix({str(n): {str(n)} for n in range(20)}, {str(n): 1 for n in range(20)})
    budget = Budget(0.5)
    first = RandomSelector(RandomSelector.seed_for(1, budget, 1)).select(matrix, 10)
    again = RandomSelector(RandomSelector.seed_for(1, budget, 1)).select(matrix, 10)
    other = RandomSelector(RandomSelector.seed_for(1, budget, 2)).select(matrix, 10)
    assert first == again
    assert first != other
    assert len(first) == 10
