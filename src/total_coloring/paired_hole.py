"""Fail-closed semantic verification for exact paired-hole partial states.

The verifier in this module is intentionally independent of every search and
solver implementation.  Its trust root is only a numbered simple graph, a
fixed vertex coloring, a partial edge coloring with one uncolored edge, and a
small set of vertex-role pointers.  Missing colors, fan closures, alternating
components, Kempe swaps, and the final total-coloring certificate are all
reconstructed rather than accepted as witness claims.

The supported v1 regime is the two-sided, zero-surplus, two-satellite residue
inside a matching maximum-degree core.  ``fully_blocked_candidate`` means only
that the checks implemented here found no direct full-component swap-and-fill
exit.  It is deliberately not a negative certificate or a theorem.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import combinations
from typing import Final, cast

from total_coloring.certificates import TotalColoringCertificate
from total_coloring.graph import (
    Edge,
    GraphError,
    GraphFormatError,
    SimpleGraph,
    canonical_json_bytes,
    sha256_hex,
    strict_json_loads,
)

PAIRED_HOLE_SCHEMA_VERSION: Final = "total-coloring.paired-hole-state.v1"
PAIRED_HOLE_KIND: Final = "paired-hole-partial-total-coloring"
_STATE_KEYS: Final = frozenset(
    {
        "schema_version",
        "kind",
        "graph",
        "degree_parameter",
        "palette_size",
        "vertex_colors",
        "edge_colors",
        "uncolored_edge",
        "alpha",
        "roles",
    }
)
_ROLE_KEYS: Final = frozenset({"x", "y", "x_fan_satellites", "y_fan_satellites"})


class PairedHoleError(ValueError):
    """Base class for paired-hole state errors."""


class PairedHoleFormatError(PairedHoleError):
    """Raised when a paired-hole state is not canonical schema-v1 data."""


class PairedHoleStatus(StrEnum):
    """The deliberately disjoint verifier outcomes."""

    VERIFIED_ONE_SWAP_EXIT = "verified_one_swap_exit"
    VERIFIED_CROSS_TERMINAL_RELEASE_EXIT = "verified_cross_terminal_release_exit"
    VERIFIED_ALPHA_TERMINAL_RELEASE = "verified_alpha_terminal_release"
    VERIFIED_TWO_SWAP_ORBIT_EXIT = "verified_two_swap_orbit_exit"
    FULLY_BLOCKED_CANDIDATE = "fully_blocked_candidate"
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_OUT_OF_SCOPE = "unsupported_out_of_scope"


class CrossComponentRelation(StrEnum):
    """Whether the two endpoint holes belong to one cross-color component."""

    LINKED = "linked"
    DISTINCT = "distinct"


class ProfileAlignment(StrEnum):
    """Whether satellite fixed colors align with the opposite hole pairs."""

    ALIGNED = "aligned"
    NONALIGNED = "nonaligned"


def _require_nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PairedHoleFormatError(f"{name} must be an integer")
    if value < 0:
        raise PairedHoleFormatError(f"{name} must be nonnegative")
    return value


def _require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], *, name: str
) -> None:
    keys = set(value)
    if keys == expected:
        return
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing keys {missing}")
    if extra:
        details.append(f"unknown keys {extra}")
    raise PairedHoleFormatError(f"invalid {name}: " + "; ".join(details))


def _require_sequence(value: object, *, name: str) -> Sequence[object]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise PairedHoleFormatError(f"{name} must be a JSON array")
    return cast(Sequence[object], value)


def _require_color(value: object, *, name: str, palette_size: int) -> int:
    color = _require_nonnegative_int(value, name=name)
    if color >= palette_size:
        raise PairedHoleFormatError(f"{name}={color} is outside palette 0..{palette_size - 1}")
    return color


def _require_edge(value: object, *, name: str) -> Edge:
    raw = _require_sequence(value, name=name)
    if len(raw) != 2:
        raise PairedHoleFormatError(f"{name} must contain exactly two endpoints")
    left = _require_nonnegative_int(raw[0], name=f"{name}[0]")
    right = _require_nonnegative_int(raw[1], name=f"{name}[1]")
    if left >= right:
        raise PairedHoleFormatError(f"{name} must be canonical with endpoint 0 < endpoint 1")
    return (left, right)


def _normalize_vertex_colors(values: Iterable[int], palette_size: int) -> tuple[int, ...]:
    if isinstance(values, str | bytes):
        raise PairedHoleFormatError("vertex_colors must be an array")
    try:
        raw_values = tuple(cast(Iterable[object], values))
    except TypeError as exc:
        raise PairedHoleFormatError("vertex_colors must be an array") from exc
    return tuple(
        _require_color(value, name=f"vertex_colors[{index}]", palette_size=palette_size)
        for index, value in enumerate(raw_values)
    )


def _normalize_edge_colors(
    values: Iterable[int | None], palette_size: int
) -> tuple[int | None, ...]:
    if isinstance(values, str | bytes):
        raise PairedHoleFormatError("edge_colors must be an array")
    try:
        raw_values = tuple(cast(Iterable[object], values))
    except TypeError as exc:
        raise PairedHoleFormatError("edge_colors must be an array") from exc
    colors: list[int | None] = []
    for index, value in enumerate(raw_values):
        if value is None:
            colors.append(None)
        else:
            colors.append(
                _require_color(value, name=f"edge_colors[{index}]", palette_size=palette_size)
            )
    return tuple(colors)


@dataclass(frozen=True, slots=True)
class PairedHoleRoles:
    """Untrusted pointers naming the two orientations and their satellites."""

    x: int
    y: int
    x_fan_satellites: tuple[int, int]
    y_fan_satellites: tuple[int, int]

    def __post_init__(self) -> None:
        x = _require_nonnegative_int(self.x, name="roles.x")
        y = _require_nonnegative_int(self.y, name="roles.y")
        x_satellites = self._normalize_satellites(
            self.x_fan_satellites, name="roles.x_fan_satellites"
        )
        y_satellites = self._normalize_satellites(
            self.y_fan_satellites, name="roles.y_fan_satellites"
        )
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "x_fan_satellites", x_satellites)
        object.__setattr__(self, "y_fan_satellites", y_satellites)

    @staticmethod
    def _normalize_satellites(values: Iterable[int], *, name: str) -> tuple[int, int]:
        if isinstance(values, str | bytes):
            raise PairedHoleFormatError(f"{name} must be an array of two vertices")
        try:
            raw_values = tuple(cast(Iterable[object], values))
        except TypeError as exc:
            raise PairedHoleFormatError(f"{name} must be an array of two vertices") from exc
        if len(raw_values) != 2:
            raise PairedHoleFormatError(f"{name} must contain exactly two vertices")
        vertices = tuple(
            _require_nonnegative_int(value, name=f"{name}[{index}]")
            for index, value in enumerate(raw_values)
        )
        if vertices[0] >= vertices[1]:
            raise PairedHoleFormatError(f"{name} must be strictly increasing")
        return cast(tuple[int, int], vertices)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PairedHoleRoles:
        if not isinstance(value, Mapping):
            raise PairedHoleFormatError("roles must be a JSON object")
        _require_exact_keys(value, _ROLE_KEYS, name="roles object")
        return cls(
            x=_require_nonnegative_int(value["x"], name="roles.x"),
            y=_require_nonnegative_int(value["y"], name="roles.y"),
            x_fan_satellites=cast(
                tuple[int, int],
                tuple(
                    _require_nonnegative_int(item, name="roles.x_fan_satellites item")
                    for item in _require_sequence(
                        value["x_fan_satellites"], name="roles.x_fan_satellites"
                    )
                ),
            ),
            y_fan_satellites=cast(
                tuple[int, int],
                tuple(
                    _require_nonnegative_int(item, name="roles.y_fan_satellites item")
                    for item in _require_sequence(
                        value["y_fan_satellites"], name="roles.y_fan_satellites"
                    )
                ),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "x": self.x,
            "y": self.y,
            "x_fan_satellites": list(self.x_fan_satellites),
            "y_fan_satellites": list(self.y_fan_satellites),
        }


@dataclass(frozen=True, slots=True)
class PairedHoleState:
    """Canonical raw input to the paired-hole semantic verifier."""

    graph: SimpleGraph
    degree_parameter: int
    palette_size: int
    vertex_colors: tuple[int, ...]
    edge_colors: tuple[int | None, ...]
    uncolored_edge: Edge
    alpha: int
    roles: PairedHoleRoles
    _fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.graph, SimpleGraph):
            raise PairedHoleFormatError("graph must be a SimpleGraph")
        degree_parameter = _require_nonnegative_int(self.degree_parameter, name="degree_parameter")
        palette_size = _require_nonnegative_int(self.palette_size, name="palette_size")
        vertex_colors = _normalize_vertex_colors(self.vertex_colors, palette_size)
        edge_colors = _normalize_edge_colors(self.edge_colors, palette_size)
        uncolored_edge = _require_edge(self.uncolored_edge, name="uncolored_edge")
        alpha = _require_color(self.alpha, name="alpha", palette_size=palette_size)
        if not isinstance(self.roles, PairedHoleRoles):
            raise PairedHoleFormatError("roles must be PairedHoleRoles")
        object.__setattr__(self, "degree_parameter", degree_parameter)
        object.__setattr__(self, "palette_size", palette_size)
        object.__setattr__(self, "vertex_colors", vertex_colors)
        object.__setattr__(self, "edge_colors", edge_colors)
        object.__setattr__(self, "uncolored_edge", uncolored_edge)
        object.__setattr__(self, "alpha", alpha)
        object.__setattr__(self, "_fingerprint", sha256_hex(self.to_dict()))

    @classmethod
    def create(
        cls,
        *,
        graph: SimpleGraph,
        degree_parameter: int,
        palette_size: int,
        vertex_colors: Iterable[int],
        edge_colors: Iterable[int | None],
        uncolored_edge: Edge,
        alpha: int,
        roles: PairedHoleRoles,
    ) -> PairedHoleState:
        """Freeze nested input arrays and construct one canonical state."""

        left, right = uncolored_edge
        canonical_edge = (left, right) if left < right else (right, left)
        return cls(
            graph=graph,
            degree_parameter=degree_parameter,
            palette_size=palette_size,
            vertex_colors=tuple(vertex_colors),
            edge_colors=tuple(edge_colors),
            uncolored_edge=canonical_edge,
            alpha=alpha,
            roles=roles,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PairedHoleState:
        """Parse an exact schema-v1 state object without semantic shortcuts."""

        if not isinstance(value, Mapping):
            raise PairedHoleFormatError("paired-hole document must be a JSON object")
        _require_exact_keys(value, _STATE_KEYS, name="paired-hole state object")
        if value["schema_version"] != PAIRED_HOLE_SCHEMA_VERSION:
            raise PairedHoleFormatError("unsupported paired-hole schema_version")
        if value["kind"] != PAIRED_HOLE_KIND:
            raise PairedHoleFormatError("unsupported paired-hole kind")
        raw_graph = value["graph"]
        if not isinstance(raw_graph, Mapping):
            raise PairedHoleFormatError("graph must be a JSON object")
        try:
            graph = SimpleGraph.from_dict(cast(Mapping[str, object], raw_graph))
        except GraphError as exc:
            raise PairedHoleFormatError(f"invalid graph: {exc}") from exc
        palette_size = _require_nonnegative_int(value["palette_size"], name="palette_size")
        raw_vertex_colors = _require_sequence(value["vertex_colors"], name="vertex_colors")
        raw_edge_colors = _require_sequence(value["edge_colors"], name="edge_colors")
        raw_roles = value["roles"]
        if not isinstance(raw_roles, Mapping):
            raise PairedHoleFormatError("roles must be a JSON object")
        return cls(
            graph=graph,
            degree_parameter=_require_nonnegative_int(
                value["degree_parameter"], name="degree_parameter"
            ),
            palette_size=palette_size,
            vertex_colors=tuple(
                _require_color(item, name=f"vertex_colors[{index}]", palette_size=palette_size)
                for index, item in enumerate(raw_vertex_colors)
            ),
            edge_colors=tuple(
                None
                if item is None
                else _require_color(item, name=f"edge_colors[{index}]", palette_size=palette_size)
                for index, item in enumerate(raw_edge_colors)
            ),
            uncolored_edge=_require_edge(value["uncolored_edge"], name="uncolored_edge"),
            alpha=_require_color(value["alpha"], name="alpha", palette_size=palette_size),
            roles=PairedHoleRoles.from_dict(cast(Mapping[str, object], raw_roles)),
        )

    @classmethod
    def from_json(cls, data: str | bytes) -> PairedHoleState:
        """Parse bounded strict JSON, rejecting duplicate keys and noncanonical data."""

        try:
            value = strict_json_loads(data)
        except GraphFormatError as exc:
            raise PairedHoleFormatError(str(exc)) from exc
        if not isinstance(value, Mapping):
            raise PairedHoleFormatError("paired-hole document must be a JSON object")
        return cls.from_dict(cast(Mapping[str, object], value))

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the canonical numbered-state JSON."""

        return self._fingerprint

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PAIRED_HOLE_SCHEMA_VERSION,
            "kind": PAIRED_HOLE_KIND,
            "graph": self.graph.to_dict(),
            "degree_parameter": self.degree_parameter,
            "palette_size": self.palette_size,
            "vertex_colors": list(self.vertex_colors),
            "edge_colors": list(self.edge_colors),
            "uncolored_edge": list(self.uncolored_edge),
            "alpha": self.alpha,
            "roles": self.roles.to_dict(),
        }

    def to_json(self) -> str:
        """Return canonical JSON without a trailing newline."""

        return canonical_json_bytes(self.to_dict()).decode("utf-8")


