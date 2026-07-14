"""Immutable, dependency-free representation of finite simple graphs.

The canonical representation in this module is canonical for a *numbered*
graph: vertices are ``0, ..., order - 1`` and edges are sorted pairs ``u < v``.
It is deliberately not advertised as an isomorphism-canonical labeling.  A
future nauty adapter may provide a separately versioned isomorphism identity.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

Edge = tuple[int, int]

GRAPH_SCHEMA_VERSION: Final = "total-coloring.simple-graph.v1"
DEFAULT_MAX_JSON_ORDER: Final = 100_000
DEFAULT_MAX_JSON_BYTES: Final = 16 * 1024 * 1024
DEFAULT_MAX_JSON_DEPTH: Final = 128
DEFAULT_MAX_JSON_INTEGER_DIGITS: Final = 128
_GRAPH_KEYS: Final = frozenset({"schema_version", "order", "edges"})


class GraphError(ValueError):
    """Base class for graph validation and serialization errors."""


class GraphValidationError(GraphError):
    """Raised when an object does not describe a finite simple graph."""


class GraphFormatError(GraphError):
    """Raised when serialized graph data is malformed or non-canonical."""


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a JSON-compatible value deterministically as UTF-8.

    The representation uses sorted object keys, no insignificant whitespace,
    and rejects non-finite floating-point values.  Semantic formats in this
    package should hash these bytes rather than platform-dependent repr output.
    """

    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise GraphFormatError("value is not canonical-JSON serializable") from exc


