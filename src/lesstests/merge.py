"""The Merge step: one test takes over the parameter names of other tests that hit the same operation and status."""

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.parse import parse_qsl, urlsplit

from lesstests.api import ApiDescription, Operation
from lesstests.exchange import Exchange
from lesstests.java import JavaTest
from lesstests.recording import RecordedTest


class MaskedJava:
    """Java source with every literal and comment blanked out, at the same length, so offsets stay valid."""

    _LITERALS_AND_COMMENTS = re.compile(
        r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*[\s\S]*?\*/'
    )

    def __init__(self, text: str) -> None:
        """Mask the text once."""
        self.text = text
        self.masked = self._LITERALS_AND_COMMENTS.sub(self._blank, text)

    @staticmethod
    def _blank(match: re.Match[str]) -> str:
        """Keep the quotes of a literal and fill the rest with `x`, so a `(` or `,` inside it can't count.

        Returns:
            The blanked text, as long as the original.
        """
        text = match.group()
        if text.startswith(('"', "'")):
            quote = 3 if text.startswith('"""') else 1
            return text[:quote] + "x" * (len(text) - 2 * quote) + text[-quote:]
        return re.sub(r"[^\n]", " ", text)

    def closing(self, opening: int) -> int:
        """Find the bracket that closes the one at `opening`.

        Returns:
            The offset of the closing bracket.

        Raises:
            ValueError: If it never closes.
        """
        pairs = {"(": ")", "{": "}", "[": "]"}
        stack = []
        for offset in range(opening, len(self.masked)):
            char = self.masked[offset]
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    raise ValueError(f"Unbalanced {char!r} at {offset}")
                if not stack:
                    return offset
        raise ValueError(f"The bracket at {opening} never closes")

    def split_arguments(self, opening: int) -> list[tuple[int, int]]:
        """Split the arguments of the call whose `(` sits at `opening`.

        Returns:
            The start and end offset of every argument, with the spaces around it trimmed.
        """
        end = self.closing(opening)
        spans, start, depth = [], opening + 1, 0
        for offset in range(opening + 1, end):
            char = self.masked[offset]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                spans.append(self._trim(start, offset))
                start = offset + 1
        if self.masked[start:end].strip():
            spans.append(self._trim(start, end))
        return spans

    def _trim(self, start: int, end: int) -> tuple[int, int]:
        while start < end and self.masked[start].isspace():
            start += 1
        while end > start and self.masked[end - 1].isspace():
            end -= 1
        return start, end


@dataclass(frozen=True, slots=True)
class MapPair:
    """One `key, value` pair of a `Map.of` call, as Java source text."""

    key: str
    value: str

    _LITERAL = re.compile(r'"(?:\\.|[^"\\\n])*"|-?\d+(?:\.\d+)?[LlFfDd]?|true|false|\'(?:\\.|[^\'\\\n])\'')

    @property
    def name(self) -> str:
        """The key without its quotes."""
        return json.loads(self.key)

    @property
    def portable(self) -> bool:
        """Whether the pair is plain literals, so it compiles in any other test method."""
        return self.key.startswith('"') and self._LITERAL.fullmatch(self.value) is not None

    def to_java(self) -> str:
        """Write the pair as a map entry.

        Returns:
            A `java.util.Map.entry` call.
        """
        return f"java.util.Map.entry({self.key}, {self.value})"


@dataclass(frozen=True, slots=True)
class MapArgument:
    """The params or headers argument of a helper call, when the tool can read and replace it."""

    start: int
    end: int
    pairs: tuple[MapPair, ...]

    def names(self) -> set[str]:
        """The keys of the map.

        Returns:
            The names, without quotes.
        """
        return {pair.name for pair in self.pairs if pair.key.startswith('"')}

    def with_pairs(self, extra: Iterable[MapPair]) -> str:
        """Write a new map with the old pairs plus some more.

        Returns:
            A `java.util.Map.ofEntries` call, since `Map.of` stops at ten pairs.
        """
        entries = ", ".join(pair.to_java() for pair in [*self.pairs, *extra])
        return f"java.util.Map.ofEntries({entries})"