@dataclass(frozen=True, slots=True)
class PairedHoleIssue:
    """One deterministic format, validity, or scope finding."""

    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class FanClosureAnalysis:
    """A full fan-reachability closure recomputed from the partial coloring."""

    center: int
    root: int
    satellites: tuple[int, ...]
    closure: tuple[int, ...]
    satellite_edge_colors: tuple[tuple[int, int], ...]
    surplus: int


@dataclass(frozen=True, slots=True)
class AlternatingComponent:
    """One full two-edge-color component in canonical graph order."""

    colors: tuple[int, int]
    vertices: tuple[int, ...]
    edge_indices: tuple[int, ...]
    edges: tuple[Edge, ...]
    walk: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TerminalLockAnalysis:
    """Terminal-release data for one blocked alpha-beta path."""

    path_length: int
    penultimate: int
    terminal: int
    common_release_colors: tuple[int, ...]
    terminal_release_applicable: bool
    uncolored_incidence: tuple[int, int]
    degree_sum: int
    lock_degree_lower_bound: int
    bound_applies: bool
    bound_satisfied: bool | None


@dataclass(frozen=True, slots=True)
class AlphaBetaBlockage:
    """One of the twelve derived hole-to-fixed-color component claims."""

    beta: int
    hole: int
    component: AlternatingComponent
    path_vertices: tuple[int, ...]
    path_edge_indices: tuple[int, ...]
    blocked_by_fixed_beta: bool
    terminal: TerminalLockAnalysis | None


@dataclass(frozen=True, slots=True)
class CrossSwapAttempt:
    """A full-component swap followed by a direct fill attempt."""

    side: str
    swapped_component: AlternatingComponent
    fill_color: int
    swap_is_proper: bool
    common_hole_after_swap: bool
    completion_certificate: TotalColoringCertificate | None
    issues: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return (
            self.swap_is_proper
            and self.common_hole_after_swap
            and self.completion_certificate is not None
            and not self.issues
        )


@dataclass(frozen=True, slots=True)
class CrossTerminalReleaseExit:
    """A replayed cross terminal release followed by a certified fill."""

    a: int
    b: int
    side: str
    hole: int
    path_vertices: tuple[int, ...]
    path_edges: tuple[Edge, ...]
    release_color: int
    fill_color: int
    resulting_partial_fingerprint: str
    completion_certificate: TotalColoringCertificate


@dataclass(frozen=True, slots=True)
class CrossRootTerminalAnalysis:
    """Terminal lock/release analysis for one distinct A x B root component."""

    side: str
    hole: int
    path_vertices: tuple[int, ...]
    path_edges: tuple[Edge, ...]
    blocked_by_fixed_color: bool
    terminal: TerminalLockAnalysis | None
    releases: tuple[CrossTerminalReleaseExit, ...]


@dataclass(frozen=True, slots=True)
class CrossComponentAnalysis:
    """Linked/distinct topology and direct exits for one color pair in A x B."""

    a: int
    b: int
    relation: CrossComponentRelation
    x_component: AlternatingComponent
    y_component: AlternatingComponent
    attempts: tuple[CrossSwapAttempt, ...]
    root_terminal_analyses: tuple[CrossRootTerminalAnalysis, ...]


