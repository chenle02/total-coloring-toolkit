"""Semantic total-coloring problem construction."""

from __future__ import annotations

import itertools

from total_coloring.graph import SimpleGraph
from total_coloring.model import ColoringProblem


def total_coloring_problem(graph: SimpleGraph, color_count: int) -> ColoringProblem:
    """Construct the explicit conflict system for a total coloring of ``graph``."""

    item_names = tuple(f"vertex:{vertex}" for vertex in range(graph.order)) + tuple(
        f"edge:{left}-{right}" for left, right in graph.edges
    )
    conflicts: set[tuple[int, int]] = set()

    for left, right in graph.edges:
        conflicts.add((left, right))

    incident_items: list[list[int]] = [[] for _vertex in range(graph.order)]
    for edge_index, (left, right) in enumerate(graph.edges):
        item = graph.order + edge_index
        conflicts.add((left, item))
        conflicts.add((right, item))
        incident_items[left].append(item)
        incident_items[right].append(item)
    for items in incident_items:
        conflicts.update(itertools.combinations(items, 2))

    return ColoringProblem(
        item_names=item_names,
        color_count=color_count,
        conflicts=tuple(sorted(conflicts)),
        metadata=(("graph_fingerprint", graph.fingerprint), ("kind", "total_coloring")),
    )


def split_total_assignment(
    graph: SimpleGraph, assignment: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Split a problem assignment into vertex and canonical-edge color arrays."""

    expected = graph.order + graph.size
    if len(assignment) != expected:
        raise ValueError(f"expected {expected} colors, found {len(assignment)}")
    return assignment[: graph.order], assignment[graph.order :]
