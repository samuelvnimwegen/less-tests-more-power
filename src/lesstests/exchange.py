import base64
from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class Headers:
    """HTTP headers in the order they were sent, with repeats kept."""

    pairs: tuple[tuple[str, str], ...] = ()

    def get(self, name: str) -> str | None:
        """Look up the first header with this name, ignoring case.

        Returns:
            The header value, or None when the header is missing.
        """
        wanted = name.lower()
        return next((value for key, value in self.pairs if key.lower() == wanted), None)

    def without(self, *names: str) -> "Headers":
        """Drop every header with one of these names, ignoring case.

        Returns:
            The remaining headers.
        """
        dropped = {name.lower() for name in names}
        return Headers(tuple((key, value) for key, value in self.pairs if key.lower() not in dropped))

    def to_list(self) -> list[list[str]]:
        """Turn the headers into JSON friendly pairs.

        Returns:
            One `[name, value]` list per header.
        """
        return [[key, value] for key, value in self.pairs]

    @classmethod
    def from_list(cls, pairs: list[list[str]]) -> Self:
        """Read headers saved by `to_list`.

        Returns:
            The headers.
        """
        return cls(tuple((key, value) for key, value in pairs))


@dataclass(frozen=True, slots=True)
class Body:
    """A raw HTTP body that survives a round trip through JSON."""

    data: bytes = b""

    def to_dict(self) -> dict[str, str]:
        """Store the body as text when it decodes as UTF-8, otherwise as base64.

        Returns:
            A dict with either a `text` or a `base64` key.
        """
        try:
            return {"text": self.data.decode("utf-8")}
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(self.data).decode("ascii")}

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> Self:
        """Read a body saved by `to_dict`.

        Returns:
            The body.
        """
        if "base64" in raw:
            return cls(base64.b64decode(raw["base64"]))
        return cls(raw.get("text", "").encode("utf-8"))


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One request as the test sent it. The target is the path with its query string."""

    method: str
    target: str
    headers: Headers
    body: Body

    def to_restcov_text(self) -> str:
        """Write the request line and headers in the log format RESTCov reads.

        The body stays out, because RESTCov would read its lines as headers. It only needs the body headers.

        Returns:
            The request as text.
        """
        lines = [f"{self.method} {self.target} HTTP/1.1"]
        lines += [f"{key}: {value}" for key, value in self.headers.pairs]
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        """Turn the request into JSON friendly data.

        Returns:
            The request as a dict.
        """
        return {
            "method": self.method,
            "target": self.target,
            "headers": self.headers.to_list(),
            "body": self.body.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read a request saved by `to_dict`.

        Returns:
            The request.
        """
        return cls(raw["method"], raw["target"], Headers.from_list(raw["headers"]), Body.from_dict(raw["body"]))


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One answer as the service sent it."""

    status: int
    reason: str
    headers: Headers
    body: Body

    def to_restcov_text(self) -> str:
        """Write the status line and headers in the log format RESTCov reads.

        Returns:
            The response as text, without its body.
        """
        lines = [f"HTTP/1.1 {self.status} {self.reason}".rstrip()]
        lines += [f"{key}: {value}" for key, value in self.headers.pairs]
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        """Turn the response into JSON friendly data.

        Returns:
            The response as a dict.
        """
        return {
            "status": self.status,
            "reason": self.reason,
            "headers": self.headers.to_list(),
            "body": self.body.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read a response saved by `to_dict`.

        Returns:
            The response.
        """
        return cls(raw["status"], raw["reason"], Headers.from_list(raw["headers"]), Body.from_dict(raw["body"]))


@dataclass(frozen=True, slots=True)
class Exchange:
    """A request and the answer it got."""

    request: HttpRequest
    response: HttpResponse

    def to_dict(self) -> dict[str, Any]:
        """Turn the exchange into JSON friendly data.

        Returns:
            The exchange as a dict.
        """
        return {"request": self.request.to_dict(), "response": self.response.to_dict()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read an exchange saved by `to_dict`.

        Returns:
            The exchange.
        """
        return cls(HttpRequest.from_dict(raw["request"]), HttpResponse.from_dict(raw["response"]))