@dataclass(frozen=True, slots=True)
class TerminalReleaseWitness:
    """One replayed terminal-edge recoloring and prefix swap."""

    beta: int
    hole: int
    release_color: int
    terminal_edge: Edge
    prefix_edges: tuple[Edge, ...]
    resulting_partial_fingerprint: str


@dataclass(frozen=True, slots=True)
class KempeMove:
    """One independently reconstructed legal full-component swap."""

    colors: tuple[int, int]
    component: AlternatingComponent


@dataclass(frozen=True, slots=True)
class OrbitTopologySignature:
    """How the first move changes the selected cross-color component."""

    first_role_color: int
    cross_colors: tuple[int, int]
    cross_root: int
    relation_before_first_move: CrossComponentRelation
    relation_after_first_move: CrossComponentRelation
    cross_component_before: AlternatingComponent
    first_role_color_edges: tuple[Edge, ...]
    intersection_edges: tuple[Edge, ...]


@dataclass(frozen=True, slots=True)
class TwoSwapOrbitExit:
    """Two legal full-component swaps followed by a verified direct fill."""

    moves: tuple[KempeMove, KempeMove]
    topology: OrbitTopologySignature
    fill_color: int
    completion_certificate: TotalColoringCertificate


@dataclass(frozen=True, slots=True)
class PairedHoleVerification:
    """Complete fail-closed semantic result for one input state."""

    status: PairedHoleStatus
    state_fingerprint: str | None = None
    issues: tuple[PairedHoleIssue, ...] = ()
    missing_colors: tuple[tuple[int, ...], ...] = ()
    fans: tuple[FanClosureAnalysis, ...] = ()
    profile_alignment: ProfileAlignment | None = None
    alpha_beta_blockages: tuple[AlphaBetaBlockage, ...] = ()
    terminal_release_witnesses: tuple[TerminalReleaseWitness, ...] = ()
    cross_components: tuple[CrossComponentAnalysis, ...] = ()
    verified_exits: tuple[CrossSwapAttempt, ...] = ()
    cross_terminal_release_exits: tuple[CrossTerminalReleaseExit, ...] = ()
    two_swap_orbit_exit: TwoSwapOrbitExit | None = None

    @property
    def has_verified_exit(self) -> bool:
        return self.status in {
            PairedHoleStatus.VERIFIED_ONE_SWAP_EXIT,
            PairedHoleStatus.VERIFIED_CROSS_TERMINAL_RELEASE_EXIT,
            PairedHoleStatus.VERIFIED_TWO_SWAP_ORBIT_EXIT,
        }


@dataclass(frozen=True, slots=True)
class _Component:
    vertices: tuple[int, ...]
    edge_indices: tuple[int, ...]


def terminal_degree_lower_bound(
    degree_parameter: int,
    path_length: int,
    penultimate_uncolored_incidence: int,
    terminal_uncolored_incidence: int,
) -> int:
    """Return the disjoint-missing-set terminal bound.

    A one-edge blocked path excludes only alpha from both missing sets and has
    lower bound ``R + 1 + h(s) + h(t)``.  A path of at least two edges also
    excludes beta at the penultimate vertex and has the stronger ``R + 2``
    base.  This distinction prevents applying the longer-path bound to a
    single terminal edge.
    """

    degree = _require_nonnegative_int(degree_parameter, name="degree_parameter")
    length = _require_nonnegative_int(path_length, name="path_length")
    left_h = _require_nonnegative_int(
        penultimate_uncolored_incidence, name="penultimate_uncolored_incidence"
    )
    right_h = _require_nonnegative_int(
        terminal_uncolored_incidence, name="terminal_uncolored_incidence"
    )
    if length == 0:
        raise PairedHoleFormatError("path_length must be positive")
    return degree + (1 if length == 1 else 2) + left_h + right_h


def terminal_release_is_applicable(path_length: int, common_release_colors: Iterable[int]) -> bool:
    """Whether the final-alpha-edge recoloring/prefix swap is defined.

    Besides a common terminal missing color, the blocked alternating path must
    contain a preceding beta edge.  Since such a path starts and ends in alpha,
    this means at least three edges.  In particular, a common missing color on
    a single alpha edge is not advertised as a terminal-release witness.
    """

    length = _require_nonnegative_int(path_length, name="path_length")
    if length == 0:
        raise PairedHoleFormatError("path_length must be positive")
    if isinstance(common_release_colors, str | bytes):
        raise PairedHoleFormatError("common_release_colors must be an iterable of colors")
    try:
        colors = tuple(common_release_colors)
    except TypeError as exc:
        raise PairedHoleFormatError("common_release_colors must be an iterable of colors") from exc
    if any(isinstance(color, bool) or not isinstance(color, int) or color < 0 for color in colors):
        raise PairedHoleFormatError("common_release_colors must contain nonnegative integers")
    return length >= 3 and bool(colors)


def _edge_index(graph: SimpleGraph) -> dict[Edge, int]:
    return {edge: index for index, edge in enumerate(graph.edges)}


def _validity_issues(state: PairedHoleState) -> tuple[PairedHoleIssue, ...]:
    graph = state.graph
    issues: list[PairedHoleIssue] = []
    if len(state.vertex_colors) != graph.order:
        issues.append(
            PairedHoleIssue(
                "vertex_assignment_count",
                "vertex_colors",
                f"expected {graph.order} colors, got {len(state.vertex_colors)}",
            )
        )
    if len(state.edge_colors) != graph.size:
        issues.append(
            PairedHoleIssue(
                "edge_assignment_count",
                "edge_colors",
                f"expected {graph.size} entries, got {len(state.edge_colors)}",
            )
        )
    if state.uncolored_edge not in graph.edges:
        issues.append(
            PairedHoleIssue(
                "uncolored_edge_absent",
                "uncolored_edge",
                "the distinguished uncolored edge is not an edge of the graph",
            )
        )

    x, y = state.roles.x, state.roles.y
    role_vertices = (
        x,
        y,
        *state.roles.x_fan_satellites,
        *state.roles.y_fan_satellites,
    )
    for index, vertex in enumerate(role_vertices):
        if vertex >= graph.order:
            issues.append(
                PairedHoleIssue(
                    "role_vertex_out_of_range",
                    f"roles.vertices[{index}]",
                    f"vertex {vertex} is outside 0..{graph.order - 1}",
                )
            )
    if len(set(role_vertices)) != len(role_vertices):
        issues.append(
            PairedHoleIssue(
                "role_vertices_not_distinct",
                "roles",
                "x, y, and the four satellite pointers must be distinct",
            )
        )
    if x < graph.order and y < graph.order and {x, y} != set(state.uncolored_edge):
        issues.append(
            PairedHoleIssue(
                "endpoint_role_mismatch",
                "roles",
                "roles.x and roles.y must be the endpoints of uncolored_edge",
            )
        )

    if len(state.edge_colors) == graph.size:
        null_indices = tuple(
            index for index, color in enumerate(state.edge_colors) if color is None
        )
        expected_index = _edge_index(graph).get(state.uncolored_edge)
        if expected_index is None or null_indices != (expected_index,):
            issues.append(
                PairedHoleIssue(
                    "uncolored_edge_set",
                    "edge_colors",
                    "exactly the distinguished edge must have null color",
                )
            )

    if len(state.vertex_colors) != graph.order or len(state.edge_colors) != graph.size:
        return tuple(issues)

    for edge_index, (left, right) in enumerate(graph.edges):
        if state.vertex_colors[left] == state.vertex_colors[right]:
            issues.append(
                PairedHoleIssue(
                    "adjacent_vertices_same_color",
                    f"graph.edges[{edge_index}]",
                    f"adjacent vertices {left} and {right} share a color",
                )
            )
        color = state.edge_colors[edge_index]
        if color is None:
            continue
        issues.extend(
            PairedHoleIssue(
                "incident_vertex_edge_same_color",
                f"edge_colors[{edge_index}]",
                f"edge {(left, right)} shares a color with endpoint {endpoint}",
            )
            for endpoint in (left, right)
            if color == state.vertex_colors[endpoint]
        )

    first_by_color: list[dict[int, int]] = [dict() for _ in range(graph.order)]
    for edge_index, (left, right) in enumerate(graph.edges):
        color = state.edge_colors[edge_index]
        if color is None:
            continue
        for endpoint in (left, right):
            previous = first_by_color[endpoint].get(color)
            if previous is None:
                first_by_color[endpoint][color] = edge_index
            else:
                issues.append(
                    PairedHoleIssue(
                        "adjacent_edges_same_color",
                        f"edge_colors[{edge_index}]",
                        f"edges {graph.edges[previous]} and {(left, right)} share color {color}",
                    )
                )
    return tuple(issues)


