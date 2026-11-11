"""Reads an API description and matches requests onto its operations the way RESTCov does."""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qsl, urlsplit

import yaml

from lesstests.exchange import Exchange

_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


@dataclass(frozen=True, slots=True)
class Parameter:
    """One documented parameter, like the query parameter `corpora`."""

    location: str
    name: str


@dataclass(frozen=True, slots=True)
class Operation:
    """One documented operation, like `GET /files`."""

    method: str
    path: str
    parameters: frozenset[Parameter]

    @property
    def name(self) -> str:
        """The name RESTCov's items use, like `GET /files`."""
        return f"{self.method} {self.path}"

    @property
    def placeholders(self) -> int:
        """How many path parameters the template holds. RESTCov prefers the template with the fewest."""
        return self.path.count("{")

    def matches(self, method: str, path: str) -> bool:
        """Check a request against the template, with RESTCov's pattern.

        Returns:
            True when the method is the same and the path fits the template.
        """
        pattern = "^" + re.sub(r"\{[^/]+\}", "[^/]+", self.path) + "$"
        return method.upper() == self.method and re.match(pattern, path) is not None

    def documents(self, location: str, name: str) -> bool:
        """Check whether this operation documents a parameter. Header names ignore case.

        Returns:
            True when the parameter is documented here.
        """
        if location == "header":
            return any(p.location == "header" and p.name.lower() == name.lower() for p in self.parameters)
        return Parameter(location, name) in self.parameters


@dataclass(frozen=True, slots=True, order=True)
class ValueItem:
    """One value a request sent for a documented query or header parameter, like `corpora=user` on `GET /files`.

    Path values stay out, because they are ids that change from run to run on a live API.
    """

    operation: str
    location: str
    name: str
    value: str

    def to_list(self) -> list[str]:
        """Turn the item into a JSON friendly list.

        Returns:
            The four fields.
        """
        return [self.operation, self.location, self.name, self.value]

    @classmethod
    def from_list(cls, raw: list[str]) -> Self:
        """Read an item saved by `to_list`.

        Returns:
            The item.
        """
        return cls(*raw)


class ApiDescription:
    """The operations of an OpenAPI 3 or Swagger 2 document, with the server prefixes RESTCov strips."""

    def __init__(self, operations: list[Operation], prefixes: list[str]) -> None:
        """Keep the operations and the path prefixes to strip, in the order RESTCov tries them."""
        self.operations = operations
        self._prefixes = [prefix.rstrip("/") for prefix in prefixes if prefix.rstrip("/")]

    @classmethod
    def load(cls, path: Path, base_path: str | None = None) -> Self:
        """Read a JSON or YAML document. `base_path` is the extra prefix RESTCov gets with `--base-path`.

        Returns:
            The description.
        """
        text = path.read_text(encoding="utf-8")
        document = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        reader = _DocumentReader(document)
        prefixes = [*reader.server_paths(), *([base_path] if base_path else [])]
        return cls(reader.operations(), prefixes)

    def path_of(self, target: str) -> str:
        """Strip the scheme, host, query and server prefixes the way RESTCov does.

        RESTCov tries every prefix in turn and strips each one that matches, so the same happens here.

        Returns:
            The path to match against the templates.
        """
        path = urlsplit(target).path or "/"
        for prefix in self._prefixes:
            if path.startswith(prefix):
                path = path[len(prefix) :] or "/"
        return path if path.startswith("/") else "/" + path

    def match(self, method: str, target: str) -> Operation | None:
        """Find the operation of a request. A template without placeholders wins, then the one with the fewest.

        Returns:
            The operation, or None when nothing matches.
        """
        path = self.path_of(target)
        candidates = [operation for operation in self.operations if operation.matches(method, path)]
        if not candidates:
            return None
        return min(candidates, key=lambda operation: operation.placeholders)

    def values(self, exchanges: Iterable[Exchange]) -> frozenset[ValueItem]:
        """Collect the value items of some exchanges.

        Returns:
            Every documented query and header value the requests sent.
        """
        found: set[ValueItem] = set()
        for exchange in exchanges:
            request = exchange.request
            operation = self.match(request.method, request.target)
            if operation is None:
                continue

            # RESTCov reads the query with `parse_qs`, which drops blank values, so they count for nothing here too.
            query = parse_qsl(urlsplit(request.target).query)
            found |= {
                ValueItem(operation.name, "query", name, value)
                for name, value in query
                if operation.documents("query", name)
            }
            found |= {
                ValueItem(operation.name, "header", name.lower(), value)
                for name, value in request.headers.pairs
                if operation.documents("header", name)
            }
        return frozenset(found)


class _DocumentReader:
    """Walks one parsed document. Only `$ref`s to shared parameters are followed, like RESTCov does."""

    def __init__(self, document: dict[str, Any]) -> None:
        """Keep the document."""
        self._document = document
        self._swagger = "swagger" in document

    def server_paths(self) -> list[str]:
        """The path part of every server, or of `basePath` for Swagger 2.

        Returns:
            The prefixes.
        """
        if self._swagger:
            return [(self._document.get("basePath") or "").rstrip("/")]
        return [urlsplit(server.get("url", "").rstrip("/")).path for server in self._document.get("servers") or []]

    def operations(self) -> list[Operation]:
        """Every operation with its path level and operation level parameters.

        Returns:
            The operations in document order.
        """
        found = []
        for path, item in (self._document.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            shared = self._parameters(item.get("parameters"))
            for method, operation in item.items():
                if method.lower() in _METHODS and isinstance(operation, dict):
                    own = self._parameters(operation.get("parameters"))
                    found.append(Operation(method.upper(), path, frozenset(shared | own)))
        return found

    def _parameters(self, raw: Any) -> set[Parameter]:
        found = set()
        for entry in raw or []:
            resolved = self._resolve(entry)
            if isinstance(resolved, dict) and resolved.get("name") and resolved.get("in"):
                found.add(Parameter(resolved["in"], resolved["name"]))
        return found

    def _resolve(self, entry: Any) -> Any:
        """Follow a `$ref` to `#/components/parameters/`. RESTCov follows no other kind and none in Swagger 2.

        Returns:
            The shared parameter, or the entry itself.
        """
        reference = entry.get("$ref") if isinstance(entry, dict) else None
        if self._swagger or not isinstance(reference, str) or not reference.startswith("#/components/parameters/"):
            return entry
        shared = (self._document.get("components") or {}).get("parameters") or {}
        return shared.get(reference.rsplit("/", 1)[-1], entry)
