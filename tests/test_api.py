import json
from pathlib import Path

from lesstests.api import ApiDescription, ValueItem
from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse

DRIVE = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://storage.googleapis.com/"}],
    "components": {"parameters": {"fields": {"in": "query", "name": "fields"}}},
    "paths": {
        "/files": {
            "parameters": [{"$ref": "#/components/parameters/fields"}],
            "get": {"parameters": [{"in": "query", "name": "corpora"}, {"in": "header", "name": "X-Trace"}]},
        },
        "/files/{fileId}": {"get": {"parameters": [{"in": "path", "name": "fileId"}]}},
        "/files/generateIds": {"get": {}},
    },
}

PETSTORE = {
    "swagger": "2.0",
    "host": "petstore.swagger.io",
    "basePath": "/v2",
    "parameters": {"limit": {"in": "query", "name": "limit"}},
    "paths": {
        "/pet/{petId}": {"get": {"parameters": [{"in": "path", "name": "petId"}, {"$ref": "#/parameters/limit"}]}},
    },
}


def _api(tmp_path: Path, document: dict[str, object], base_path: str | None = None) -> ApiDescription:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(document))
    return ApiDescription.load(path, base_path)


def _exchange(target: str, *headers: tuple[str, str]) -> Exchange:
    request = HttpRequest("GET", target, Headers(headers), Body())
    return Exchange(request, HttpResponse(200, "OK", Headers(), Body()))


def test_the_extra_base_path_lets_full_drive_urls_match(tmp_path: Path) -> None:
    api = _api(tmp_path, DRIVE, "/drive/v3")
    assert api.match("GET", "/drive/v3/files?corpora=user").name == "GET /files"  # type: ignore[union-attr]
    assert api.match("GET", "/files").name == "GET /files"  # type: ignore[union-attr]
    assert api.match("POST", "/files") is None
    assert _api(tmp_path, DRIVE).match("GET", "/drive/v3/files") is None


def test_a_template_without_placeholders_wins(tmp_path: Path) -> None:
    api = _api(tmp_path, DRIVE)
    assert api.match("GET", "/files/generateIds").path == "/files/generateIds"  # type: ignore[union-attr]
    assert api.match("GET", "/files/abc").path == "/files/{fileId}"  # type: ignore[union-attr]


def test_values_keep_documented_query_and_header_values_only(tmp_path: Path) -> None:
    api = _api(tmp_path, DRIVE)
    exchange = _exchange("/files?corpora=user&fields=id&pageSize=5&blank=", ("x-trace", "abc"), ("Accept", "*/*"))
    assert api.values([exchange]) == {
        ValueItem("GET /files", "query", "corpora", "user"),
        ValueItem("GET /files", "query", "fields", "id"),
        ValueItem("GET /files", "header", "x-trace", "abc"),
    }


def test_blank_values_count_for_nothing_like_in_restcov(tmp_path: Path) -> None:
    api = _api(tmp_path, DRIVE)
    assert api.values([_exchange("/files?corpora=")]) == frozenset()


def test_swagger_strips_the_base_path_and_follows_no_ref(tmp_path: Path) -> None:
    api = _api(tmp_path, PETSTORE)
    operation = api.match("GET", "/v2/pet/7?limit=3")
    assert operation is not None
    assert operation.name == "GET /pet/{petId}"
    assert api.values([_exchange("/v2/pet/7?limit=3")]) == frozenset()


def test_value_items_survive_json(tmp_path: Path) -> None:
    item = ValueItem("GET /files", "query", "corpora", "user")
    assert ValueItem.from_list(json.loads(json.dumps(item.to_list()))) == item