def _missing_colors(state: PairedHoleState) -> tuple[frozenset[int], ...]:
    used = [{state.vertex_colors[vertex]} for vertex in range(state.graph.order)]
    for edge_index, (left, right) in enumerate(state.graph.edges):
        color = state.edge_colors[edge_index]
        if color is not None:
            used[left].add(color)
            used[right].add(color)
    palette = set(range(state.palette_size))
    return tuple(frozenset(palette - row) for row in used)


def _uncolored_incidence(state: PairedHoleState) -> tuple[int, ...]:
    counts = [0] * state.graph.order
    for index, (left, right) in enumerate(state.graph.edges):
        if state.edge_colors[index] is None:
            counts[left] += 1
            counts[right] += 1
    return tuple(counts)


def _full_fan_closure(
    state: PairedHoleState,
    missing: tuple[frozenset[int], ...],
    *,
    center: int,
    root: int,
) -> FanClosureAnalysis:
    edge_indices = _edge_index(state.graph)
    closure = {root}
    changed = True
    while changed:
        changed = False
        available = set().union(*(missing[vertex] for vertex in closure))
        for neighbor in sorted(state.graph.neighbors(center)):
            edge = (center, neighbor) if center < neighbor else (neighbor, center)
            color = state.edge_colors[edge_indices[edge]]
            if color is not None and color in available and neighbor not in closure:
                closure.add(neighbor)
                changed = True

    satellites = tuple(sorted(closure - {root}))
    colored_satellites = tuple(
        sorted(
            (
                satellite,
                cast(
                    int,
                    state.edge_colors[
                        edge_indices[
                            (center, satellite) if center < satellite else (satellite, center)
                        ]
                    ],
                ),
            )
            for satellite in satellites
        )
    )
    degree = state.degree_parameter
    surplus = degree - state.graph.degree(root)
    surplus += sum(degree - 1 - state.graph.degree(vertex) for vertex in satellites)
    return FanClosureAnalysis(
        center=center,
        root=root,
        satellites=satellites,
        closure=tuple(sorted(closure)),
        satellite_edge_colors=colored_satellites,
        surplus=surplus,
    )


def _scope_issues(
    state: PairedHoleState,
    missing: tuple[frozenset[int], ...],
) -> tuple[list[PairedHoleIssue], tuple[FanClosureAnalysis, FanClosureAnalysis]]:
    graph = state.graph
    x, y = state.roles.x, state.roles.y
    issues: list[PairedHoleIssue] = []
    if state.palette_size != state.degree_parameter + 2:
        issues.append(
            PairedHoleIssue(
                "palette_not_r_plus_two",
                "palette_size",
                "the exact residue requires palette_size = degree_parameter + 2",
            )
        )
    if graph.max_degree != state.degree_parameter:
        issues.append(
            PairedHoleIssue(
                "maximum_degree_mismatch",
                "degree_parameter",
                f"graph maximum degree is {graph.max_degree}",
            )
        )
    if graph.degree(x) != state.degree_parameter or graph.degree(y) != state.degree_parameter:
        issues.append(
            PairedHoleIssue(
                "hole_edge_not_in_degree_core",
                "uncolored_edge",
                "both uncolored-edge endpoints must have degree R",
            )
        )
    core = {
        vertex for vertex in range(graph.order) if graph.degree(vertex) == state.degree_parameter
    }
    if any(len(graph.neighbors(vertex) & core) > 1 for vertex in core):
        issues.append(
            PairedHoleIssue(
                "degree_core_not_matching",
                "graph",
                "the degree-R core must induce a matching plus isolated vertices",
            )
        )
    if state.alpha in state.vertex_colors:
        issues.append(
            PairedHoleIssue(
                "alpha_used_on_vertex",
                "alpha",
                "alpha must be unused by the fixed vertex coloring",
            )
        )
    oversized = sorted(color for color, count in Counter(state.vertex_colors).items() if count > 2)
    if oversized:
        issues.append(
            PairedHoleIssue(
                "vertex_color_class_too_large",
                "vertex_colors",
                f"fixed vertex color classes exceed size two for colors {oversized}",
            )
        )

    x_fan = _full_fan_closure(state, missing, center=x, root=y)
    y_fan = _full_fan_closure(state, missing, center=y, root=x)
    for name, fan, claimed in (
        ("x", x_fan, state.roles.x_fan_satellites),
        ("y", y_fan, state.roles.y_fan_satellites),
    ):
        if len(fan.satellites) != 2:
            issues.append(
                PairedHoleIssue(
                    "fan_closure_not_q_two",
                    f"roles.{name}_fan_satellites",
                    f"derived full fan closure has {len(fan.satellites)} satellites",
                )
            )
        elif fan.satellites != claimed:
            issues.append(
                PairedHoleIssue(
                    "fan_role_pointer_mismatch",
                    f"roles.{name}_fan_satellites",
                    f"claimed {claimed}, derived {fan.satellites}",
                )
            )
        if fan.surplus != 0:
            issues.append(
                PairedHoleIssue(
                    "fan_surplus_not_zero",
                    f"roles.{name}_fan_satellites",
                    f"derived weighted fan surplus is {fan.surplus}",
                )
            )

    if len(missing[x]) != 2 or len(missing[y]) != 2:
        issues.append(
            PairedHoleIssue(
                "endpoint_missing_sets_not_pairs",
                "uncolored_edge",
                "both endpoint missing-color sets must have size two",
            )
        )
        return issues, (x_fan, y_fan)

    a_set = missing[x]
    b_set = missing[y]
    p = state.vertex_colors[x]
    q = state.vertex_colors[y]
    role_sets = (a_set, b_set, frozenset({p}), frozenset({q}), frozenset({state.alpha}))
    if any(role_sets[left] & role_sets[right] for left in range(5) for right in range(left)):
        issues.append(
            PairedHoleIssue(
                "color_roles_not_pairwise_disjoint",
                "vertex_colors",
                "A, B, p, q, and alpha must be pairwise disjoint",
            )
        )
    if state.degree_parameter < 5:
        issues.append(
            PairedHoleIssue(
                "degree_parameter_below_two_sided_minimum",
                "degree_parameter",
                "seven distinct color roles force R at least five",
            )
        )

    edge_indices = _edge_index(graph)
    for name, fan, expected_edge_colors, center_color in (
        ("x", x_fan, b_set, p),
        ("y", y_fan, a_set, q),
    ):
        actual_edge_colors = frozenset(color for _vertex, color in fan.satellite_edge_colors)
        if actual_edge_colors != expected_edge_colors:
            issues.append(
                PairedHoleIssue(
                    "fan_edge_color_identity",
                    f"roles.{name}_fan_satellites",
                    f"derived satellite edge colors {sorted(actual_edge_colors)} do not match "
                    f"the root missing set {sorted(expected_edge_colors)}",
                )
            )
        for satellite in fan.satellites:
            edge = tuple(sorted((fan.center, satellite)))
            edge_color = state.edge_colors[edge_indices[cast(Edge, edge)]]
            assert edge_color is not None
            expected_missing = frozenset({center_color}) | (expected_edge_colors - {edge_color})
            if missing[satellite] != expected_missing:
                issues.append(
                    PairedHoleIssue(
                        "satellite_missing_profile",
                        f"vertex_colors[{satellite}]",
                        f"derived missing set {sorted(missing[satellite])}, expected "
                        f"{sorted(expected_missing)}",
                    )
                )
    return issues, (x_fan, y_fan)


def _component(state: PairedHoleState, *, first: int, second: int, start: int) -> _Component:
    allowed = {first, second}
    adjacency: list[list[tuple[int, int]]] = [[] for _ in range(state.graph.order)]
    for edge_index, (left, right) in enumerate(state.graph.edges):
        if state.edge_colors[edge_index] in allowed:
            adjacency[left].append((right, edge_index))
            adjacency[right].append((left, edge_index))
    pending = [start]
    vertices = {start}
    edge_indices: set[int] = set()
    while pending:
        vertex = pending.pop()
        for neighbor, edge_index in adjacency[vertex]:
            edge_indices.add(edge_index)
            if neighbor not in vertices:
                vertices.add(neighbor)
                pending.append(neighbor)
    return _Component(tuple(sorted(vertices)), tuple(sorted(edge_indices)))


