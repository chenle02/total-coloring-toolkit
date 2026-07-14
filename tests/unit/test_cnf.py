from __future__ import annotations

import pytest

from total_coloring.cnf import (
    ENCODER_ID,
    CnfFormula,
    decode_positive_model,
    encode_onehot_pairwise,
    variable,
)
from total_coloring.model import ColoringProblem

DIGEST = "0" * 64


def test_onehot_pairwise_encoding_is_transparent_and_deterministic() -> None:
    problem = ColoringProblem(
        item_names=("a", "b"),
        color_count=3,
        conflicts=((0, 1),),
        fixed_colors=((0, 1),),
    )

    formula = encode_onehot_pairwise(problem)

    assert formula.encoder_id == ENCODER_ID
    assert formula.variable_count == 6
    assert len(formula.clauses) == 2 + 6 + 3 + 1
    assert formula.clauses[0] == (1, 2, 3)
    assert formula.clauses[-1] == (2,)
    assert formula.problem_digest == problem.semantic_digest
    assert formula.digest == hashlib_sha256(formula.to_dimacs())


def test_model_decode_requires_exactly_one_color_and_checks_semantics() -> None:
    problem = ColoringProblem(
        item_names=("a", "b"),
        color_count=2,
        conflicts=((0, 1),),
    )

    assert decode_positive_model(problem, {1, 4}) == (0, 1)
    with pytest.raises(ValueError, match="item 0 has 2 selected colors"):
        decode_positive_model(problem, {1, 2, 4})
    with pytest.raises(ValueError, match="violates semantics"):
        decode_positive_model(problem, {1, 3})


@pytest.mark.parametrize("coordinates", [(-1, 0, 2), (0, -1, 2), (0, 2, 2)])
def test_variable_rejects_invalid_coordinates(coordinates: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        variable(*coordinates)


def test_cnf_formula_rejects_invalid_literals_and_clauses() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        CnfFormula(-1, ((1,),), DIGEST)
    with pytest.raises(ValueError, match="empty clauses"):
        CnfFormula(1, ((),), DIGEST)
    with pytest.raises(ValueError, match="duplicate"):
        CnfFormula(2, ((1, 1),), DIGEST)
    with pytest.raises(ValueError, match="variable range"):
        CnfFormula(2, ((3,),), DIGEST)


def test_cnf_defensively_freezes_clauses_and_validates_provenance() -> None:
    clauses = [[1]]
    formula = CnfFormula(1, clauses, DIGEST)  # type: ignore[arg-type]
    digest = formula.digest
    clauses[0][0] = -1

    assert formula.clauses == ((1,),)
    assert formula.digest == digest
    with pytest.raises(ValueError, match="problem_digest"):
        CnfFormula(1, ((1,),), "not-a-digest")
    with pytest.raises(ValueError, match="encoder_id"):
        CnfFormula(1, ((1,),), DIGEST, "bad\nheader")


@pytest.mark.parametrize(
    "formula",
    [
        (True, ((1,),)),
        (1.5, ((1,),)),
        (1, ((True,),)),
        (1, ((1.0,),)),
    ],
)
def test_cnf_rejects_noninteger_counts_and_literals(formula: tuple[object, object]) -> None:
    with pytest.raises(ValueError):
        CnfFormula(formula[0], formula[1], DIGEST)  # type: ignore[arg-type]


def test_empty_problem_encodes_as_standard_empty_dimacs() -> None:
    problem = ColoringProblem((), 0, ())
    formula = encode_onehot_pairwise(problem)

    assert formula.variable_count == 0
    assert formula.clauses == ()
    assert formula.to_dimacs().endswith("p cnf 0 0\n")
    assert decode_positive_model(problem, set()) == ()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 999])
def test_decode_rejects_foreign_or_malformed_positive_variables(value: object) -> None:
    problem = ColoringProblem(("x",), 1, ())

    with pytest.raises(ValueError, match="positive SAT variable"):
        decode_positive_model(problem, {value})  # type: ignore[arg-type]


def hashlib_sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()
