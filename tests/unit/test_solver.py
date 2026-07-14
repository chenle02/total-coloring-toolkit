from __future__ import annotations

import math

import pytest

from total_coloring.model import ColoringProblem
from total_coloring.solver import SearchLimits, SolveStatus, solve_dsatur


def clique_problem(order: int, colors: int) -> ColoringProblem:
    return ColoringProblem(
        item_names=tuple(f"v:{vertex}" for vertex in range(order)),
        color_count=colors,
        conflicts=tuple((left, right) for left in range(order) for right in range(left + 1, order)),
    )


def test_dsatur_returns_semantically_verified_witness() -> None:
    problem = clique_problem(4, 4)

    result = solve_dsatur(problem)

    assert result.status is SolveStatus.WITNESS
    assert result.assignment is not None
    assert problem.verify_assignment(result.assignment) == ()
    assert result.problem_digest == problem.semantic_digest
    assert result.stats.nodes > 0


def test_dsatur_exhaustion_is_not_overclaimed_as_proved_unsat() -> None:
    result = solve_dsatur(clique_problem(4, 3))

    assert result.status is SolveStatus.CANDIDATE_UNSAT
    assert result.assignment is None
    assert "no independent UNSAT proof" in result.detail


def test_dsatur_respects_all_different_and_fixed_colors() -> None:
    problem = ColoringProblem(
        item_names=("a", "b", "c", "d"),
        color_count=4,
        conflicts=(),
        all_different=((0, 1, 2, 3),),
        fixed_colors=((0, 0), (1, 1)),
    )

    result = solve_dsatur(problem)

    assert result.status is SolveStatus.WITNESS
    assert result.assignment is not None
    assert result.assignment[:2] == (0, 1)
    assert len(set(result.assignment)) == 4


def test_conflicting_fixed_colors_short_circuit() -> None:
    problem = ColoringProblem(
        item_names=("a", "b"),
        color_count=2,
        conflicts=((0, 1),),
        fixed_colors=((0, 0), (1, 0)),
    )

    result = solve_dsatur(problem)

    assert result.status is SolveStatus.CANDIDATE_UNSAT
    assert result.stats.nodes == 0
    assert result.detail == "fixed colors violate a conflict"


def test_node_limit_returns_unknown_not_unsat() -> None:
    result = solve_dsatur(clique_problem(5, 5), limits=SearchLimits(max_nodes=1))

    assert result.status is SolveStatus.UNKNOWN
    assert result.assignment is None
    assert result.detail == "node limit reached (1)"


@pytest.mark.parametrize(
    "limits",
    [
        SearchLimits,
    ],
)
def test_limits_fixture_is_importable(limits: object) -> None:
    assert limits is SearchLimits


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_nodes": 0},
        {"max_nodes": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
    ],
)
def test_limits_reject_nonpositive_or_boolean_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SearchLimits(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_nodes": 1.5},
        {"timeout_seconds": math.nan},
        {"timeout_seconds": math.inf},
        {"timeout_seconds": "1"},
    ],
)
def test_limits_reject_malformed_numeric_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SearchLimits(**kwargs)  # type: ignore[arg-type]


def test_iterative_solver_is_independent_of_python_recursion_limit() -> None:
    problem = ColoringProblem(
        item_names=tuple(f"x:{index}" for index in range(1_500)),
        color_count=1,
        conflicts=(),
    )

    result = solve_dsatur(problem)

    assert result.status is SolveStatus.WITNESS
    assert result.assignment == (0,) * 1_500


def test_solver_supports_the_trivial_empty_problem() -> None:
    result = solve_dsatur(ColoringProblem((), 0, ()))

    assert result.status is SolveStatus.WITNESS
    assert result.assignment == ()
