"""Deterministic dependency-free reference solver for finite coloring problems."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import StrEnum

from total_coloring.model import ColoringProblem


class SolveStatus(StrEnum):
    """Publication-aware solver outcomes.

    ``CANDIDATE_UNSAT`` means the reference search exhausted its tree, but no
    independently checked proof artifact accompanies that negative result.
    """

    WITNESS = "witness"
    CANDIDATE_UNSAT = "candidate_unsat"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SearchLimits:
    max_nodes: int | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_nodes is not None and (
            isinstance(self.max_nodes, bool)
            or not isinstance(self.max_nodes, int)
            or self.max_nodes <= 0
        ):
            raise ValueError("max_nodes must be a positive integer or None")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive, finite, or None")


@dataclass(frozen=True, slots=True)
class SearchStats:
    nodes: int
    backtracks: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class SolveResult:
    status: SolveStatus
    problem_digest: str
    assignment: tuple[int, ...] | None
    stats: SearchStats
    detail: str

    @property
    def is_witness(self) -> bool:
        return self.status is SolveStatus.WITNESS


class _AbortSearch(Exception):
    pass


def solve_dsatur(
    problem: ColoringProblem,
    *,
    limits: SearchLimits | None = None,
) -> SolveResult:
    """Solve ``problem`` exactly by deterministic DSATUR backtracking.

    The backend is a correctness reference and witness generator. Its fully
    exhausted negative result remains ``CANDIDATE_UNSAT`` until a separately
    checkable proof format is attached.
    """

    active_limits = limits or SearchLimits()
    started = time.monotonic()
    assignment = [-1] * problem.item_count
    nodes = 0
    backtracks = 0
    abort_detail = ""

    for item, color in problem.fixed_colors:
        assignment[item] = color
    for left in range(problem.item_count):
        if assignment[left] < 0:
            continue
        mask = problem.neighbor_masks[left]
        while mask:
            bit = mask & -mask
            right = bit.bit_length() - 1
            if right > left and assignment[right] == assignment[left]:
                elapsed = time.monotonic() - started
                return SolveResult(
                    status=SolveStatus.CANDIDATE_UNSAT,
                    problem_digest=problem.semantic_digest,
                    assignment=None,
                    stats=SearchStats(nodes=0, backtracks=0, elapsed_seconds=elapsed),
                    detail="fixed colors violate a conflict",
                )
            mask ^= bit

    fixed_color_set = {color for _item, color in problem.fixed_colors}
    symmetry_breaking = not fixed_color_set or fixed_color_set == set(
        range(max(fixed_color_set) + 1)
    )
    full_palette_mask = (1 << problem.color_count) - 1

    def check_limits() -> None:
        nonlocal abort_detail
        if active_limits.max_nodes is not None and nodes >= active_limits.max_nodes:
            abort_detail = f"node limit reached ({active_limits.max_nodes})"
            raise _AbortSearch
        if (
            active_limits.timeout_seconds is not None
            and time.monotonic() - started >= active_limits.timeout_seconds
        ):
            abort_detail = f"time limit reached ({active_limits.timeout_seconds:g}s)"
            raise _AbortSearch

    def forbidden_mask(item: int) -> int:
        result = 0
        neighbors = problem.neighbor_masks[item]
        while neighbors:
            bit = neighbors & -neighbors
            neighbor = bit.bit_length() - 1
            color = assignment[neighbor]
            if color >= 0:
                result |= 1 << color
            neighbors ^= bit
        return result

    def choose_item() -> tuple[int, int] | None:
        best_item = -1
        best_forbidden = 0
        best_key = (-1, -1, 0)
        for item, color in enumerate(assignment):
            if color >= 0:
                continue
            forbidden = forbidden_mask(item)
            available = full_palette_mask & ~forbidden
            if not available:
                return (item, full_palette_mask)
            key = (
                forbidden.bit_count(),
                problem.neighbor_masks[item].bit_count(),
                -item,
            )
            if key > best_key:
                best_key = key
                best_item = item
                best_forbidden = forbidden
        return None if best_item < 0 else (best_item, best_forbidden)

    def color_candidates(forbidden: int) -> tuple[int, ...]:
        available = full_palette_mask & ~forbidden
        if not symmetry_breaking:
            return tuple(color for color in range(problem.color_count) if available >> color & 1)
        used = {color for color in assignment if color >= 0}
        candidates = sorted(color for color in used if available >> color & 1)
        for color in range(problem.color_count):
            if color not in used:
                if available >> color & 1:
                    candidates.append(color)
                break
        return tuple(candidates)

    def search() -> bool:
        """Run DSATUR with an explicit stack, independent of recursion limits."""

        nonlocal nodes, backtracks
        stack: list[tuple[int, tuple[int, ...], int]] = []
        while True:
            check_limits()
            nodes += 1
            selected = choose_item()
            if selected is None:
                return True
            item, forbidden = selected
            candidates = color_candidates(forbidden)
            if candidates:
                assignment[item] = candidates[0]
                stack.append((item, candidates, 1))
                continue

            backtracks += 1
            while stack:
                parent_item, parent_candidates, next_candidate = stack.pop()
                assignment[parent_item] = -1
                if next_candidate < len(parent_candidates):
                    assignment[parent_item] = parent_candidates[next_candidate]
                    stack.append((parent_item, parent_candidates, next_candidate + 1))
                    break
                backtracks += 1
            else:
                return False

    try:
        found = search()
    except _AbortSearch:
        elapsed = time.monotonic() - started
        return SolveResult(
            status=SolveStatus.UNKNOWN,
            problem_digest=problem.semantic_digest,
            assignment=None,
            stats=SearchStats(nodes=nodes, backtracks=backtracks, elapsed_seconds=elapsed),
            detail=abort_detail,
        )

    elapsed = time.monotonic() - started
    if found:
        witness = tuple(assignment)
        violations = problem.verify_assignment(witness)
        if violations:
            return SolveResult(
                status=SolveStatus.ERROR,
                problem_digest=problem.semantic_digest,
                assignment=None,
                stats=SearchStats(nodes=nodes, backtracks=backtracks, elapsed_seconds=elapsed),
                detail="internal witness verification failed: " + "; ".join(violations),
            )
        return SolveResult(
            status=SolveStatus.WITNESS,
            problem_digest=problem.semantic_digest,
            assignment=witness,
            stats=SearchStats(nodes=nodes, backtracks=backtracks, elapsed_seconds=elapsed),
            detail="semantic witness verified",
        )
    return SolveResult(
        status=SolveStatus.CANDIDATE_UNSAT,
        problem_digest=problem.semantic_digest,
        assignment=None,
        stats=SearchStats(nodes=nodes, backtracks=backtracks, elapsed_seconds=elapsed),
        detail="reference search exhausted; no independent UNSAT proof attached",
    )
