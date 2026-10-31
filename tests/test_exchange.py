from lesstests.exchange import Body, Exchange, Headers, HttpRequest, HttpResponse


def test_headers_lookup_ignores_case_and_keeps_repeats() -> None:
    headers = Headers((("Content-Type", "application/json"), ("Set-Cookie", "a=1"), ("set-cookie", "b=2")))
    assert headers.get("content-type") == "application/json"
    assert headers.get("missing") is None
    assert headers.without("SET-COOKIE").pairs == (("Content-Type", "application/json"),)


def test_body_keeps_text_as_text_and_bytes_as_base64() -> None:
    assert Body(b'{"a": 1}').to_dict() == {"text": '{"a": 1}'}
    binary = Body(b"\xff\x00\xfe")
    assert "base64" in binary.to_dict()
    assert Body.from_dict(binary.to_dict()) == binary


def test_restcov_text_leaves_the_body_out() -> None:
    request = HttpRequest(
        "POST",
        "/v2/pet?x=1",
        Headers((("Content-Type", "application/json"), ("Host", "localhost:8081"))),
        Body(b'{"a": 1}'),
    )
    assert (
        request.to_restcov_text() == "POST /v2/pet?x=1 HTTP/1.1\nContent-Type: application/json\nHost: localhost:8081\n"
    )
    response = HttpResponse(404, "Not Found", Headers((("Content-Type", "application/json"),)), Body(b"{}"))
    assert response.to_restcov_text() == "HTTP/1.1 404 Not Found\nContent-Type: application/json\n"


def test_exchange_survives_a_round_trip() -> None:
    exchange = Exchange(
        HttpRequest("GET", "/v2/pet/1", Headers((("Accept", "*/*"),)), Body()),
        HttpResponse(200, "OK", Headers((("Content-Type", "application/json"),)), Body(b'{"id": 1}')),
    )
    assert Exchange.from_dict(exchange.to_dict()) == exchange
