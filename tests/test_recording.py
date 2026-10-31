from pathlib import Path

from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, Outcome
from lesstests.recording import RecordedTest, Recording


def _record(method: str, outcome: Outcome, statuses: list[int]) -> RecordedTest:
    exchanges = [
        Exchange(
            HttpRequest("GET", f"/v2/pet/{number}", Headers((("Host", "localhost:8081"),)), Body()),
            HttpResponse(status, "", Headers((("Content-Type", "application/json"),)), Body(b"{}")),
        )
        for number, status in enumerate(statuses)
    ]
    return RecordedTest(JavaTest("suite.Pet", method), outcome, 1.25, exchanges)


def test_saved_records_come_back_with_their_logs(tmp_path: Path) -> None:
    recording = Recording(tmp_path)
    first, second = _record("b", Outcome.PASSED, [200, 404]), _record("a", Outcome.FAILED, [])
    recording.save(first)
    recording.save(second)
    assert recording.has(first.test)
    assert [record.test.method for record in recording.records()] == ["a", "b"]
    assert recording.records()[1].exchanges == first.exchanges
    assert sorted(path.name for path in recording.logs_of(first.test).iterdir()) == [
        "1-request.txt",
        "1-response.txt",
        "2-request.txt",
        "2-response.txt",
    ]
    assert list(recording.logs_of(second.test).iterdir()) == []


def test_saving_again_replaces_old_logs(tmp_path: Path) -> None:
    recording = Recording(tmp_path)
    recording.save(_record("a", Outcome.PASSED, [200, 200, 200]))
    recording.save(_record("a", Outcome.PASSED, [200]))
    assert len(list(recording.logs_of(JavaTest("suite.Pet", "a")).iterdir())) == 2


def test_gathered_logs_hold_one_folder_per_test(tmp_path: Path) -> None:
    recording = Recording(tmp_path / "rec")
    tests = [_record("a", Outcome.PASSED, [200]), _record("b", Outcome.PASSED, [201])]
    for record in tests:
        recording.save(record)
    target = recording.gather_logs([tests[1].test], tmp_path / "joined")
    assert [path.name for path in target.iterdir()] == ["suite.Pet.b"]
