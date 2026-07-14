"""Small exhaustive tests against deliberately independent truth definitions."""

from __future__ import annotations

import itertools

from total_coloring.cnf import encode_onehot_pairwise, variable
from total_coloring.model import ColoringProblem
from total_coloring.solver import SolveStatus, solve_dsatur
from total_coloring.static_solver import solve_static_backtracking


def conflict_systems(order: int) -> tuple[tuple[tuple[int, int], ...], ...]:
    pairs = tuple(itertools.combinations(range(order), 2))
    return tuple(
        tuple(pair for index, pair in enumerate(pairs) if mask >> index & 1)
        for mask in range(1 << len(pairs))
    )


def assignments(order: int, color_count: int) -> tuple[tuple[int, ...], ...]:
    if color_count == 0:
        return ((),) if order == 0 else ()
    return tuple(itertools.product(range(color_count), repeat=order))


def cnf_accepts(problem: ColoringProblem, assignment: tuple[int, ...]) -> bool:
    formula = encode_onehot_pairwise(problem)
    positive = {variable(item, color, problem.color_count) for item, color in enumerate(assignment)}
    return all(
        any((literal > 0) == (abs(literal) in positive) for literal in clause)
        for clause in formula.clauses
    )


def test_dsatur_and_cnf_match_exhaustive_semantics() -> None:
    for order in range(5):
        color_counts = (0,) if order == 0 else range(1, 4)
        for color_count in color_counts:
            candidate_assignments = assignments(order, color_count)
            for conflicts in conflict_systems(order):
                problem = ColoringProblem(
                    tuple(f"item:{item}" for item in range(order)),
                    color_count,
                    conflicts,
                )
                semantic_witnesses = tuple(
                    assignment
                    for assignment in candidate_assignments
                    if not problem.verify_assignment(assignment)
                )
                solved = solve_dsatur(problem)
                audited = solve_static_backtracking(problem)

                assert (solved.status is SolveStatus.WITNESS) == bool(semantic_witnesses)
                assert (audited.status is SolveStatus.WITNESS) == bool(semantic_witnesses)
                if solved.assignment is not None:
                    assert solved.assignment in semantic_witnesses
                if audited.assignment is not None:
                    assert audited.assignment in semantic_witnesses
                for assignment in candidate_assignments:
                    assert cnf_accepts(problem, assignment) == (
                        not problem.verify_assignment(assignment)
                    )


def test_fixed_color_symmetry_breaking_matches_brute_force() -> None:
    order = 3
    color_count = 3
    candidate_assignments = assignments(order, color_count)
    fixed_choices = (None, *range(color_count))
    for conflicts in conflict_systems(order):
        for raw_fixed in itertools.product(fixed_choices, repeat=order):
            fixed = tuple(
                (item, color) for item, color in enumerate(raw_fixed) if color is not None
            )
            problem = ColoringProblem(
                tuple(f"item:{item}" for item in range(order)),
                color_count,
                conflicts,
                fixed_colors=fixed,
            )
            expected = any(
                not problem.verify_assignment(assignment) for assignment in candidate_assignments
            )
            solved = solve_dsatur(problem)
            audited = solve_static_backtracking(problem)

            assert (solved.status is SolveStatus.WITNESS) == expected
            assert (audited.status is SolveStatus.WITNESS) == expected