def sha256_hex(value: object) -> str:
    """Return the lowercase SHA-256 digest of a canonical JSON value."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GraphFormatError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise GraphFormatError(f"non-finite JSON number is forbidden: {value}")


def _bounded_json_int(value: str, *, max_digits: int) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > max_digits:
        raise GraphFormatError(f"JSON integer exceeds {max_digits} digits")
    return int(value)


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise GraphFormatError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _reject_json_surrogates(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
                raise GraphFormatError("JSON strings may not contain surrogate code points")
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())


def _validate_json_nesting(text: str, *, max_depth: int) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > max_depth:
                raise GraphFormatError(f"JSON nesting exceeds {max_depth} levels")
        elif character in "]}":
            depth -= 1


def strict_json_loads(
    data: str | bytes,
    *,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
    max_depth: int = DEFAULT_MAX_JSON_DEPTH,
    max_integer_digits: int = DEFAULT_MAX_JSON_INTEGER_DIGITS,
) -> object:
    """Parse bounded JSON while rejecting duplicate keys and non-finite numbers."""

    if not isinstance(data, str | bytes):
        raise GraphFormatError("JSON input must be str or bytes")
    if not all(
        isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        for limit in (max_bytes, max_depth, max_integer_digits)
    ):
        raise GraphFormatError("JSON parser limits must be positive integers")
    try:
        if isinstance(data, bytes):
            if len(data) > max_bytes:
                raise GraphFormatError(f"JSON input exceeds {max_bytes} bytes")
            text = data.decode("utf-8")
        else:
            if len(data) > max_bytes:
                raise GraphFormatError(f"JSON input exceeds {max_bytes} bytes")
            encoded = data.encode("utf-8")
            if len(encoded) > max_bytes:
                raise GraphFormatError(f"JSON input exceeds {max_bytes} bytes")
            text = data
        _validate_json_nesting(text, max_depth=max_depth)
        parsed: object = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
            parse_int=lambda value: _bounded_json_int(value, max_digits=max_integer_digits),
        )
        _reject_json_surrogates(parsed)
    except GraphFormatError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise GraphFormatError("invalid JSON") from exc
    return parsed


def _require_int(value: object, *, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphValidationError(f"{name} must be an integer")
    if value < minimum:
        raise GraphValidationError(f"{name} must be at least {minimum}")
    return value


def _validate_max_order(max_order: int | None) -> int | None:
    if max_order is None:
        return None
    if isinstance(max_order, bool) or not isinstance(max_order, int) or max_order < 0:
        raise GraphValidationError("max_order must be a nonnegative integer or None")
    return max_order


def _normalize_edges(order: int, edges: Iterable[object]) -> tuple[Edge, ...]:
    normalized: list[Edge] = []
    seen: set[Edge] = set()

    try:
        iterator = iter(edges)
    except TypeError as exc:
        raise GraphValidationError("edges must be an iterable of endpoint pairs") from exc

    for index, raw_edge in enumerate(iterator):
        if isinstance(raw_edge, str | bytes):
            raise GraphValidationError(f"edge {index} must be a two-item sequence")
        try:
            endpoints = tuple(cast(Iterable[object], raw_edge))
        except TypeError as exc:
            raise GraphValidationError(f"edge {index} must be a two-item sequence") from exc
        if len(endpoints) != 2:
            raise GraphValidationError(f"edge {index} must contain exactly two endpoints")
        u = _require_int(endpoints[0], name=f"edge {index} endpoint 0")
        v = _require_int(endpoints[1], name=f"edge {index} endpoint 1")
        if u >= order or v >= order:
            raise GraphValidationError(f"edge {index} endpoint is outside 0..{order - 1}")
        if u == v:
            raise GraphValidationError(f"edge {index} is a loop")
        edge = (u, v) if u < v else (v, u)
        if edge in seen:
            raise GraphValidationError(f"duplicate edge: {edge}")
        seen.add(edge)
        normalized.append(edge)

    normalized.sort()
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class SimpleGraph:
    """A finite simple undirected graph with stable numbered vertices.

    Direct construction and :meth:`from_edges` both normalize endpoint order
    and sort edges.  Duplicate edges are rejected rather than silently merged,
    because accepting them would hide malformed scientific inputs.
    """

    order: int
    edges: tuple[Edge, ...]
    _adjacency: tuple[frozenset[int], ...] = field(init=False, repr=False, compare=False)
    _degrees: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        order = _require_int(self.order, name="order")
        normalized = _normalize_edges(order, cast(Iterable[object], self.edges))

        adjacency: list[set[int]] = [set() for _ in range(order)]
        for u, v in normalized:
            adjacency[u].add(v)
            adjacency[v].add(u)

        object.__setattr__(self, "order", order)
        object.__setattr__(self, "edges", normalized)
        object.__setattr__(self, "_adjacency", tuple(frozenset(row) for row in adjacency))
        object.__setattr__(self, "_degrees", tuple(len(row) for row in adjacency))
        object.__setattr__(self, "_fingerprint", sha256_hex(self.to_dict()))

    @classmethod
    def from_edges(cls, order: int, edges: Iterable[Edge]) -> SimpleGraph:
        """Build a graph, normalizing edge orientation and input order."""

        try:
            edge_tuple = tuple(edges)
        except TypeError as exc:
            raise GraphValidationError("edges must be iterable") from exc
        return cls(order=order, edges=edge_tuple)

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        max_order: int | None = DEFAULT_MAX_JSON_ORDER,
    ) -> SimpleGraph:
        """Parse the strict canonical graph object used by schema v1."""

        if not isinstance(value, Mapping):
            raise GraphFormatError("graph document must be a JSON object")
        keys = set(value)
        if keys != _GRAPH_KEYS:
            missing = sorted(_GRAPH_KEYS - keys)
            extra = sorted(keys - _GRAPH_KEYS)
            details: list[str] = []
            if missing:
                details.append(f"missing keys {missing}")
            if extra:
                details.append(f"unknown keys {extra}")
            raise GraphFormatError("invalid graph object: " + "; ".join(details))
        if value["schema_version"] != GRAPH_SCHEMA_VERSION:
            raise GraphFormatError("unsupported graph schema_version")

        order = _require_int(value["order"], name="order")
        checked_max_order = _validate_max_order(max_order)
        if checked_max_order is not None and order > checked_max_order:
            raise GraphValidationError(
                f"order {order} exceeds parser resource limit {checked_max_order}"
            )
        raw_edges = value["edges"]
        if isinstance(raw_edges, str | bytes) or not isinstance(raw_edges, Sequence):
            raise GraphFormatError("edges must be a JSON array")

        graph = cls.from_edges(order, cast(Iterable[Edge], raw_edges))
        if graph.to_dict() != dict(value):
            raise GraphFormatError(
                "graph JSON is not canonical: endpoints must satisfy u < v and edges must be sorted"
            )
        return graph

    @classmethod
    def from_json(
        cls,
        data: str | bytes,
        *,
        max_order: int | None = DEFAULT_MAX_JSON_ORDER,
    ) -> SimpleGraph:
        """Parse a strict canonical graph JSON document."""

        value = strict_json_loads(data)
        if not isinstance(value, Mapping):
            raise GraphFormatError("graph document must be a JSON object")
        return cls.from_dict(cast(Mapping[str, object], value), max_order=max_order)

    @classmethod
    def from_graph6(cls, data: str | bytes) -> SimpleGraph:
        """Decode one graph6 record without importing optional dependencies."""

        from total_coloring.graph6 import decode_graph6

        return decode_graph6(data)

    @property
    def size(self) -> int:
        """Number of edges."""

        return len(self.edges)

    @property
    def degrees(self) -> tuple[int, ...]:
        """Degrees in vertex order."""

        return self._degrees

    @property
    def max_degree(self) -> int:
        """Maximum degree, defined as zero for the empty graph."""

        return max(self._degrees, default=0)

    @property
    def min_degree(self) -> int:
        """Minimum degree, defined as zero for the empty graph."""

        return min(self._degrees, default=0)

    @property
    def maximum_degree(self) -> int:
        """Long-form alias for :attr:`max_degree`."""

        return self.max_degree

    @property
    def minimum_degree(self) -> int:
        """Long-form alias for :attr:`min_degree`."""

        return self.min_degree

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the canonical schema-v1 numbered-graph JSON."""

        return self._fingerprint

    @property
    def is_regular(self) -> bool:
        """Whether all vertices have the same degree (including empty graphs)."""

        return len(set(self._degrees)) <= 1

    def _validate_vertex(self, vertex: int) -> int:
        value = _require_int(vertex, name="vertex")
        if value >= self.order:
            raise GraphValidationError(f"vertex must be in 0..{self.order - 1}")
        return value

    def degree(self, vertex: int) -> int:
        """Return the degree of one vertex."""

        return self._degrees[self._validate_vertex(vertex)]

    def neighbors(self, vertex: int) -> frozenset[int]:
        """Return the immutable neighbor set of one vertex."""

        return self._adjacency[self._validate_vertex(vertex)]

    def has_edge(self, u: int, v: int) -> bool:
        """Return whether two distinct in-range vertices are adjacent."""

        left = self._validate_vertex(u)
        right = self._validate_vertex(v)
        if left == right:
            return False
        return right in self._adjacency[left]

    def incident_edges(self, vertex: int) -> tuple[Edge, ...]:
        """Return incident edges in canonical global edge order."""

        checked = self._validate_vertex(vertex)
        return tuple(edge for edge in self.edges if checked in edge)

    def relabel(self, permutation: Sequence[int]) -> SimpleGraph:
        """Return the graph under a bijection from old to new vertex labels."""

        if isinstance(permutation, str | bytes) or not isinstance(permutation, Sequence):
            raise GraphValidationError("permutation must be a sequence")
        if len(permutation) != self.order:
            raise GraphValidationError("permutation length must equal graph order")
        values = tuple(
            _require_int(value, name=f"permutation[{index}]")
            for index, value in enumerate(permutation)
        )
        if set(values) != set(range(self.order)):
            raise GraphValidationError("permutation must be a bijection of 0..order-1")
        return SimpleGraph.from_edges(self.order, ((values[u], values[v]) for u, v in self.edges))

    def to_dict(self) -> dict[str, object]:
        """Return the canonical schema-v1 JSON object."""

        return {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "order": self.order,
            "edges": [[u, v] for u, v in self.edges],
        }

    def to_json(self) -> str:
        """Return canonical JSON without a trailing newline."""

        return canonical_json_bytes(self.to_dict()).decode("utf-8")

    def to_graph6(self, *, include_header: bool = False) -> str:
        """Encode this numbered graph as one graph6 record."""

        from total_coloring.graph6 import encode_graph6

        return encode_graph6(self, include_header=include_header)


__all__ = [
    "DEFAULT_MAX_JSON_BYTES",
    "DEFAULT_MAX_JSON_DEPTH",
    "DEFAULT_MAX_JSON_INTEGER_DIGITS",
    "DEFAULT_MAX_JSON_ORDER",
    "GRAPH_SCHEMA_VERSION",
    "Edge",
    "GraphError",
    "GraphFormatError",
    "GraphValidationError",
    "SimpleGraph",
    "canonical_json_bytes",
    "sha256_hex",
    "strict_json_loads",
]
