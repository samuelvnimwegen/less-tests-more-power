import json
from pathlib import Path

import pytest

from lesstests.api import ApiDescription
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse
from lesstests.java import JavaTest, Outcome
from lesstests.merge import JavaTestFile, MaskedJava, MergedTest, MergePlanner, Merger
from lesstests.recording import RecordedTest

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://storage.googleapis.com/"}],
    "paths": {
        "/files": {
            "get": {"parameters": [{"in": "query", "name": name} for name in ("corpora", "orderBy", "q", "pageToken")]},
            "post": {},
        },
    },
}

SOURCE = """package generations;

import java.util.Map;

public class Files extends RestAssuredClient {

    @Test
    public void listByUser() {
        Response resp = get("https://www.googleapis.com/drive/v3/files",
                Map.of("corpora", "user"),
                Map.of("Accept", "application/json"), true);
        Assert.assertEquals(resp.statusCode(), 200, "a, b (c");
    }

    @Test
    public void sortByTime() {
        Response resp = get("/files", Map.of("corpora", "user", "orderBy", "modifiedTime"), null, true);
        Assert.assertEquals(resp.statusCode(), 200);
    }

    @Test
    public void starred() {
        // A comment with get( in it.
        Map<String, Object> params = Map.of("q", "starred = true");
        Response resp = get("/files", params, null, true);
        Assert.assertEquals(resp.statusCode(), 200);
    }

    @Test
    public void withVariable() {
        String token = "x";
        Response resp = get("/files", Map.of("pageToken", token), null, true);
        Assert.assertEquals(resp.statusCode(), 200);
    }

    @Test
    public void twoCalls() {
        get("/files", null, null, true);
        get("/files", null, null, true);
    }

    @Test
    public void createFile() {
        Response resp = post("/files", null, Map.of("Content-Type", "text/plain"), "x", true);
        Assert.assertEquals(resp.statusCode(), 400);
    }
}
"""

TARGETS = {
    "listByUser": ("GET", "/drive/v3/files?corpora=user", 200),
    "sortByTime": ("GET", "/files?corpora=user&orderBy=modifiedTime", 200),
    "starred": ("GET", "/files?q=starred+%3D+true", 200),
    "withVariable": ("GET", "/files?pageToken=x", 200),
    "createFile": ("POST", "/files", 400),
}


def _api(tmp_path: Path, spec: dict[str, object] = SPEC) -> ApiDescription:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return ApiDescription.load(path, "/drive/v3")


def _record(test: JavaTest, method: str, target: str, status: int) -> RecordedTest:
    request = HttpRequest(method, target, Headers((("Host", "www.googleapis.com"),)), Body())
    response = HttpResponse(status, "", Headers((("Content-Type", "application/json; charset=UTF-8"),)), Body())
    return RecordedTest(test, Outcome.PASSED, 1.0, [Exchange(request, response)])


def _planner(tmp_path: Path, source: str = SOURCE, targets: dict[str, tuple[str, str, int]] = TARGETS) -> MergePlanner:
    root = tmp_path / "java"
    (root / "generations").mkdir(parents=True)
    (root / "generations" / "Files.java").write_text(source)
    records = [_record(JavaTest("generations.Files", name), *target) for name, target in targets.items()]
    return MergePlanner(records, _api(tmp_path), root)


def test_masking_keeps_every_offset() -> None:
    text = "call(\"a, (b\", 'c', /* d, ( */ x) // e, ("
    masked = MaskedJava(text)
    assert len(masked.masked) == len(text)
    assert masked.split_arguments(4) == [(5, 12), (14, 17), (30, 31)]


def test_a_method_is_editable_only_with_one_call_and_literal_maps(tmp_path: Path) -> None:
    root = tmp_path / "java"
    root.mkdir()
    (root / "Files.java").write_text(SOURCE)
    file = JavaTestFile(root / "Files.java")
    assert file.method("listByUser").params.names() == {"corpora"}  # type: ignore[union-attr]
    assert file.method("starred").params.names() == {"q"}  # type: ignore[union-attr]
    assert file.method("sortByTime").headers.pairs == ()  # type: ignore[union-attr]
    assert file.method("twoCalls") is None
    assert file.method("missing") is None


