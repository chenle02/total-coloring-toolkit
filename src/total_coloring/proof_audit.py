"""Exact arithmetic audits for counting claims used in proof sketches.

This module verifies implications among stated numerical hypotheses. It does
not establish that a graph or Tashkinov construction satisfies those
hypotheses; that remains a separate mathematical obligation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True, slots=True)
class CountingParameters:
    """Counts from the draft's ``p/q/h`` argument."""

    repeated_colors: int
    singleton_colors: int
    multiplicity_cap: Fraction

    def __post_init__(self) -> None:
        if (
            isinstance(self.repeated_colors, bool)
            or not isinstance(self.repeated_colors, int)
            or self.repeated_colors < 0
        ):
            raise ValueError("repeated_colors must be a nonnegative integer")
        if (
            isinstance(self.singleton_colors, bool)
            or not isinstance(self.singleton_colors, int)
            or self.singleton_colors < 0
        ):
            raise ValueError("singleton_colors must be a nonnegative integer")
        if not isinstance(self.multiplicity_cap, Fraction) or self.multiplicity_cap <= 0:
            raise ValueError("multiplicity_cap must be a positive Fraction")

    @property
    def h(self) -> Fraction:
        return max(
            Fraction(self.singleton_colors),
            Fraction(self.repeated_colors) * self.multiplicity_cap,
        )

    @property
    def distinct_missing_colors(self) -> int:
        return self.repeated_colors + self.singleton_colors


@dataclass(frozen=True, slots=True)
class InequalityAudit:
    hypothesis_label: str
    left: Fraction
    right: Fraction
    strict: bool

    @property
    def holds(self) -> bool:
        return self.left < self.right if self.strict else self.left <= self.right

    @property
    def margin(self) -> Fraction:
        return self.right - self.left


def audit_draft_final_inequality(parameters: CountingParameters) -> InequalityAudit:
    """Audit ``p + q < 3h/(2c)`` using only ``h=max(q,pc)``."""

    return InequalityAudit(
        hypothesis_label="draft: h=max(q,p*c) alone",
        left=Fraction(parameters.distinct_missing_colors),
        right=Fraction(3, 2) * parameters.h / parameters.multiplicity_cap,
        strict=True,
    )


def find_draft_counterexamples(
    *,
    repeated_range: Iterable[int],
    singleton_range: Iterable[int],
    multiplicity_caps: Iterable[Fraction],
) -> tuple[CountingParameters, ...]:
    """Enumerate exact counterexamples to the draft's final implication."""

    counterexamples: list[CountingParameters] = []
    for cap in multiplicity_caps:
        for repeated in repeated_range:
            for singleton in singleton_range:
                parameters = CountingParameters(repeated, singleton, cap)
                if not audit_draft_final_inequality(parameters).holds:
                    counterexamples.append(parameters)
    return tuple(counterexamples)


def audit_corrected_incidence_closure(parameters: CountingParameters) -> InequalityAudit:
    """Audit the proposed corrected incidence contradiction.

    The numerical hypotheses are:

    - a tree-size bound ``|T| >= max(q, p*c)``;
    - at least ``2|T| + 2`` missing incidences; and
    - at most ``p*c + q`` missing incidences.

    Returning ``holds`` means the lower bound is strictly larger than the upper
    bound. It does not prove any of the three hypotheses for a graph.
    """

    lower = 2 * parameters.h + 2
    upper = Fraction(parameters.repeated_colors) * parameters.multiplicity_cap + Fraction(
        parameters.singleton_colors
    )
    return InequalityAudit(
        hypothesis_label="corrected: lower missing incidences exceed upper bound",
        left=upper,
        right=lower,
        strict=True,
    )
