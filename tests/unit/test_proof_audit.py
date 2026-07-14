from __future__ import annotations

from fractions import Fraction

import pytest

from total_coloring.proof_audit import (
    CountingParameters,
    audit_corrected_incidence_closure,
    audit_draft_final_inequality,
    find_draft_counterexamples,
)


def test_draft_inequality_fails_at_the_smallest_c_equals_two_example() -> None:
    parameters = CountingParameters(
        repeated_colors=1,
        singleton_colors=1,
        multiplicity_cap=Fraction(2),
    )

    audit = audit_draft_final_inequality(parameters)

    assert parameters.h == 2
    assert audit.left == 2
    assert audit.right == Fraction(3, 2)
    assert not audit.holds
    assert audit.margin == Fraction(-1, 2)


def test_counterexample_enumerator_is_exact_and_deterministic() -> None:
    counterexamples = find_draft_counterexamples(
        repeated_range=range(1, 3),
        singleton_range=range(1, 3),
        multiplicity_caps=(Fraction(1), Fraction(2)),
    )

    assert counterexamples == (
        CountingParameters(1, 1, Fraction(1)),
        CountingParameters(1, 2, Fraction(1)),
        CountingParameters(2, 1, Fraction(1)),
        CountingParameters(2, 2, Fraction(1)),
        CountingParameters(1, 1, Fraction(2)),
        CountingParameters(1, 2, Fraction(2)),
        CountingParameters(2, 1, Fraction(2)),
        CountingParameters(2, 2, Fraction(2)),
    )


@pytest.mark.parametrize("repeated", range(0, 8))
@pytest.mark.parametrize("singleton", range(0, 8))
@pytest.mark.parametrize("cap", [Fraction(1), Fraction(2), Fraction(3), Fraction(5, 2)])
def test_corrected_incidence_hypotheses_close_arithmetically(
    repeated: int, singleton: int, cap: Fraction
) -> None:
    parameters = CountingParameters(repeated, singleton, cap)

    audit = audit_corrected_incidence_closure(parameters)

    assert audit.holds
    assert audit.margin >= 2


@pytest.mark.parametrize(
    "parameters",
    [
        (-1, 0, Fraction(1)),
        (0, -1, Fraction(1)),
        (0, 0, Fraction(0)),
        (True, 0, Fraction(1)),
        (0, False, Fraction(1)),
        (1.5, 0, Fraction(1)),
        (0, 1.5, Fraction(1)),
        (0, 0, 1),
    ],
)
def test_counting_parameters_reject_invalid_values(
    parameters: tuple[int, int, Fraction],
) -> None:
    with pytest.raises(ValueError):
        CountingParameters(*parameters)