def _public_component(
    state: PairedHoleState, component: _Component, colors: tuple[int, int]
) -> AlternatingComponent:
    return AlternatingComponent(
        colors=colors,
        vertices=component.vertices,
        edge_indices=component.edge_indices,
        edges=tuple(state.graph.edges[index] for index in component.edge_indices),
        walk=_canonical_component_walk(state, component),
    )


def _canonical_component_walk(state: PairedHoleState, component: _Component) -> tuple[int, ...]:
    """Return a deterministic path/cycle walk for a proper two-color component."""

    adjacency: dict[int, list[int]] = {vertex: [] for vertex in component.vertices}
    for edge_index in component.edge_indices:
        left, right = state.graph.edges[edge_index]
        adjacency[left].append(right)
        adjacency[right].append(left)
    for neighbors in adjacency.values():
        neighbors.sort()
        if len(neighbors) > 2:  # pragma: no cover - valid partial colorings exclude this
            raise AssertionError("alternating component has degree greater than two")
    if not component.edge_indices:
        return component.vertices

    def walk(start: int, first_neighbor: int | None = None) -> tuple[int, ...]:
        vertices = [start]
        previous: int | None = None
        current = start
        forced = first_neighbor
        while True:
            options = [neighbor for neighbor in adjacency[current] if neighbor != previous]
            if forced is not None:
                if forced not in options:  # pragma: no cover - caller selects a neighbor
                    raise AssertionError("forced walk neighbor is not incident")
                neighbor = forced
                forced = None
            elif not options:
                break
            else:
                neighbor = options[0]
            vertices.append(neighbor)
            previous, current = current, neighbor
            if current == start:
                break
        return tuple(vertices)

    endpoints = sorted(vertex for vertex, neighbors in adjacency.items() if len(neighbors) == 1)
    if endpoints:
        return walk(endpoints[0])
    start = min(component.vertices)
    cycle_walks = tuple(walk(start, neighbor) for neighbor in adjacency[start])
    return min(cycle_walks)


