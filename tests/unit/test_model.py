from __future__ import annotations

import pytest

from total_coloring.model import ColoringProblem


def test_problem_compiles_all_different_into_semantic_conflicts() -> None:
    problem = ColoringProblem(
        item_names=("a", "b", "c"),
        color_count=3,
        conflicts=((0, 1),),
        all_different=((0, 1, 2),),
        metadata=(("kind", "fixture"),),
    )

    assert problem.neighbor_masks == (0b110, 0b101, 0b011)
    assert problem.verify_assignment((0, 1, 2)) == ()
    assert problem.verify_assignment((0, 1, 0)) == ("conflicting items 0 and 2 both use color 0",)
    assert len(problem.semantic_digest) == 64


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"item_names": ("x", "x"), "color_count": 2, "conflicts": ()},
            "item names must be unique",
        ),
        (
            {"item_names": ("x",), "color_count": 0, "conflicts": ()},
            "positive when items exist",
        ),
        (
            {"item_names": ("x", "y"), "color_count": 2, "conflicts": ((1, 0),)},
            "canonical pairs",
        ),
        (
            {"item_names": ("x", "y"), "color_count": 2, "conflicts": ((0, 0),)},
            "cannot conflict with itself",
        ),
        (
            {
                "item_names": ("x", "y"),
                "color_count": 2,
                "conflicts": (),
                "all_different": ((0,),),
            },
            "at least two",
        ),
        (
            {
                "item_names": ("x", "y"),
                "color_count": 2,
                "conflicts": (),
                "fixed_colors": ((0, 2),),
            },
            "fixed color",
        ),
    ],
)
def test_problem_rejects_noncanonical_inputs(kwargs: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ColoringProblem(**kwargs)  # type: ignore[arg-type]


def test_assignment_verifier_reports_shape_type_range_conflict_and_fixed_color() -> None:
    problem = ColoringProblem(
        item_names=("x", "y"),
        color_count=2,
        conflicts=((0, 1),),
        fixed_colors=((0, 0),),
    )

    assert problem.verify_assignment((0,)) == ("expected 2 colors, found 1",)
    assert problem.verify_assignment((False, 1)) == ("item 0 has non-integer color False",)
    assert problem.verify_assignment((2, 1)) == ("item 0 has out-of-range color 2",)
    assert problem.verify_assignment((1, 1)) == (
        "conflicting items 0 and 1 both use color 1",
        "item 0 must use fixed color 0, found 1",
    )


def test_problem_defensively_freezes_inputs_and_splits_digests() -> None:
    names = ["a", "b"]
    metadata = [["producer", "test"]]
    problem = ColoringProblem(
        item_names=names,  # type: ignore[arg-type]
        color_count=2,
        conflicts=[[0, 1]],  # type: ignore[arg-type]
        metadata=metadata,  # type: ignore[arg-type]
    )
    semantic_digest = problem.semantic_digest
    representation_digest = problem.representation_digest

    names.append("c")
    metadata[0][1] = "changed"

    assert problem.item_names == ("a", "b")
    assert problem.metadata == (("producer", "test"),)
    assert problem.semantic_digest == semantic_digest
    assert problem.representation_digest == representation_digest


def test_semantic_digest_uses_effective_constraints_not_representation() -> None:
    explicit = ColoringProblem(("a", "b"), 2, ((0, 1),), metadata=(("source", "a"),))
    grouped = ColoringProblem(
        ("x", "y"),
        2,
        (),
        all_different=((0, 1),),
        metadata=(("source", "b"),),
    )

    assert explicit.semantic_digest == grouped.semantic_digest
    assert explicit.representation_digest != grouped.representation_digest


def test_empty_problem_is_a_valid_trivial_instance() -> None:
    problem = ColoringProblem(item_names=(), color_count=0, conflicts=())

    assert problem.item_count == 0
    assert problem.verify_assignment(()) == ()


@pytest.mark.parametrize("color_count", [True, 1.5, -1])
def test_problem_rejects_malformed_color_counts(color_count: object) -> None:
    with pytest.raises(ValueError):
        ColoringProblem(item_names=(), color_count=color_count, conflicts=())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"item_names": 3, "conflicts": ()}, "item_names must be iterable"),
        ({"item_names": ("x",), "conflicts": 3}, "conflicts must be iterable"),
        ({"item_names": ("x",), "conflicts": (1,)}, "two-item pair"),
        ({"item_names": ("x",), "conflicts": ((0,),)}, "two-item pair"),
        ({"item_names": ("x",), "conflicts": (), "all_different": 3}, "must be iterable"),
        ({"item_names": ("x",), "conflicts": (), "all_different": (1,)}, "group"),
        ({"item_names": ("x",), "conflicts": (), "fixed_colors": 3}, "must be iterable"),
        ({"item_names": ("x",), "conflicts": (), "fixed_colors": (1,)}, "two-item pair"),
        ({"item_names": ("x",), "conflicts": (), "metadata": 3}, "must be iterable"),
        ({"item_names": ("x",), "conflicts": (), "metadata": (1,)}, "key/value pair"),
        (
            {"item_names": ("x",), "conflicts": (), "metadata": ((1, "value"),)},
            "must be strings",
        ),
        (
            {
                "item_names": ("x",),
                "conflicts": (),
                "metadata": (("b", "2"), ("a", "1")),
            },
            "unique and sorted",
        ),
    ],
)
def test_problem_rejects_malformed_nested_collections(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ColoringProblem(color_count=1, **kwargs)  # type: ignore[arg-type]
