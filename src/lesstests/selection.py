import math
import random
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_array, lil_array

from lesstests.coverage import CoverageItem, CoverageSet, SuiteCoverage
from lesstests.java import JavaTest


@dataclass(frozen=True, slots=True)
class Budget:
    """The share of tests a smaller suite keeps, like 0.75 for 75 percent."""

    share: float

    def __post_init__(self) -> None:
        """Check the share.

        Raises:
            ValueError: If the share is not above 0 and at most 1. This also catches nan and inf.
        """
        if not 0 < self.share <= 1:
            raise ValueError(f"A budget keeps more than 0% and at most 100% of the tests, not {self.share * 100}%")

    def size(self, tests: int) -> int:
        """Turn the share into a number of tests, rounding halves up and keeping at least one.

        Returns:
            The number of tests to keep.
        """
        # Float maths can leave an exact half just under .5, like 0.29 * 50 = 14.499999999999998.
        return max(1, math.floor(round(self.share * tests, 9) + 0.5))

    @property
    def label(self) -> str:
        """The share as a percentage, like `75%` or `12.5%`."""
        return f"{self.share * 100:g}%"


class CoverageMatrix:
    """Which tests reach which coverage items, plus the requests each test sends."""

    def __init__(self, coverage: SuiteCoverage, requests: dict[JavaTest, int], tests: list[JavaTest]) -> None:
        """Build the matrix over the given tests. Items no test reaches are left out."""
        self.tests = sorted(tests)
        self.requests = [requests[test] for test in self.tests]
        self.items: list[CoverageItem] = sorted({item for test in self.tests for item in coverage.per_test[test].items})
        index = {item: number for number, item in enumerate(self.items)}
        self.reaches = [sorted(index[item] for item in coverage.per_test[test].items) for test in self.tests]


class Selector(Protocol):
    """Picks a given number of tests from a matrix."""

    name: str

    def select(self, matrix: CoverageMatrix, size: int) -> list[JavaTest]:
        """Pick tests.

        Returns:
            The picked tests.
        """
        ...


class RandomSelector:
    """Picks tests at random. One instance gives one draw, so a study makes one per draw with its own seed."""

    name = "random"

    def __init__(self, seed: int) -> None:
        """Seed the draw."""
        self._random = random.Random(seed)

    def select(self, matrix: CoverageMatrix, size: int) -> list[JavaTest]:
        """Pick tests at random.

        Returns:
            The picked tests, sorted.
        """
        return sorted(self._random.sample(matrix.tests, min(size, len(matrix.tests))))

    @staticmethod
    def seed_for(base: int, budget: Budget, draw: int) -> int:
        """Give every budget and draw its own seed, so adding a budget leaves the other draws unchanged.

        Returns:
            The seed.
        """
        return base * 1_000_003 + round(budget.share * 1000) * 1_009 + draw


class CoverageSelector:
    """The Select step. An integer program keeps the most coverage items, then the fewest requests among those.

    It solves two programs. The first finds the most items any group of that size can reach. The second keeps
    that many items and asks for the fewest requests. A tie left after both is broken by the solver.
    """

    name = "selected"

    def select(self, matrix: CoverageMatrix, size: int) -> list[JavaTest]:
        """Pick the tests.

        Returns:
            The picked tests, sorted.

        Raises:
            RuntimeError: If the solver finds no plan.
        """
        size = min(size, len(matrix.tests))
        if not matrix.items:
            ranked = sorted(zip(matrix.requests, matrix.tests, strict=True))
            return sorted(test for _, test in ranked[:size])
        tests, items = len(matrix.tests), len(matrix.items)
        constraints = self._constraints(matrix, size)
        integrality = np.concatenate([np.ones(tests), np.zeros(items)])
        bounds = Bounds(0, 1)

        most_items = np.concatenate([np.zeros(tests), -np.ones(items)])
        first = milp(most_items, constraints=constraints, integrality=integrality, bounds=bounds)
        if not first.success:
            raise RuntimeError(f"The coverage program failed: {first.message}")
        reachable = round(-first.fun)

        keep_items = LinearConstraint(
            np.concatenate([np.zeros(tests), np.ones(items)]).reshape(1, -1), reachable, np.inf
        )
        fewest_requests = np.concatenate([np.asarray(matrix.requests, dtype=float), np.zeros(items)])
        second = milp(fewest_requests, constraints=[*constraints, keep_items], integrality=integrality, bounds=bounds)
        if not second.success:
            raise RuntimeError(f"The request program failed: {second.message}")
        return sorted(test for test, chosen in zip(matrix.tests, second.x[:tests], strict=True) if chosen > 0.5)

    @staticmethod
    def _constraints(matrix: CoverageMatrix, size: int) -> list[LinearConstraint]:
        """Item i counts only when a picked test reaches it, and exactly `size` tests are picked.

        Returns:
            The two constraint blocks.
        """
        tests, items = len(matrix.tests), len(matrix.items)
        reach = lil_array((items, tests + items))
        for test, reached in enumerate(matrix.reaches):
            for item in reached:
                reach[item, test] = -1
        for item in range(items):
            reach[item, tests + item] = 1
        count = np.concatenate([np.ones(tests), np.zeros(items)]).reshape(1, -1)
        return [LinearConstraint(csr_array(reach), -np.inf, 0), LinearConstraint(count, size, size)]


class SmallestCover:
    """Picks the fewest tests that still reach every item of a target, then the fewest requests among those.

    The first program finds the smallest size. The second keeps that size and asks for the fewest requests.
    """

    def select(
        self, candidates: dict[JavaTest, CoverageSet], requests: dict[JavaTest, int], target: CoverageSet
    ) -> list[JavaTest]:
        """Pick the tests.

        Returns:
            The picked tests, sorted.

        Raises:
            ValueError: If no candidate reaches some item of the target.
            RuntimeError: If the solver finds no plan.
        """
        tests = sorted(candidates)
        items = sorted(target.items)
        if not items:
            return []
        index = {item: number for number, item in enumerate(items)}
        reach = lil_array((len(items), len(tests)))
        for column, test in enumerate(tests):
            for item in candidates[test].items:
                if item in index:
                    reach[index[item], column] = 1
        covered = csr_array(reach)
        unreachable = [items[row] for row in range(len(items)) if covered[[row], :].sum() == 0]
        if unreachable:
            raise ValueError(f"No candidate reaches {len(unreachable)} target items, such as {unreachable[0]}")
        every_item = LinearConstraint(covered, 1, np.inf)
        integrality = np.ones(len(tests))
        bounds = Bounds(0, 1)

        fewest_tests = milp(np.ones(len(tests)), constraints=[every_item], integrality=integrality, bounds=bounds)
        if not fewest_tests.success:
            raise RuntimeError(f"The cover program failed: {fewest_tests.message}")
        size = round(fewest_tests.fun)
        keep_size = LinearConstraint(np.ones((1, len(tests))), size, size)
        cost = np.asarray([requests[test] for test in tests], dtype=float)
        cheapest = milp(cost, constraints=[every_item, keep_size], integrality=integrality, bounds=bounds)
        if not cheapest.success:
            raise RuntimeError(f"The request program failed: {cheapest.message}")
        return sorted(test for test, chosen in zip(tests, cheapest.x, strict=True) if chosen > 0.5)
