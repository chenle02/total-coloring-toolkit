from __future__ import annotations

from total_coloring.model import ColoringProblem
from total_coloring.solver import SearchLimits, SolveStatus
from total_coloring.static_solver import solve_static_backtracking


def clique_problem(order: int, colors: int) -> ColoringProblem:
    return ColoringProblem(
        tuple(f"v:{vertex}" for vertex in range(order)),
        colors,
        tuple((left, right) for left in range(order) for right in range(left + 1, order)),
    )


def test_static_backend_finds_witness_and_candidate_exhaustion() -> None:
    witness = solve_static_backtracking(clique_problem(4, 4))
    exhausted = solve_static_backtracking(clique_problem(4, 3))

    assert witness.status is SolveStatus.WITNESS
    assert witness.assignment is not None
    assert exhausted.status is SolveStatus.CANDIDATE_UNSAT
    assert exhausted.assignment is None


def test_static_backend_respects_noninitial_fixed_colors() -> None:
    problem = ColoringProblem(
        ("a", "b", "c"),
        3,
        ((0, 1), (1, 2)),
        fixed_colors=((0, 2),),
    )

    result = solve_static_backtracking(problem)

    assert result.status is SolveStatus.WITNESS
    assert result.assignment is not None
    assert result.assignment[0] == 2
    assert problem.verify_assignment(result.assignment) == ()


def test_static_backend_is_iterative_and_limit_aware() -> None:
    large = ColoringProblem(tuple(f"x:{index}" for index in range(1_500)), 1, ())

    solved = solve_static_backtracking(large)
    limited = solve_static_backtracking(large, limits=SearchLimits(max_nodes=1))

    assert solved.status is SolveStatus.WITNESS
    assert solved.assignment == (0,) * 1_500
    assert limited.status is SolveStatus.UNKNOWN


def test_static_backend_supports_empty_and_conflicting_fixed_instances() -> None:
    empty = solve_static_backtracking(ColoringProblem((), 0, ()))
    conflict = solve_static_backtracking(
        ColoringProblem(
            ("a", "b"),
            2,
            ((0, 1),),
            fixed_colors=((0, 1), (1, 1)),
        )
    )

    assert empty.status is SolveStatus.WITNESS
    assert empty.assignment == ()
    assert conflict.status is SolveStatus.CANDIDATE_UNSAT
    assert conflict.stats.nodes == 0