def test_the_first_round_merges_into_the_test_with_the_most_names(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    merged = planner.first_round()
    assert len(merged) == 1
    merge = merged[0]
    assert merge.base.method == "sortByTime"
    assert merge.test == JavaTest("generations.Files", "lesstestsMerged1")
    assert merge.key.status == 200
    assert merge.key.response_type == "application/json"

    # `withVariable` sends its value through a local variable, which would not compile in another method.
    assert [pair.name for pair in merge.query] == ["q"]
    assert {donor.method for donor in merge.donors} == {"listByUser", "starred", "withVariable"}


def test_the_merged_copy_lands_at_the_end_of_the_base_class(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    edited = planner.edited_files(planner.first_round())
    text = edited[Path("generations", "Files.java")]
    assert text.count("public void sortByTime()") == 1
    assert "public void lesstestsMerged1()" in text
    expected = (
        'get("/files", java.util.Map.ofEntries(java.util.Map.entry("corpora", "user"), '
        'java.util.Map.entry("orderBy", "modifiedTime"), java.util.Map.entry("q", "starred = true")), null, true)'
    )
    assert expected in text
    assert text.rstrip().endswith("}")
    assert text.index("lesstestsMerged1") > text.index("createFile")


def test_a_name_the_base_map_holds_is_never_added_twice(tmp_path: Path) -> None:
    source = """package generations;

public class Files {
    @Test
    public void base() {
        get("/files", Map.of("corpora", "user", "orderBy", "name", "q", "x"), null, true);
    }

    @Test
    public void donor() {
        get("/files", Map.of("corpora", "drive"), null, true);
    }
}
"""
    targets = {"base": ("GET", "/files?orderBy=name&q=x", 200), "donor": ("GET", "/files?corpora=drive", 200)}

    # The base never sent corpora, but its map holds it, so `Map.ofEntries` would throw on a second one.
    assert _planner(tmp_path, source, targets).first_round() == []


class FakeRunner:
    """Answers 400 for every merge whose donors include a test named in `breaks`."""

    def __init__(self, breaks: set[str]) -> None:
        self.breaks = breaks
        self.rounds: list[list[str]] = []

    def record(self, merged: list[MergedTest], edited: dict[Path, str], folder: Path) -> dict[JavaTest, RecordedTest]:
        self.rounds.append([merge.test.method for merge in merged])
        found = {}
        for merge in merged:
            status = 400 if any(donor.method in self.breaks for donor in merge.donors) else 200
            found[merge.test] = _record(merge.test, "GET", "/files?corpora=user", status)
        return found


def _wide_source(donors: list[str]) -> tuple[str, dict[str, tuple[str, str, int]]]:
    methods = []
    targets = {}
    for number, name in enumerate(["base", *donors]):
        pairs = '"corpora", "user", "orderBy", "name"' if name == "base" else f'"p{number}", "v"'
        methods.append(
            f'    @Test\n    public void {name}() {{\n        get("/files", Map.of({pairs}), null, true);\n    }}\n'
        )
        query = "corpora=user&orderBy=name" if name == "base" else f"p{number}=v"
        targets[name] = ("GET", f"/files?{query}", 200)
    return "package generations;\n\npublic class Files {\n" + "\n".join(methods) + "}\n", targets


def test_a_merge_that_changes_the_status_gets_split_until_the_bad_donor_is_alone(tmp_path: Path) -> None:
    donors = ["d1", "d2", "d3", "bad"]
    source, targets = _wide_source(donors)
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/files": {
                "get": {
                    "parameters": [
                        {"in": "query", "name": name} for name in ("corpora", "orderBy", "p1", "p2", "p3", "p4")
                    ]
                }
            }
        },
    }
    root = tmp_path / "java"
    (root / "generations").mkdir(parents=True)
    (root / "generations" / "Files.java").write_text(source)
    records = [_record(JavaTest("generations.Files", name), *target) for name, target in targets.items()]
    api = _api(tmp_path, spec)
    runner = FakeRunner({"bad"})
    outcome = Merger(MergePlanner(records, api, root), runner, api, rounds=4).run(tmp_path / "rounds")

    assert [len(names) for names in runner.rounds] == [1, 2, 2]
    kept = sorted(sorted(donor.method for donor in merge.donors) for merge in outcome.accepted)
    assert kept == [["d1", "d2"], ["d3"]]
    reasons = [attempt["reason"] for attempt in outcome.attempts if not attempt["kept"]]
    assert reasons == ["got 400 instead of 200"] * 3
    assert len({merge.test for merge in outcome.accepted}) == 2


class ShapedRunner:
    """Gives every merged copy the same exchanges, or no record at all when `targets` is None."""

    def __init__(self, targets: list[tuple[str, str, int]] | None) -> None:
        self.targets = targets

    def record(self, merged: list[MergedTest], edited: dict[Path, str], folder: Path) -> dict[JavaTest, RecordedTest]:
        if self.targets is None:
            return {}
        found = {}
        for merge in merged:
            exchanges = [exchange for target in self.targets for exchange in _record(merge.test, *target).exchanges]
            found[merge.test] = RecordedTest(merge.test, Outcome.PASSED, 1.0, exchanges)
        return found


@pytest.mark.parametrize(
    ("targets", "reason"),
    [
        (None, "not recorded"),
        ([("GET", "/files?corpora=user", 200), ("GET", "/files", 200)], "sent 2 requests"),
        ([("POST", "/files", 200)], "reached another operation"),
    ],
)
def test_a_merged_copy_that_is_not_one_request_to_its_operation_is_dropped(
    tmp_path: Path, targets: list[tuple[str, str, int]] | None, reason: str
) -> None:
    api = _api(tmp_path)
    outcome = Merger(_planner(tmp_path), ShapedRunner(targets), api, rounds=1).run(tmp_path / "rounds")
    assert outcome.accepted == []
    assert outcome.attempts
    assert {attempt["reason"] for attempt in outcome.attempts} == {reason}


def test_a_merge_round_trips_through_json(tmp_path: Path) -> None:
    merge = _planner(tmp_path).first_round()[0]
    assert MergedTest.from_dict(json.loads(json.dumps(merge.to_dict()))) == merge


@pytest.mark.parametrize("value", ['"text"', "5", "-2.5", "true", "10L", "'c'"])
def test_plain_literals_are_portable(value: str) -> None:
    from lesstests.merge import MapPair

    assert MapPair('"k"', value).portable
    assert not MapPair('"k"', "token").portable
    assert not MapPair("KEY", value).portable


def test_a_name_the_base_sends_in_its_path_string_is_not_added_again(tmp_path: Path) -> None:
    source = """package generations;

public class Files {
    @Test
    public void base() {
        get("/files?corpora=user", Map.of(), null, true);
    }

    @Test
    public void donor() {
        get("/files", Map.of("corpora", "drive"), null, true);
    }
}
"""

    # Found on PetStore, where a merged copy sent `status=available&status=available`.
    targets = {"base": ("GET", "/files?corpora=user", 200), "donor": ("GET", "/files?corpora=drive", 200)}
    assert _planner(tmp_path, source, targets).first_round() == []