@dataclass(frozen=True, slots=True)
class TestMethodSource:
    """The source of one test method whose only request goes through a helper like `get(path, params, headers)`."""

    file: Path
    start: int
    end: int
    name_start: int
    params: MapArgument
    headers: MapArgument

    def copy(self, text: str, new_name: str, query: Iterable[MapPair], headers: Iterable[MapPair]) -> str:
        """Write a copy of the method under a new name with more query and header pairs.

        Returns:
            The new method as source text.
        """
        query, headers = list(query), list(headers)
        edits = [(self.name_start, self.name_start + len(self.method_name(text)), new_name)]
        if query:
            edits.append((self.params.start, self.params.end, self.params.with_pairs(query)))
        if headers:
            edits.append((self.headers.start, self.headers.end, self.headers.with_pairs(headers)))
        method = text
        for start, end, replacement in sorted(edits, reverse=True):
            method = method[:start] + replacement + method[end:]
        shift = sum(len(replacement) - (end - start) for start, end, replacement in edits)
        return method[self.start : self.end + shift]

    def method_name(self, text: str) -> str:
        """Read the method's name back from the file.

        Returns:
            The name.
        """
        return re.match(r"\w+", text[self.name_start :]).group()  # type: ignore[union-attr]


class JavaTestFile:
    """One Java file of a suite, with its test methods found by offset."""

    _TEST_METHOD = re.compile(
        r"@Test\b(?:\s*\([^)]*\))?\s*(?:@\w+(?:\s*\([^)]*\))?\s*)*public\s+(?:final\s+)?void\s+(?P<name>\w+)\s*\("
    )
    _HELPER = re.compile(r"(?<![\w.])(?:get|post|put|patch|delete)\s*\(")

    def __init__(self, path: Path) -> None:
        """Read and mask the file."""
        self.path = path
        self.source = MaskedJava(path.read_text(encoding="utf-8"))

    def method(self, name: str) -> TestMethodSource | None:
        """Find a test method that the Merge step can edit.

        Returns:
            The method, or None when it is missing, sends more than one helper call, or its maps aren't literal.
        """
        masked = self.source.masked
        match = next((m for m in self._TEST_METHOD.finditer(masked) if m.group("name") == name), None)
        if match is None:
            return None
        body_open = masked.index("{", match.end())
        body_close = self.source.closing(body_open)
        calls = list(self._HELPER.finditer(masked, body_open, body_close))
        if len(calls) != 1:
            return None
        arguments = self.source.split_arguments(calls[0].end() - 1)
        if len(arguments) < 3:
            return None
        body = (body_open, body_close)
        params = self._map_argument(arguments[1], body)
        headers = self._map_argument(arguments[2], body)
        if params is None or headers is None:
            return None
        return TestMethodSource(self.path, match.start(), body_close + 1, match.start("name"), params, headers)

    def with_methods(self, methods: Iterable[str]) -> str:
        """Add methods to the end of the class.

        Returns:
            The new file text.
        """
        text = self.source.text
        last = self.source.masked.rindex("}")
        added = "".join(f"\n    {method}\n" for method in methods)
        return text[:last] + added + text[last:]

    def _map_argument(self, span: tuple[int, int], body: tuple[int, int]) -> MapArgument | None:
        """Read `null`, a `Map.of(...)` call, or a local variable that holds one.

        Returns:
            The argument with its pairs, or None for anything else.
        """
        start, end = span
        text = self.source.text[start:end]
        if text == "null":
            return MapArgument(start, end, ())
        opening = self.source.masked.find("(", start, end)
        if opening != -1 and re.fullmatch(r"Map\.of\s*", self.source.masked[start:opening]):
            if self.source.closing(opening) != end - 1:
                return None
            pairs = self._pairs(opening)
            return None if pairs is None else MapArgument(start, end, pairs)
        if re.fullmatch(r"\w+", text):
            assignment = re.compile(rf"\b{text}\s*=\s*Map\.of\s*\(")
            found = list(assignment.finditer(self.source.masked, body[0], start))
            if len(found) != 1:
                return None
            pairs = self._pairs(found[0].end() - 1)
            return None if pairs is None else MapArgument(start, end, pairs)
        return None

    def _pairs(self, opening: int) -> tuple[MapPair, ...] | None:
        arguments = [self.source.text[a:b] for a, b in self.source.split_arguments(opening)]
        if len(arguments) % 2:
            return None
        return tuple(MapPair(arguments[i], arguments[i + 1]) for i in range(0, len(arguments), 2))


