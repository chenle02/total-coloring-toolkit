"""Independent deterministic static-order backtracking audit backend.

This backend deliberately avoids DSATUR's dynamic ordering and color-symmetry
breaking. It is slower, but useful for differential checks of bounded claims.
Like DSATUR, an exhausted search is only ``CANDIDATE_UNSAT`` until accompanied
by an independently checkable negative proof artifact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from total_coloring.model import ColoringProblem
from total_coloring.solver import SearchLimits, SearchStats, SolveResult, SolveStatus


class _AbortSearch(Exception):
    pass


@dataclass(slots=True)
class _Frame:
    item: int
    candidates: tuple[int, ...]
    next_candidate: int = 0


def solve_static_backtracking(
    problem: ColoringProblem,
    *,
    limits: SearchLimits | None = None,
) -> SolveResult:
    """Solve with fixed item order, all palette colors, and an explicit stack."""

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
        neighbors = problem.neighbor_masks[left]
        while neighbors:
            bit = neighbors & -neighbors
            right = bit.bit_length() - 1
            if right > left and assignment[right] == assignment[left]:
                return SolveResult(
                    SolveStatus.CANDIDATE_UNSAT,
                    problem.semantic_digest,
                    None,
                    SearchStats(0, 0, time.monotonic() - started),
                    "fixed colors violate a conflict",
                )
            neighbors ^= bit

    order = tuple(
        sorted(
            (item for item, color in enumerate(assignment) if color < 0),
            key=lambda item: (-problem.neighbor_masks[item].bit_count(), item),
        )
    )

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

    def candidates(item: int) -> tuple[int, ...]:
        forbidden = 0
        neighbors = problem.neighbor_masks[item]
        while neighbors:
            bit = neighbors & -neighbors
            neighbor = bit.bit_length() - 1
            color = assignment[neighbor]
            if color >= 0:
                forbidden |= 1 << color
            neighbors ^= bit
        return tuple(color for color in range(problem.color_count) if not (forbidden >> color) & 1)

    if not order:
        witness = tuple(assignment)
        return SolveResult(
            SolveStatus.WITNESS,
            problem.semantic_digest,
            witness,
            SearchStats(1, 0, time.monotonic() - started),
            "semantic witness verified by static audit backend",
        )

    frames: list[_Frame] = []
    depth = 0
    found = False
    try:
        while True:
            check_limits()
            if len(frames) == depth:
                item = order[depth]
                frames.append(_Frame(item, candidates(item)))
                nodes += 1
            frame = frames[depth]
            if frame.next_candidate < len(frame.candidates):
                assignment[frame.item] = frame.candidates[frame.next_candidate]
                frame.next_candidate += 1
                depth += 1
                if depth == len(order):
                    found = True
                    break
                continue

            assignment[frame.item] = -1
            frames.pop()
            backtracks += 1
            if depth == 0:
                break
            depth -= 1
            assignment[order[depth]] = -1
    except _AbortSearch:
        return SolveResult(
            SolveStatus.UNKNOWN,
            problem.semantic_digest,
            None,
            SearchStats(nodes, backtracks, time.monotonic() - started),
            abort_detail,
        )

    elapsed = time.monotonic() - started
    if not found:
        return SolveResult(
            SolveStatus.CANDIDATE_UNSAT,
            problem.semantic_digest,
            None,
            SearchStats(nodes, backtracks, elapsed),
            "static audit search exhausted; no independent UNSAT proof attached",
        )
    witness = tuple(assignment)
    violations = problem.verify_assignment(witness)
    if violations:
        return SolveResult(
            SolveStatus.ERROR,
            problem.semantic_digest,
            None,
            SearchStats(nodes, backtracks, elapsed),
            "internal witness verification failed: " + "; ".join(violations),
        )
    return SolveResult(
        SolveStatus.WITNESS,
        problem.semantic_digest,
        witness,
        SearchStats(nodes, backtracks, elapsed),
        "semantic witness verified by static audit backend",
    )
