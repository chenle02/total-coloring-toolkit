"""Stable identifiers and dispatch for deterministic coloring backends."""

from __future__ import annotations

from enum import StrEnum

from total_coloring.model import ColoringProblem
from total_coloring.solver import SearchLimits, SolveResult, solve_dsatur
from total_coloring.static_solver import solve_static_backtracking


class SolverBackend(StrEnum):
    """Versioned solver identities suitable for reproducibility receipts."""

    DSATUR = "dsatur-iterative-v1"
    STATIC = "static-order-iterative-v1"


DEFAULT_SOLVER_BACKEND = SolverBackend.DSATUR


def solve_with_backend(
    problem: ColoringProblem,
    *,
    backend: SolverBackend = DEFAULT_SOLVER_BACKEND,
    limits: SearchLimits | None = None,
) -> SolveResult:
    """Solve ``problem`` with one explicitly identified reference backend."""

    if not isinstance(backend, SolverBackend):
        raise ValueError("backend must be a SolverBackend")
    if backend is SolverBackend.DSATUR:
        return solve_dsatur(problem, limits=limits)
    if backend is SolverBackend.STATIC:
        return solve_static_backtracking(problem, limits=limits)
    raise ValueError(f"unsupported solver backend: {backend!r}")  # pragma: no cover


__all__ = ["DEFAULT_SOLVER_BACKEND", "SolverBackend", "solve_with_backend"]
