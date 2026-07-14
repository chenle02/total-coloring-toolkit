from __future__ import annotations

import pytest

from total_coloring.auxiliary import iter_equitable_partitions, solve_auxiliary_partition
from total_coloring.backends import SolverBackend, solve_with_backend
from total_coloring.graph import SimpleGraph
from total_coloring.model import ColoringProblem
from total_coloring.solver import SolveStatus


def test_backend_dispatch_finds_independently_verified_witnesses() -> None:
    problem = ColoringProblem(
        item_names=("a", "b", "c"),
        color_count=3,
        conflicts=((0, 1), (0, 2), (1, 2)),
    )

    dsatur = solve_with_backend(problem, backend=SolverBackend.DSATUR)
    static = solve_with_backend(problem, backend=SolverBackend.STATIC)

    assert dsatur.status is static.status is SolveStatus.WITNESS
    assert dsatur.assignment is not None and problem.verify_assignment(dsatur.assignment) == ()
    assert static.assignment is not None and problem.verify_assignment(static.assignment) == ()
    assert dsatur.problem_digest == static.problem_digest == problem.semantic_digest


def test_auxiliary_partition_accepts_explicit_independent_backend() -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (1, 2), (2, 3), (0, 3)])
    partition = next(iter_equitable_partitions(graph))

    dsatur = solve_auxiliary_partition(
        graph,
        partition,
        graph.max_degree + 2,
        backend=SolverBackend.DSATUR,
    )
    static = solve_auxiliary_partition(
        graph,
        partition,
        graph.max_degree + 2,
        backend=SolverBackend.STATIC,
    )

    assert dsatur.status is static.status is SolveStatus.WITNESS
    assert dsatur.total_coloring is not None
    assert static.total_coloring is not None


def test_backend_dispatch_rejects_implicit_strings() -> None:
    problem = ColoringProblem(("item",), 1, ())
    with pytest.raises(ValueError, match="SolverBackend"):
        solve_with_backend(problem, backend="dsatur-iterative-v1")  # type: ignore[arg-type]
