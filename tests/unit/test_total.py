from __future__ import annotations

from total_coloring.graph import SimpleGraph
from total_coloring.solver import SolveStatus, solve_dsatur
from total_coloring.total import split_total_assignment, total_coloring_problem


def test_total_problem_has_expected_items_and_conflicts_for_path() -> None:
    graph = SimpleGraph.from_edges(3, [(0, 1), (1, 2)])

    problem = total_coloring_problem(graph, color_count=3)

    assert problem.item_names == (
        "vertex:0",
        "vertex:1",
        "vertex:2",
        "edge:0-1",
        "edge:1-2",
    )
    assert problem.conflicts == (
        (0, 1),
        (0, 3),
        (1, 2),
        (1, 3),
        (1, 4),
        (2, 4),
        (3, 4),
    )


def test_reference_solver_finds_and_splits_verified_total_coloring() -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (1, 2), (2, 3), (0, 3)])
    problem = total_coloring_problem(graph, color_count=4)

    result = solve_dsatur(problem)

    assert result.status is SolveStatus.WITNESS
    assert result.assignment is not None
    vertex_colors, edge_colors = split_total_assignment(graph, result.assignment)
    assert len(vertex_colors) == graph.order
    assert len(edge_colors) == graph.size
    assert problem.verify_assignment(result.assignment) == ()


def test_total_coloring_lower_bound_detects_triangle_with_two_colors() -> None:
    graph = SimpleGraph.from_edges(3, [(0, 1), (0, 2), (1, 2)])

    result = solve_dsatur(total_coloring_problem(graph, color_count=2))

    assert result.status is SolveStatus.CANDIDATE_UNSAT


def test_split_rejects_wrong_assignment_size() -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])

    try:
        split_total_assignment(graph, (0, 1))
    except ValueError as error:
        assert str(error) == "expected 3 colors, found 2"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("wrong assignment size was accepted")
