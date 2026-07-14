from __future__ import annotations

import pytest

from total_coloring.edge import edge_coloring_problem, verify_edge_coloring
from total_coloring.graph import SimpleGraph
from total_coloring.solver import SolveStatus, solve_dsatur


def test_edge_coloring_problem_solves_path() -> None:
    graph = SimpleGraph.from_edges(3, [(0, 1), (1, 2)])
    problem = edge_coloring_problem(graph, 2)

    solved = solve_dsatur(problem)

    assert solved.status is SolveStatus.WITNESS
    assert solved.assignment is not None
    assert verify_edge_coloring(graph, 2, solved.assignment).valid


def test_disjoint_distinguished_edges_are_rainbow() -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (2, 3)])
    problem = edge_coloring_problem(
        graph,
        2,
        distinguished_edges=graph.edges,
        fix_distinguished_colors=True,
    )

    assert problem.all_different == ((0, 1),)
    assert problem.fixed_colors == ((0, 0), (1, 1))
    solved = solve_dsatur(problem)
    assert solved.assignment == (0, 1)
    assert verify_edge_coloring(graph, 2, solved.assignment, distinguished_edges=graph.edges).valid


def test_edge_verifier_detects_every_violation_category() -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (0, 2), (2, 3)])

    adjacent = verify_edge_coloring(graph, 3, (0, 0, 1))
    rainbow = verify_edge_coloring(graph, 3, (0, 1, 0), distinguished_edges=((0, 1), (2, 3)))
    out_of_range = verify_edge_coloring(graph, 3, (0, 1, 3))

    assert not adjacent.valid
    assert "incident with vertex 0" in adjacent.violations[0]
    assert not rainbow.valid
    assert "distinguished edges" in rainbow.violations[0]
    assert not out_of_range.valid
    with pytest.raises(ValueError, match="invalid edge coloring"):
        adjacent.require_valid()


@pytest.mark.parametrize(
    ("distinguished", "message"),
    [
        (((0, 2),), "not in the graph"),
        (((0, 1), (1, 0)), "duplicate"),
    ],
)
def test_edge_problem_rejects_bad_distinguished_edges(
    distinguished: tuple[tuple[int, int], ...], message: str
) -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])

    with pytest.raises(ValueError, match=message):
        edge_coloring_problem(graph, 2, distinguished_edges=distinguished)


def test_edge_problem_supports_empty_graph_and_rejects_too_small_palette() -> None:
    empty_problem = edge_coloring_problem(SimpleGraph.from_edges(2, []), 0)
    assert solve_dsatur(empty_problem).assignment == ()

    matching = SimpleGraph.from_edges(4, [(0, 1), (2, 3)])
    with pytest.raises(ValueError, match="palette"):
        edge_coloring_problem(matching, 1, distinguished_edges=matching.edges)


@pytest.mark.parametrize("color_count", [True, 1.5, -1, 0])
def test_edge_problem_rejects_malformed_or_empty_palettes(color_count: object) -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])

    with pytest.raises(ValueError, match="color_count"):
        edge_coloring_problem(graph, color_count)  # type: ignore[arg-type]


def test_edge_problem_requires_boolean_symmetry_option() -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])

    with pytest.raises(ValueError, match="boolean"):
        edge_coloring_problem(graph, 2, fix_distinguished_colors=1)  # type: ignore[arg-type]
