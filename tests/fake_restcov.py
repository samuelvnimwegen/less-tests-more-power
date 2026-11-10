from pathlib import Path

from lesstests.coverage import CoverageItem, CoverageSet, Criterion, RestCov, RestCovReport


class FakeReport(RestCovReport):
    """A report that hands back fixed items, with zero documented totals."""

    def __init__(self, items: CoverageSet) -> None:
        super().__init__({})
        self._items = items

    def items(self) -> CoverageSet:
        return self._items

    def totals(self) -> dict[str, int]:
        return {criterion.value: 0 for criterion in Criterion}


class FakeRestCov(RestCov):
    """Stands in for RESTCov. Every request line in the logs becomes one path item."""

    def __init__(self) -> None:
        super().__init__(Path("restcov.py"), Path("spec.json"))
        self.measured: list[Path] = []

    def measure(self, logs: Path, out: Path) -> RestCovReport:
        self.measured.append(logs)
        lines = {path.read_text(encoding="utf-8").splitlines()[0] for path in logs.rglob("*-request.txt")}
        return FakeReport(CoverageSet(CoverageItem(Criterion.PATH, line) for line in lines))
