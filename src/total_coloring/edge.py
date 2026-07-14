"""Edge-coloring problem construction and independent witness checks."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass

from total_coloring.graph import Edge, SimpleGraph
from total_coloring.model import ColoringProblem


@dataclass(frozen=True, slots=True)
class EdgeColoringVerification:
    """Semantic verification result for an edge-color assignment."""

    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.violations

    def require_valid(self) -> None:
        if self.violations:
            raise ValueError("invalid edge coloring: " + "; ".join(self.violations))


def _distinguished_indices(
    graph: SimpleGraph, distinguished_edges: Iterable[Edge]
) -> tuple[int, ...]:
    edge_to_index = {edge: index for index, edge in enumerate(graph.edges)}
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_left, raw_right in distinguished_edges:
        edge = (raw_left, raw_right) if raw_left < raw_right else (raw_right, raw_left)
        try:
            index = edge_to_index[edge]
        except KeyError as exc:
            raise ValueError(f"distinguished edge {edge} is not in the graph") from exc
        if index in seen:
            raise ValueError(f"duplicate distinguished edge {edge}")
        seen.add(index)
        normalized.append(index)
    return tuple(sorted(normalized))


def edge_coloring_problem(
    graph: SimpleGraph,
    color_count: int,
    *,
    distinguished_edges: Iterable[Edge] = (),
    fix_distinguished_colors: bool = False,
) -> ColoringProblem:
    """Construct a proper edge-coloring problem with an optional rainbow family.

    When ``fix_distinguished_colors`` is true, the distinguished edges are fixed
    to colors ``0, ..., r-1`` in canonical edge order. This is without loss of
    generality because color names are interchangeable and the family is required
    to be rainbow.
    """

    if (
        isinstance(color_count, bool)
        or not isinstance(color_count, int)
        or color_count < 0
        or (graph.size > 0 and color_count == 0)
    ):
        raise ValueError("color_count must be nonnegative and positive when edges exist")
    if not isinstance(fix_distinguished_colors, bool):
        raise ValueError("fix_distinguished_colors must be a boolean")
    distinguished = _distinguished_indices(graph, distinguished_edges)
    if len(distinguished) > color_count:
        raise ValueError("the palette is smaller than the distinguished rainbow family")

    conflicts: set[tuple[int, int]] = set()
    incident: list[list[int]] = [[] for _vertex in range(graph.order)]
    for edge_index, (left, right) in enumerate(graph.edges):
        incident[left].append(edge_index)
        incident[right].append(edge_index)
    for edge_indices in incident:
        conflicts.update(itertools.combinations(edge_indices, 2))

    groups = (distinguished,) if len(distinguished) >= 2 else ()
    fixed = (
        tuple((edge_index, color) for color, edge_index in enumerate(distinguished))
        if fix_distinguished_colors
        else ()
    )
    return ColoringProblem(
        item_names=tuple(f"edge:{left}-{right}" for left, right in graph.edges),
        color_count=color_count,
        conflicts=tuple(sorted(conflicts)),
        all_different=groups,
        fixed_colors=fixed,
        metadata=(
            ("graph_fingerprint", graph.fingerprint),
            ("kind", "rainbow_edge_coloring" if distinguished else "edge_coloring"),
        ),
    )


def verify_edge_coloring(
    graph: SimpleGraph,
    color_count: int,
    assignment: tuple[int, ...],
    *,
    distinguished_edges: Iterable[Edge] = (),
) -> EdgeColoringVerification:
    """Check edge colors directly, independently of any solver encoding."""

    violations: list[str] = []
    if (
        isinstance(color_count, bool)
        or not isinstance(color_count, int)
        or color_count < 0
        or (graph.size > 0 and color_count == 0)
    ):
        return EdgeColoringVerification(
            ("color_count must be nonnegative and positive when edges exist",)
        )
    if len(assignment) != graph.size:
        return EdgeColoringVerification(
            (f"expected {graph.size} edge colors, found {len(assignment)}",)
        )
    for edge_index, color in enumerate(assignment):
        if isinstance(color, bool) or not isinstance(color, int):
            violations.append(f"edge {edge_index} has non-integer color {color!r}")
        elif not 0 <= color < color_count:
            violations.append(f"edge {edge_index} has out-of-range color {color}")
    if violations:
        return EdgeColoringVerification(tuple(violations))

    for vertex in range(graph.order):
        indices = [edge_index for edge_index, edge in enumerate(graph.edges) if vertex in edge]
        by_color: dict[int, int] = {}
        for edge_index in indices:
            color = assignment[edge_index]
            if color in by_color:
                violations.append(
                    f"edges {by_color[color]} and {edge_index} incident with vertex "
                    f"{vertex} both use color {color}"
                )
            else:
                by_color[color] = edge_index

    try:
        distinguished = _distinguished_indices(graph, distinguished_edges)
    except ValueError as exc:
        violations.append(str(exc))
    else:
        seen_colors: dict[int, int] = {}
        for edge_index in distinguished:
            color = assignment[edge_index]
            if color in seen_colors:
                violations.append(
                    f"distinguished edges {seen_colors[color]} and {edge_index} "
                    f"both use color {color}"
                )
            else:
                seen_colors[color] = edge_index
    return EdgeColoringVerification(tuple(violations))