def _path_from_start(
    state: PairedHoleState, component: _Component, *, start: int
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    adjacency: dict[int, list[tuple[int, int]]] = {vertex: [] for vertex in component.vertices}
    for edge_index in component.edge_indices:
        left, right = state.graph.edges[edge_index]
        adjacency[left].append((right, edge_index))
        adjacency[right].append((left, edge_index))
    if any(len(row) > 2 for row in adjacency.values()) or len(adjacency[start]) > 1:
        return None
    vertices = [start]
    edge_indices: list[int] = []
    previous: int | None = None
    current = start
    while True:
        options = [item for item in adjacency[current] if item[0] != previous]
        if not options:
            break
        if len(options) != 1:
            return None
        neighbor, edge_index = options[0]
        edge_indices.append(edge_index)
        vertices.append(neighbor)
        previous, current = current, neighbor
    if len(edge_indices) != len(component.edge_indices):
        return None
    return tuple(vertices), tuple(edge_indices)


def _derive_blockages(
    state: PairedHoleState,
    missing: tuple[frozenset[int], ...],
    fans: tuple[FanClosureAnalysis, FanClosureAnalysis],
) -> tuple[tuple[AlphaBetaBlockage, ...], list[PairedHoleIssue]]:
    x, y = state.roles.x, state.roles.y
    special_vertices = {x, y, *fans[0].satellites, *fans[1].satellites}
    a_set = missing[x]
    b_set = missing[y]
    p = state.vertex_colors[x]
    q = state.vertex_colors[y]
    betas = tuple(sorted({p, q, *a_set, *b_set}))
    issues: list[PairedHoleIssue] = []
    analyses: list[AlphaBetaBlockage] = []
    uncolored_incidence = _uncolored_incidence(state)

    for beta in betas:
        holes = tuple(sorted(vertex for vertex in special_vertices if beta in missing[vertex]))
        if len(holes) != 2:
            issues.append(
                PairedHoleIssue(
                    "beta_hole_count",
                    f"colors[{beta}]",
                    f"expected two derived special-vertex holes, got {len(holes)}",
                )
            )
            continue
        components_for_beta: list[_Component] = []
        for hole in holes:
            component = _component(state, first=state.alpha, second=beta, start=hole)
            components_for_beta.append(component)
            path = _path_from_start(state, component, start=hole)
            blocked = False
            terminal_analysis: TerminalLockAnalysis | None = None
            path_vertices: tuple[int, ...] = ()
            path_edges: tuple[int, ...] = ()
            if path is not None:
                path_vertices, path_edges = path
                if path_edges:
                    colors = tuple(state.edge_colors[index] for index in path_edges)
                    expected = tuple(
                        state.alpha if index % 2 == 0 else beta for index in range(len(path_edges))
                    )
                    terminal_vertex = path_vertices[-1]
                    blocked = colors == expected and state.vertex_colors[terminal_vertex] == beta
                    if blocked:
                        penultimate = path_vertices[-2]
                        common = tuple(sorted(missing[penultimate] & missing[terminal_vertex]))
                        lower_bound = terminal_degree_lower_bound(
                            state.degree_parameter,
                            len(path_edges),
                            uncolored_incidence[penultimate],
                            uncolored_incidence[terminal_vertex],
                        )
                        degree_sum = state.graph.degree(penultimate) + state.graph.degree(
                            terminal_vertex
                        )
                        terminal_analysis = TerminalLockAnalysis(
                            path_length=len(path_edges),
                            penultimate=penultimate,
                            terminal=terminal_vertex,
                            common_release_colors=common,
                            terminal_release_applicable=terminal_release_is_applicable(
                                len(path_edges), common
                            ),
                            uncolored_incidence=(
                                uncolored_incidence[penultimate],
                                uncolored_incidence[terminal_vertex],
                            ),
                            degree_sum=degree_sum,
                            lock_degree_lower_bound=lower_bound,
                            bound_applies=not common,
                            bound_satisfied=degree_sum >= lower_bound if not common else None,
                        )
                        if (
                            terminal_analysis.bound_applies
                            and not terminal_analysis.bound_satisfied
                        ):
                            issues.append(
                                PairedHoleIssue(
                                    "terminal_lock_bound_violation",
                                    f"colors[{beta}]",
                                    "derived disjoint terminal missing sets violate their "
                                    "degree lower bound",
                                )
                            )
            analyses.append(
                AlphaBetaBlockage(
                    beta=beta,
                    hole=hole,
                    component=_public_component(state, component, colors=(state.alpha, beta)),
                    path_vertices=path_vertices,
                    path_edge_indices=path_edges,
                    blocked_by_fixed_beta=blocked,
                    terminal=terminal_analysis,
                )
            )
        if components_for_beta[0] == components_for_beta[1]:
            issues.append(
                PairedHoleIssue(
                    "alpha_beta_holes_linked",
                    f"colors[{beta}]",
                    "the two beta holes lie in the same alpha-beta component",
                )
            )

    analyses.sort(key=lambda item: (item.beta, item.hole))
    if len(analyses) != 12 or any(not item.blocked_by_fixed_beta for item in analyses):
        issues.append(
            PairedHoleIssue(
                "alpha_beta_blockage_incomplete",
                "edge_colors",
                "the exact residue requires all twelve derived alpha-beta paths to be blocked",
            )
        )
    return tuple(analyses), issues


def _partial_assignment_issues(
    state: PairedHoleState, edge_colors: Sequence[int | None]
) -> tuple[str, ...]:
    issues: list[str] = []
    first_by_color: list[dict[int, int]] = [dict() for _ in range(state.graph.order)]
    for edge_index, (left, right) in enumerate(state.graph.edges):
        color = edge_colors[edge_index]
        if color is None:
            continue
        if color == state.vertex_colors[left] or color == state.vertex_colors[right]:
            issues.append(f"incident_vertex_edge_same_color:edge_colors[{edge_index}]")
        for endpoint in (left, right):
            previous = first_by_color[endpoint].get(color)
            if previous is None:
                first_by_color[endpoint][color] = edge_index
            else:
                issues.append(f"adjacent_edges_same_color:edge_colors[{edge_index}]")
    return tuple(issues)


def _swapped_assignment(
    state: PairedHoleState,
    component: _Component,
    *,
    first: int,
    second: int,
) -> tuple[tuple[int | None, ...], tuple[str, ...]]:
    swapped = list(state.edge_colors)
    for edge_index in component.edge_indices:
        color = swapped[edge_index]
        if color == first:
            swapped[edge_index] = second
        elif color == second:
            swapped[edge_index] = first
        else:  # pragma: no cover - component construction guarantees this
            raise AssertionError("component contains an edge of a third color")
    frozen = tuple(swapped)
    return frozen, _partial_assignment_issues(state, frozen)


def _missing_for_assignment(
    state: PairedHoleState, edge_colors: Sequence[int | None], vertex: int
) -> frozenset[int]:
    used = {state.vertex_colors[vertex]}
    for index, edge in enumerate(state.graph.edges):
        if vertex in edge and edge_colors[index] is not None:
            used.add(cast(int, edge_colors[index]))
    return frozenset(set(range(state.palette_size)) - used)


def _swap_and_fill(
    state: PairedHoleState,
    component: _Component,
    *,
    first: int,
    second: int,
    side: str,
    fill_color: int,
) -> CrossSwapAttempt:
    frozen_swapped, swap_issues = _swapped_assignment(state, component, first=first, second=second)
    swapped = list(frozen_swapped)
    x, y = state.roles.x, state.roles.y
    common_hole = fill_color in _missing_for_assignment(
        state, swapped, x
    ) and fill_color in _missing_for_assignment(state, swapped, y)
    certificate: TotalColoringCertificate | None = None
    issues = list(swap_issues)
    uncolored_index = _edge_index(state.graph)[state.uncolored_edge]
    if not common_hole:
        issues.append("fill_color_not_missing_at_both_endpoints")
    if not issues:
        swapped[uncolored_index] = fill_color
        if any(color is None for color in swapped):  # pragma: no cover - exact one-hole validity
            issues.append("completion_still_contains_uncolored_edge")
        else:
            certificate = TotalColoringCertificate.create(
                state.graph,
                state.palette_size,
                state.vertex_colors,
                cast(Sequence[int], swapped),
            )
            certificate_issues = certificate.verify(state.graph).issues
            issues.extend(f"certificate:{issue.code}:{issue.path}" for issue in certificate_issues)
            if certificate_issues:
                certificate = None
    return CrossSwapAttempt(
        side=side,
        swapped_component=_public_component(state, component, colors=(first, second)),
        fill_color=fill_color,
        swap_is_proper=not swap_issues,
        common_hole_after_swap=common_hole,
        completion_certificate=certificate,
        issues=tuple(issues),
    )


def _cross_terminal_analysis(
    state: PairedHoleState,
    component: _Component,
    *,
    a: int,
    b: int,
    side: str,
    require_blocked: bool,
) -> tuple[CrossRootTerminalAnalysis, tuple[PairedHoleIssue, ...]]:
    root = state.roles.x if side == "x" else state.roles.y
    fill_color = b if side == "x" else a
    first_color = b if side == "x" else a
    second_color = a if side == "x" else b
    path = _path_from_start(state, component, start=root)
    issues: list[PairedHoleIssue] = []
    if path is None:
        return (
            CrossRootTerminalAnalysis(side, root, (), (), False, None, ()),
            (
                PairedHoleIssue(
                    "cross_root_component_not_path",
                    f"cross_components[{a},{b}].{side}",
                    "the full root component is not a path from its missing-color endpoint",
                ),
            ),
        )
    path_vertices, path_indices = path
    path_edges = tuple(state.graph.edges[index] for index in path_indices)
    expected_colors = tuple(
        first_color if index % 2 == 0 else second_color for index in range(len(path_indices))
    )
    actual_colors = tuple(state.edge_colors[index] for index in path_indices)
    blocked = False
    terminal: TerminalLockAnalysis | None = None
    releases: list[CrossTerminalReleaseExit] = []
    if path_indices and actual_colors == expected_colors:
        terminal_vertex = path_vertices[-1]
        last_color = cast(int, actual_colors[-1])
        swapped_last_color = a if last_color == b else b
        blocked = state.vertex_colors[terminal_vertex] == swapped_last_color
        if blocked:
            penultimate = path_vertices[-2]
            missing = _missing_colors(state)
            common = tuple(sorted(missing[penultimate] & missing[terminal_vertex]))
            uncolored = _uncolored_incidence(state)
            lower_bound = terminal_degree_lower_bound(
                state.degree_parameter,
                len(path_indices),
                uncolored[penultimate],
                uncolored[terminal_vertex],
            )
            degree_sum = state.graph.degree(penultimate) + state.graph.degree(terminal_vertex)
            release_applicable = len(path_indices) >= 2 and bool(common)
            terminal = TerminalLockAnalysis(
                path_length=len(path_indices),
                penultimate=penultimate,
                terminal=terminal_vertex,
                common_release_colors=common,
                terminal_release_applicable=release_applicable,
                uncolored_incidence=(uncolored[penultimate], uncolored[terminal_vertex]),
                degree_sum=degree_sum,
                lock_degree_lower_bound=lower_bound,
                bound_applies=not common,
                bound_satisfied=degree_sum >= lower_bound if not common else None,
            )
            if terminal.bound_applies and not terminal.bound_satisfied:
                issues.append(
                    PairedHoleIssue(
                        "cross_terminal_lock_bound_violation",
                        f"cross_components[{a},{b}].{side}",
                        "derived disjoint cross-terminal missing sets violate their degree "
                        "lower bound",
                    )
                )
            if release_applicable:
                terminal_edge_index = path_indices[-1]
                prefix_indices = path_indices[:-1]
                for release_color in common:
                    released = list(state.edge_colors)
                    released[terminal_edge_index] = release_color
                    for edge_index in prefix_indices:
                        color = released[edge_index]
                        if color == a:
                            released[edge_index] = b
                        elif color == b:
                            released[edge_index] = a
                        else:  # pragma: no cover - path construction guarantees this
                            raise AssertionError(
                                "cross terminal-release prefix contains a third color"
                            )
                    frozen = tuple(released)
                    replay_issues = _partial_assignment_issues(state, frozen)
                    common_after = fill_color in _missing_for_assignment(
                        state, frozen, state.roles.x
                    ) and fill_color in _missing_for_assignment(state, frozen, state.roles.y)
                    if replay_issues or not common_after:
                        detail = (
                            ", ".join(replay_issues)
                            if replay_issues
                            else "fill color was not released at both endpoints"
                        )
                        issues.append(
                            PairedHoleIssue(
                                "cross_terminal_release_move_invalid",
                                f"cross_components[{a},{b}].{side}",
                                f"replayed cross terminal release failed: {detail}",
                            )
                        )
                        continue
                    partial = _state_with_edge_colors(state, frozen)
                    certificate = _completion_with_fill(partial, fill_color)
                    if certificate is None:
                        issues.append(
                            PairedHoleIssue(
                                "cross_terminal_release_completion_invalid",
                                f"cross_components[{a},{b}].{side}",
                                "the replayed release did not yield its prescribed fill "
                                "certificate",
                            )
                        )
                        continue
                    releases.append(
                        CrossTerminalReleaseExit(
                            a=a,
                            b=b,
                            side=side,
                            hole=root,
                            path_vertices=path_vertices,
                            path_edges=path_edges,
                            release_color=release_color,
                            fill_color=fill_color,
                            resulting_partial_fingerprint=sha256_hex(list(frozen)),
                            completion_certificate=certificate,
                        )
                    )
    if require_blocked and not blocked:
        issues.append(
            PairedHoleIssue(
                "cross_root_terminal_not_blocked",
                f"cross_components[{a},{b}].{side}",
                "a failed direct cross swap was not explained by a fixed-color path terminal",
            )
        )
    return (
        CrossRootTerminalAnalysis(
            side=side,
            hole=root,
            path_vertices=path_vertices,
            path_edges=path_edges,
            blocked_by_fixed_color=blocked,
            terminal=terminal,
            releases=tuple(releases),
        ),
        tuple(issues),
    )


def _state_with_edge_colors(
    state: PairedHoleState, edge_colors: Iterable[int | None]
) -> PairedHoleState:
    return PairedHoleState.create(
        graph=state.graph,
        degree_parameter=state.degree_parameter,
        palette_size=state.palette_size,
        vertex_colors=state.vertex_colors,
        edge_colors=edge_colors,
        uncolored_edge=state.uncolored_edge,
        alpha=state.alpha,
        roles=state.roles,
    )


def _completion_from_common_hole(
    state: PairedHoleState,
) -> tuple[int, TotalColoringCertificate] | None:
    x, y = state.roles.x, state.roles.y
    common = sorted(
        _missing_for_assignment(state, state.edge_colors, x)
        & _missing_for_assignment(state, state.edge_colors, y)
    )
    for fill_color in common:
        certificate = _completion_with_fill(state, fill_color)
        if certificate is not None:
            return fill_color, certificate
    return None


def _completion_with_fill(
    state: PairedHoleState, fill_color: int
) -> TotalColoringCertificate | None:
    x, y = state.roles.x, state.roles.y
    if fill_color not in _missing_for_assignment(
        state, state.edge_colors, x
    ) or fill_color not in _missing_for_assignment(state, state.edge_colors, y):
        return None
    completed = list(state.edge_colors)
    completed[_edge_index(state.graph)[state.uncolored_edge]] = fill_color
    if any(color is None for color in completed):
        return None
    certificate = TotalColoringCertificate.create(
        state.graph,
        state.palette_size,
        state.vertex_colors,
        cast(Sequence[int], completed),
    )
    return certificate if certificate.verify(state.graph).valid else None


def _derive_cross_components(
    state: PairedHoleState,
    missing: tuple[frozenset[int], ...],
) -> tuple[
    tuple[CrossComponentAnalysis, ...],
    tuple[CrossSwapAttempt, ...],
    tuple[CrossTerminalReleaseExit, ...],
    tuple[PairedHoleIssue, ...],
]:
    x, y = state.roles.x, state.roles.y
    analyses: list[CrossComponentAnalysis] = []
    exits: list[CrossSwapAttempt] = []
    terminal_exits: list[CrossTerminalReleaseExit] = []
    issues: list[PairedHoleIssue] = []
    for a in sorted(missing[x]):
        for b in sorted(missing[y]):
            x_component = _component(state, first=a, second=b, start=x)
            y_component = _component(state, first=a, second=b, start=y)
            linked = _component_relation(x_component, y_component) is CrossComponentRelation.LINKED
            attempts: tuple[CrossSwapAttempt, ...] = ()
            root_terminal_analyses: tuple[CrossRootTerminalAnalysis, ...] = ()
            if not linked:
                attempts = (
                    _swap_and_fill(
                        state,
                        x_component,
                        first=a,
                        second=b,
                        side="x",
                        fill_color=b,
                    ),
                    _swap_and_fill(
                        state,
                        y_component,
                        first=a,
                        second=b,
                        side="y",
                        fill_color=a,
                    ),
                )
                exits.extend(attempt for attempt in attempts if attempt.verified)
                derived_terminal_analyses: list[CrossRootTerminalAnalysis] = []
                for side, component, attempt in (
                    ("x", x_component, attempts[0]),
                    ("y", y_component, attempts[1]),
                ):
                    terminal_analysis, terminal_issues = _cross_terminal_analysis(
                        state,
                        component,
                        a=a,
                        b=b,
                        side=side,
                        require_blocked=not attempt.verified,
                    )
                    derived_terminal_analyses.append(terminal_analysis)
                    issues.extend(terminal_issues)
                    terminal_exits.extend(terminal_analysis.releases)
                root_terminal_analyses = tuple(derived_terminal_analyses)
            analyses.append(
                CrossComponentAnalysis(
                    a=a,
                    b=b,
                    relation=(
                        CrossComponentRelation.LINKED if linked else CrossComponentRelation.DISTINCT
                    ),
                    x_component=_public_component(state, x_component, colors=(a, b)),
                    y_component=_public_component(state, y_component, colors=(a, b)),
                    attempts=attempts,
                    root_terminal_analyses=root_terminal_analyses,
                )
            )
    return tuple(analyses), tuple(exits), tuple(terminal_exits), tuple(issues)


def _component_relation(first: _Component, second: _Component) -> CrossComponentRelation:
    return CrossComponentRelation.LINKED if first == second else CrossComponentRelation.DISTINCT


def _unique_components_meeting(
    state: PairedHoleState,
    *,
    first: int,
    second: int,
    vertices: Iterable[int],
) -> tuple[_Component, ...]:
    by_edges: dict[tuple[int, ...], _Component] = {}
    for vertex in sorted(set(vertices)):
        component = _component(state, first=first, second=second, start=vertex)
        if component.edge_indices:
            by_edges.setdefault(component.edge_indices, component)
    return tuple(by_edges[key] for key in sorted(by_edges))


def _derive_two_swap_orbit_exit(
    state: PairedHoleState,
    missing: tuple[frozenset[int], ...],
) -> TwoSwapOrbitExit | None:
    """Search the exact bounded two-move orbit relevant to the residue.

    The first move ranges over every full two-role-color component in the
    graph.  Restricting it to alpha components would miss a cross-cross
    detachment, restricting it to alpha and A x B pairs would miss a
    vertex-role--hole-role detachment, and restricting it to the six fan-role
    vertices would miss an outside component that intersects and detaches a
    linked cross path.
    The second is a full A x B component meeting x or y.  The retained
    bounded orbit requires the two moves to share exactly one role color;
    other two-move patterns remain outside this verifier's documented scope.
    Every intermediate partial coloring and final certificate is checked from
    scratch.  Exhaustion of this deliberately bounded orbit is not a negative
    proof and leads only to ``fully_blocked_candidate``.
    """

    x, y = state.roles.x, state.roles.y
    a_set = tuple(sorted(missing[x]))
    b_set = tuple(sorted(missing[y]))
    role_colors = tuple(
        sorted(
            {
                state.vertex_colors[x],
                state.vertex_colors[y],
                *a_set,
                *b_set,
            }
        )
    )
    cross_pairs = tuple((a, b) for a in a_set for b in b_set)
    preferred_first_pairs = (
        tuple((state.alpha, beta) for beta in role_colors if beta != state.alpha) + cross_pairs
    )
    preferred_first_pair_set = set(preferred_first_pairs)
    first_pairs = preferred_first_pairs + tuple(
        pair for pair in combinations(role_colors, 2) if pair not in preferred_first_pair_set
    )
    for first, second in first_pairs:
        for first_component in _unique_components_meeting(
            state,
            first=first,
            second=second,
            vertices=range(state.graph.order),
        ):
            first_colors, first_issues = _swapped_assignment(
                state,
                first_component,
                first=first,
                second=second,
            )
            if first_issues:
                continue
            intermediate = _state_with_edge_colors(state, first_colors)
            for a, b in cross_pairs:
                shared_colors = tuple(sorted({first, second} & {a, b}))
                if len(shared_colors) != 1:
                    continue
                shared_color = shared_colors[0]
                second_components = _unique_components_meeting(
                    intermediate,
                    first=a,
                    second=b,
                    vertices=(x, y),
                )
                for second_component in second_components:
                    second_colors, second_issues = _swapped_assignment(
                        intermediate,
                        second_component,
                        first=a,
                        second=b,
                    )
                    if second_issues:
                        continue
                    final_partial = _state_with_edge_colors(state, second_colors)
                    completion = _completion_from_common_hole(final_partial)
                    if completion is None:
                        continue
                    fill_color, certificate = completion
                    x_before = _component(state, first=a, second=b, start=x)
                    y_before = _component(state, first=a, second=b, start=y)
                    x_after = _component(intermediate, first=a, second=b, start=x)
                    y_after = _component(intermediate, first=a, second=b, start=y)
                    cross_root = x if x in second_component.vertices else y
                    cross_before = x_before if cross_root == x else y_before
                    first_role_color_indices = frozenset(
                        edge_index
                        for edge_index in first_component.edge_indices
                        if state.edge_colors[edge_index] == shared_color
                    )
                    intersection_indices = first_role_color_indices & frozenset(
                        cross_before.edge_indices
                    )
                    return TwoSwapOrbitExit(
                        moves=(
                            KempeMove(
                                colors=(first, second),
                                component=_public_component(
                                    state,
                                    first_component,
                                    colors=(first, second),
                                ),
                            ),
                            KempeMove(
                                colors=(a, b),
                                component=_public_component(
                                    intermediate,
                                    second_component,
                                    colors=(a, b),
                                ),
                            ),
                        ),
                        topology=OrbitTopologySignature(
                            first_role_color=shared_color,
                            cross_colors=(a, b),
                            cross_root=cross_root,
                            relation_before_first_move=_component_relation(x_before, y_before),
                            relation_after_first_move=_component_relation(x_after, y_after),
                            cross_component_before=_public_component(
                                state, cross_before, colors=(a, b)
                            ),
                            first_role_color_edges=tuple(
                                state.graph.edges[index]
                                for index in sorted(first_role_color_indices)
                            ),
                            intersection_edges=tuple(
                                state.graph.edges[index] for index in sorted(intersection_indices)
                            ),
                        ),
                        fill_color=fill_color,
                        completion_certificate=certificate,
                    )
    return None


def _profile_alignment(
    state: PairedHoleState,
    missing: tuple[frozenset[int], ...],
    fans: tuple[FanClosureAnalysis, FanClosureAnalysis],
) -> ProfileAlignment:
    x, y = state.roles.x, state.roles.y
    x_satellite_colors = {state.vertex_colors[vertex] for vertex in fans[0].satellites}
    y_satellite_colors = {state.vertex_colors[vertex] for vertex in fans[1].satellites}
    return (
        ProfileAlignment.ALIGNED
        if x_satellite_colors == missing[x] and y_satellite_colors == missing[y]
        else ProfileAlignment.NONALIGNED
    )


def _terminal_release_witnesses(
    state: PairedHoleState, blockages: Iterable[AlphaBetaBlockage]
) -> tuple[tuple[TerminalReleaseWitness, ...], tuple[PairedHoleIssue, ...]]:
    witnesses: list[TerminalReleaseWitness] = []
    issues: list[PairedHoleIssue] = []
    for blockage in blockages:
        terminal = blockage.terminal
        if terminal is None or not terminal.terminal_release_applicable:
            continue
        terminal_edge_index = blockage.path_edge_indices[-1]
        prefix_indices = blockage.path_edge_indices[:-1]
        for release_color in terminal.common_release_colors:
            released = list(state.edge_colors)
            if released[terminal_edge_index] != state.alpha:
                issues.append(
                    PairedHoleIssue(
                        "terminal_release_move_invalid",
                        f"colors[{blockage.beta}]",
                        "the derived terminal edge is not colored alpha",
                    )
                )
                continue
            released[terminal_edge_index] = release_color
            for edge_index in prefix_indices:
                color = released[edge_index]
                if color == state.alpha:
                    released[edge_index] = blockage.beta
                elif color == blockage.beta:
                    released[edge_index] = state.alpha
                else:  # pragma: no cover - blockage path construction guarantees this
                    raise AssertionError("terminal-release prefix contains a third color")
            frozen = tuple(released)
            move_issues = _partial_assignment_issues(state, frozen)
            alpha_released = state.alpha in _missing_for_assignment(state, frozen, blockage.hole)
            if move_issues or not alpha_released:
                detail = ", ".join(move_issues) if move_issues else "alpha was not released"
                issues.append(
                    PairedHoleIssue(
                        "terminal_release_move_invalid",
                        f"colors[{blockage.beta}]",
                        f"replayed terminal-release move failed: {detail}",
                    )
                )
                continue
            witnesses.append(
                TerminalReleaseWitness(
                    beta=blockage.beta,
                    hole=blockage.hole,
                    release_color=release_color,
                    terminal_edge=state.graph.edges[terminal_edge_index],
                    prefix_edges=tuple(state.graph.edges[index] for index in prefix_indices),
                    resulting_partial_fingerprint=sha256_hex(list(frozen)),
                )
            )
    return tuple(witnesses), tuple(issues)


def verify_paired_hole_state(state: PairedHoleState) -> PairedHoleVerification:
    """Verify one parsed state and classify it without invoking a solver.

    Invalid partial total colorings are separated from valid colorings outside
    this exact proof residue.  A positive result carries a freshly constructed
    :class:`TotalColoringCertificate` for every verified direct exit.
    """

    if not isinstance(state, PairedHoleState):
        raise TypeError("state must be a PairedHoleState")
    validity_issues = _validity_issues(state)
    if validity_issues:
        return PairedHoleVerification(
            status=PairedHoleStatus.INVALID_STATE,
            state_fingerprint=state.fingerprint,
            issues=validity_issues,
        )

    missing = _missing_colors(state)
    missing_public = tuple(tuple(sorted(colors)) for colors in missing)
    scope_issues, fans = _scope_issues(state, missing)
    pointer_issues = tuple(
        issue for issue in scope_issues if issue.code == "fan_role_pointer_mismatch"
    )
    if pointer_issues:
        return PairedHoleVerification(
            status=PairedHoleStatus.INVALID_STATE,
            state_fingerprint=state.fingerprint,
            issues=pointer_issues,
            missing_colors=missing_public,
            fans=fans,
        )
    if scope_issues:
        return PairedHoleVerification(
            status=PairedHoleStatus.UNSUPPORTED_OUT_OF_SCOPE,
            state_fingerprint=state.fingerprint,
            issues=tuple(scope_issues),
            missing_colors=missing_public,
            fans=fans,
        )

    profile_alignment = _profile_alignment(state, missing, fans)
    blockages, blockage_issues = _derive_blockages(state, missing, fans)
    terminal_witnesses, terminal_issues = _terminal_release_witnesses(state, blockages)
    residue_issues = (*blockage_issues, *terminal_issues)
    if residue_issues:
        return PairedHoleVerification(
            status=PairedHoleStatus.UNSUPPORTED_OUT_OF_SCOPE,
            state_fingerprint=state.fingerprint,
            issues=tuple(residue_issues),
            missing_colors=missing_public,
            fans=fans,
            profile_alignment=profile_alignment,
            alpha_beta_blockages=blockages,
            terminal_release_witnesses=terminal_witnesses,
        )

    cross_components, exits, cross_terminal_exits, cross_issues = _derive_cross_components(
        state, missing
    )
    if cross_issues:
        return PairedHoleVerification(
            status=PairedHoleStatus.UNSUPPORTED_OUT_OF_SCOPE,
            state_fingerprint=state.fingerprint,
            issues=cross_issues,
            missing_colors=missing_public,
            fans=fans,
            profile_alignment=profile_alignment,
            alpha_beta_blockages=blockages,
            terminal_release_witnesses=terminal_witnesses,
            cross_components=cross_components,
            verified_exits=exits,
            cross_terminal_release_exits=cross_terminal_exits,
        )
    orbit_exit = (
        _derive_two_swap_orbit_exit(state, missing)
        if not exits and not cross_terminal_exits and not terminal_witnesses
        else None
    )
    if exits:
        status = PairedHoleStatus.VERIFIED_ONE_SWAP_EXIT
    elif cross_terminal_exits:
        status = PairedHoleStatus.VERIFIED_CROSS_TERMINAL_RELEASE_EXIT
    elif terminal_witnesses:
        status = PairedHoleStatus.VERIFIED_ALPHA_TERMINAL_RELEASE
    elif orbit_exit is not None:
        status = PairedHoleStatus.VERIFIED_TWO_SWAP_ORBIT_EXIT
    else:
        status = PairedHoleStatus.FULLY_BLOCKED_CANDIDATE
    return PairedHoleVerification(
        status=status,
        state_fingerprint=state.fingerprint,
        missing_colors=missing_public,
        fans=fans,
        profile_alignment=profile_alignment,
        alpha_beta_blockages=blockages,
        terminal_release_witnesses=terminal_witnesses,
        cross_components=cross_components,
        verified_exits=exits,
        cross_terminal_release_exits=cross_terminal_exits,
        two_swap_orbit_exit=orbit_exit,
    )


def verify_paired_hole_json(data: str | bytes) -> PairedHoleVerification:
    """Parse and verify a state, mapping all hostile-input failures to invalid."""

    try:
        state = PairedHoleState.from_json(data)
    except (PairedHoleError, GraphError) as exc:
        return PairedHoleVerification(
            status=PairedHoleStatus.INVALID_STATE,
            issues=(PairedHoleIssue("format_error", "$", str(exc)),),
        )
    return verify_paired_hole_state(state)


__all__ = [
    "PAIRED_HOLE_KIND",
    "PAIRED_HOLE_SCHEMA_VERSION",
    "AlphaBetaBlockage",
    "AlternatingComponent",
    "CrossComponentAnalysis",
    "CrossComponentRelation",
    "CrossRootTerminalAnalysis",
    "CrossSwapAttempt",
    "CrossTerminalReleaseExit",
    "FanClosureAnalysis",
    "KempeMove",
    "OrbitTopologySignature",
    "PairedHoleError",
    "PairedHoleFormatError",
    "PairedHoleIssue",
    "PairedHoleRoles",
    "PairedHoleState",
    "PairedHoleStatus",
    "PairedHoleVerification",
    "ProfileAlignment",
    "TerminalLockAnalysis",
    "TerminalReleaseWitness",
    "TwoSwapOrbitExit",
    "terminal_degree_lower_bound",
    "terminal_release_is_applicable",
    "verify_paired_hole_json",
    "verify_paired_hole_state",
]
