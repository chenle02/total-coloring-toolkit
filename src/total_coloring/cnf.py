"""Transparent deterministic CNF encoding for finite coloring problems."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from total_coloring.model import ColoringProblem

ENCODER_ID = "onehot-pairwise-v1"


@dataclass(frozen=True, slots=True)
class CnfFormula:
    variable_count: int
    clauses: tuple[tuple[int, ...], ...]
    problem_digest: str
    encoder_id: str = ENCODER_ID
    _digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.variable_count, bool)
            or not isinstance(self.variable_count, int)
            or self.variable_count < 0
        ):
            raise ValueError("variable_count must be a nonnegative integer")
        try:
            clauses = tuple(tuple(clause) for clause in self.clauses)
        except TypeError as exc:
            raise ValueError("clauses must be an iterable of literal iterables") from exc
        object.__setattr__(self, "clauses", clauses)
        if re.fullmatch(r"[0-9a-f]{64}", self.problem_digest) is None:
            raise ValueError("problem_digest must be lowercase SHA-256 hex")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.encoder_id) is None:
            raise ValueError("encoder_id must be a safe nonempty token")
        for clause in clauses:
            if not clause:
                raise ValueError("empty clauses must be represented by an explicit UNSAT result")
            if len(set(clause)) != len(clause):
                raise ValueError("clauses must not contain duplicate literals")
            for literal in clause:
                if (
                    isinstance(literal, bool)
                    or not isinstance(literal, int)
                    or literal == 0
                    or abs(literal) > self.variable_count
                ):
                    raise ValueError("clause literal is outside the declared variable range")
        object.__setattr__(self, "_digest", hashlib.sha256(self.to_dimacs().encode()).hexdigest())

    @property
    def digest(self) -> str:
        return self._digest

    def to_dimacs(self) -> str:
        lines = [
            f"c encoder {self.encoder_id}",
            f"c problem_sha256 {self.problem_digest}",
            f"p cnf {self.variable_count} {len(self.clauses)}",
        ]
        lines.extend(" ".join(str(literal) for literal in clause) + " 0" for clause in self.clauses)
        return "\n".join(lines) + "\n"


def variable(item: int, color: int, color_count: int) -> int:
    """Stable one-hot variable number, starting at one."""

    if any(isinstance(value, bool) or not isinstance(value, int) for value in (item, color_count)):
        raise ValueError("invalid item/color coordinate")
    if isinstance(color, bool) or not isinstance(color, int):
        raise ValueError("invalid item/color coordinate")
    if item < 0 or color_count <= 0 or color < 0 or color >= color_count:
        raise ValueError("invalid item/color coordinate")
    return 1 + item * color_count + color


def encode_onehot_pairwise(problem: ColoringProblem) -> CnfFormula:
    """Encode a coloring problem without auxiliary variables.

    The intentionally simple encoding is the auditable publication baseline:
    at least one color, pairwise at most one color, pairwise conflict clauses,
    and unit clauses for fixed colors.
    """

    clauses: list[tuple[int, ...]] = []
    for item in range(problem.item_count):
        clauses.append(
            tuple(
                variable(item, color, problem.color_count) for color in range(problem.color_count)
            )
        )
        clauses.extend(
            (
                -variable(item, left_color, problem.color_count),
                -variable(item, right_color, problem.color_count),
            )
            for left_color in range(problem.color_count)
            for right_color in range(left_color + 1, problem.color_count)
        )

    for left in range(problem.item_count):
        neighbors = problem.neighbor_masks[left] & ~((1 << (left + 1)) - 1)
        while neighbors:
            bit = neighbors & -neighbors
            right = bit.bit_length() - 1
            clauses.extend(
                (
                    -variable(left, color, problem.color_count),
                    -variable(right, color, problem.color_count),
                )
                for color in range(problem.color_count)
            )
            neighbors ^= bit

    clauses.extend(
        (variable(item, color, problem.color_count),) for item, color in problem.fixed_colors
    )
    return CnfFormula(
        variable_count=problem.item_count * problem.color_count,
        clauses=tuple(clauses),
        problem_digest=problem.semantic_digest,
    )


def decode_positive_model(
    problem: ColoringProblem, positive_variables: set[int]
) -> tuple[int, ...]:
    """Decode and semantically verify a SAT model's positive variables."""

    maximum_variable = problem.item_count * problem.color_count
    for value in positive_variables:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum_variable
        ):
            raise ValueError(f"positive SAT variable {value!r} is outside 1..{maximum_variable}")
    assignment: list[int] = []
    for item in range(problem.item_count):
        selected = [
            color
            for color in range(problem.color_count)
            if variable(item, color, problem.color_count) in positive_variables
        ]
        if len(selected) != 1:
            raise ValueError(f"item {item} has {len(selected)} selected colors in SAT model")
        assignment.append(selected[0])
    result = tuple(assignment)
    violations = problem.verify_assignment(result)
    if violations:
        raise ValueError("decoded SAT model violates semantics: " + "; ".join(violations))
    return result