@dataclass(frozen=True, slots=True, order=True)
class GroupKey:
    """What a merged request must keep: one operation, one status and the same content types."""

    operation: str
    status: int
    request_type: str
    response_type: str


@dataclass(frozen=True, slots=True)
class MergeMember:
    """One recorded test that sends a single request the Merge step can edit.

    `query` and `headers` hold the documented names its recorded request sent.
    """

    test: JavaTest
    key: GroupKey
    source: TestMethodSource
    query: frozenset[str]
    headers: frozenset[str]


@dataclass(slots=True)
class MergedTest:
    """A copy of a base test that also sends the parameter names of some donor tests."""

    test: JavaTest
    key: GroupKey
    base: JavaTest
    donors: list[JavaTest]
    query: list[MapPair]
    headers: list[MapPair]
    round: int

    def to_dict(self) -> dict[str, Any]:
        """Turn the merge into JSON friendly data.

        Returns:
            The merge as a dict.
        """
        return {
            "test": self.test.id,
            "key": [self.key.operation, self.key.status, self.key.request_type, self.key.response_type],
            "base": self.base.id,
            "donors": [donor.id for donor in self.donors],
            "query": [[pair.key, pair.value] for pair in self.query],
            "headers": [[pair.key, pair.value] for pair in self.headers],
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read a merge saved by `to_dict`.

        Returns:
            The merge.
        """
        return cls(
            JavaTest.from_id(raw["test"]),
            GroupKey(*raw["key"]),
            JavaTest.from_id(raw["base"]),
            [JavaTest.from_id(donor) for donor in raw["donors"]],
            [MapPair(*pair) for pair in raw["query"]],
            [MapPair(*pair) for pair in raw["headers"]],
            raw["round"],
        )


@dataclass(slots=True)
class MergeGroup:
    """The members that share a group key. The member with the most parameter names is the base."""

    key: GroupKey
    members: list[MergeMember] = field(default_factory=list)

    @property
    def base(self) -> MergeMember:
        """The member that the merged copies start from. Ties go to the first test by name."""
        return min(self.members, key=lambda member: (-len(member.query | member.headers), member.test))

    def additions(self, donors: Iterable[MergeMember]) -> tuple[list[MapPair], list[MapPair]]:
        """The pairs the donors add to the base, one value per name, first donor first.

        Only names the base does not send yet count, and only pairs made of plain literals.

        Returns:
            The query pairs and the header pairs.
        """
        base = self.base

        # A name already in the base's map would make `Map.ofEntries` throw, even when the base never sent it.
        taken_query = base.query | base.source.params.names()
        taken_headers = base.headers | {name.lower() for name in base.source.headers.names()}
        query: dict[str, MapPair] = {}
        headers: dict[str, MapPair] = {}
        for donor in donors:
            for pair in donor.source.params.pairs:
                if pair.portable and pair.name in donor.query and pair.name not in taken_query:
                    query.setdefault(pair.name, pair)
            for pair in donor.source.headers.pairs:
                if pair.portable and pair.name.lower() in donor.headers and pair.name.lower() not in taken_headers:
                    headers.setdefault(pair.name.lower(), pair)
        return list(query.values()), list(headers.values())


class MergePlanner:
    """Finds the merge groups of a recording and writes the merged copies into the Java sources."""

    def __init__(self, records: list[RecordedTest], api: ApiDescription, source_root: Path) -> None:
        """Group every test that sent exactly one request that matched an operation and whose source is editable."""
        self._source_root = source_root
        self._files: dict[Path, JavaTestFile] = {}
        groups: dict[GroupKey, MergeGroup] = {}
        for record in records:
            member = self._member(record, api)
            if member is not None:
                groups.setdefault(member.key, MergeGroup(member.key)).members.append(member)
        self.groups = sorted(groups.values(), key=lambda group: group.key)

    def first_round(self) -> list[MergedTest]:
        """One merged copy per group whose donors add at least one name.

        Returns:
            The merged tests, named in order.
        """
        merged: list[MergedTest] = []
        for group in self.groups:
            donors = [member for member in group.members if member is not group.base]
            candidate = self._merged(group, donors, 1, len(merged) + 1)
            if candidate is not None:
                merged.append(candidate)
        return merged

    def split(self, failed: list[MergedTest], round_number: int, start: int) -> list[MergedTest]:
        """Split the donors of each failed merge in two halves, so a donor that changes the status gets isolated.

        Returns:
            The new merged tests, numbered from `start`.
        """
        new: list[MergedTest] = []
        for merge in failed:
            if len(merge.donors) < 2:
                continue
            group = self._group(merge.key)
            donors = [member for member in group.members if member.test in merge.donors]
            half = len(donors) // 2
            for part in (donors[:half], donors[half:]):
                candidate = self._merged(group, part, round_number, start + len(new))
                if candidate is not None:
                    new.append(candidate)
        return new

    def edited_files(self, merged: Iterable[MergedTest]) -> dict[Path, str]:
        """Write every merged copy into the class of its base test.

        Returns:
            The new text of every file that changed, keyed by its path below the source root.
        """
        per_file: dict[Path, list[str]] = defaultdict(list)
        for merge in merged:
            base = self._member_of(merge)
            file = self._files[base.source.file]
            per_file[base.source.file].append(
                base.source.copy(file.source.text, merge.test.method, merge.query, merge.headers)
            )
        return {
            path.relative_to(self._source_root): self._files[path].with_methods(methods)
            for path, methods in per_file.items()
        }

    def _merged(
        self, group: MergeGroup, donors: list[MergeMember], round_number: int, number: int
    ) -> MergedTest | None:
        query, headers = group.additions(donors)
        if not query and not headers:
            return None
        base = group.base.test
        test = JavaTest(base.class_name, f"lesstestsMerged{number}")
        return MergedTest(test, group.key, base, [donor.test for donor in donors], query, headers, round_number)

    def _group(self, key: GroupKey) -> MergeGroup:
        return next(group for group in self.groups if group.key == key)

    def _member_of(self, merge: MergedTest) -> MergeMember:
        return next(member for member in self._group(merge.key).members if member.test == merge.base)

    def _member(self, record: RecordedTest, api: ApiDescription) -> MergeMember | None:
        if len(record.exchanges) != 1:
            return None
        exchange = record.exchanges[0]
        operation = api.match(exchange.request.method, exchange.request.target)
        if operation is None:
            return None
        source = self._source_of(record.test)
        if source is None:
            return None
        key = GroupKey(
            operation.name,
            exchange.response.status,
            MediaType.of(exchange.request.headers.get("Content-Type")),
            MediaType.of(exchange.response.headers.get("Content-Type")),
        )
        query, headers = self._sent_names(operation, exchange)
        return MergeMember(record.test, key, source, query, headers)

    @staticmethod
    def _sent_names(operation: Operation, exchange: Exchange) -> tuple[frozenset[str], frozenset[str]]:
        """The documented names the recorded request really sent, wherever the test put them.

        A test can write a parameter into its path string instead of its map, so the source alone can miss one.

        Returns:
            The query names and the lower case header names.
        """
        query = frozenset(
            name for name, _ in parse_qsl(urlsplit(exchange.request.target).query) if operation.documents("query", name)
        )
        headers = frozenset(
            name.lower() for name, _ in exchange.request.headers.pairs if operation.documents("header", name)
        )
        return query, headers

    def _source_of(self, test: JavaTest) -> TestMethodSource | None:
        path = self._source_root / Path(*test.class_name.split(".")).with_suffix(".java")
        if not path.exists():
            return None
        if path not in self._files:
            self._files[path] = JavaTestFile(path)
        return self._files[path].method(test.method)


class MediaType:
    """Content type helpers, normalised the way RESTCov compares them."""

    @staticmethod
    def of(header: str | None) -> str:
        """Drop the parameters and the case, so `application/json; charset=utf-8` becomes `application/json`.

        Returns:
            The bare media type, or an empty string when there is none.
        """
        return (header or "").split(";", 1)[0].strip().lower()


class CandidateRunner(Protocol):
    """Compiles the suite with some merged copies and records each copy alone."""

    def record(self, merged: list[MergedTest], edited: dict[Path, str], folder: Path) -> dict[JavaTest, RecordedTest]:
        """Record every merged test of one round.

        Returns:
            The record of each merged test.
        """
        ...


@dataclass(slots=True)
class MergeOutcome:
    """The merged tests that kept their status, plus every attempt with the reason it was dropped."""

    accepted: list[MergedTest]
    records: dict[JavaTest, RecordedTest]
    attempts: list[dict[str, Any]]


class Merger:
    """Runs the merge rounds. A merged copy is kept only when it sent one request and got the group's status back."""

    def __init__(self, planner: MergePlanner, runner: CandidateRunner, api: ApiDescription, rounds: int = 4) -> None:
        """Wire the parts. `rounds` bounds how often failed merges get split."""
        self._planner = planner
        self._runner = runner
        self._api = api
        self._rounds = rounds

    def run(self, folder: Path) -> MergeOutcome:
        """Try the first round, then split what failed, until nothing is left or the rounds run out.

        Returns:
            The accepted merges with their records.
        """
        accepted: list[MergedTest] = []
        records: dict[JavaTest, RecordedTest] = {}
        attempts: list[dict[str, Any]] = []
        pending = self._planner.first_round()
        number = len(pending) + 1
        for round_number in range(1, self._rounds + 1):
            if not pending:
                break
            edited = self._planner.edited_files([*accepted, *pending])
            recorded = self._runner.record(pending, edited, folder / f"round-{round_number}")
            failed = []
            for merge in pending:
                record = recorded.get(merge.test)
                reason = self._rejection(merge, record)
                attempts.append({**merge.to_dict(), "kept": reason is None, "reason": reason})
                if reason is None and record is not None:
                    accepted.append(merge)
                    records[merge.test] = record
                else:
                    failed.append(merge)
            pending = self._planner.split(failed, round_number + 1, number)
            number += len(pending)
        return MergeOutcome(accepted, records, attempts)

    def _rejection(self, merge: MergedTest, record: RecordedTest | None) -> str | None:
        """Say why a merged copy can't stay, or None when it can.

        Returns:
            The reason, or None.
        """
        if record is None:
            return "not recorded"
        if len(record.exchanges) != 1:
            return f"sent {len(record.exchanges)} requests"
        exchange = record.exchanges[0]
        operation = self._api.match(exchange.request.method, exchange.request.target)
        if operation is None or operation.name != merge.key.operation:
            return "reached another operation"
        if exchange.response.status != merge.key.status:
            return f"got {exchange.response.status} instead of {merge.key.status}"
        return None
