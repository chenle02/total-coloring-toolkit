"""Canonical finite coloring problems shared by all solver backends."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from typing import Any


def _canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ColoringProblem:
    """A finite coloring problem with explicit conflicts and side constraints.

    Items are stable integer positions into ``item_names``. Conflicts are
    canonical pairs ``(lower, higher)``. An all-different group is retained as
    semantic metadata and also compiled into pairwise conflicts for the
    reference solver.
    """

    item_names: tuple[str, ...]
    color_count: int
    conflicts: tuple[tuple[int, int], ...]
    all_different: tuple[tuple[int, ...], ...] = ()
    fixed_colors: tuple[tuple[int, int], ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    _neighbor_masks: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _effective_conflicts: tuple[tuple[int, int], ...] = field(init=False, repr=False, compare=False)
    _semantic_digest: str = field(init=False, repr=False, compare=False)
    _representation_digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            item_names = tuple(self.item_names)
        except TypeError as exc:
            raise ValueError("item_names must be iterable") from exc
        object.__setattr__(self, "item_names", item_names)
        item_count = len(item_names)
        if len(set(item_names)) != item_count:
            raise ValueError("item names must be unique")
        if any(not isinstance(name, str) or not name for name in item_names):
            raise ValueError("item names must be non-empty strings")
        if (
            isinstance(self.color_count, bool)
            or not isinstance(self.color_count, int)
            or self.color_count < 0
            or (item_count > 0 and self.color_count == 0)
        ):
            raise ValueError(
                "color_count must be a nonnegative integer and positive when items exist"
            )

        try:
            raw_conflicts = tuple(self.conflicts)
        except TypeError as exc:
            raise ValueError("conflicts must be iterable") from exc
        checked_conflicts: list[tuple[int, int]] = []
        for raw_conflict in raw_conflicts:
            try:
                conflict_pair = tuple(raw_conflict)
            except TypeError as exc:
                raise ValueError("each conflict must be a two-item pair") from exc
            if len(conflict_pair) != 2:
                raise ValueError("each conflict must be a two-item pair")
            raw_left, raw_right = conflict_pair
            left = _item_index(raw_left, item_count, "conflict endpoint")
            right = _item_index(raw_right, item_count, "conflict endpoint")
            if left == right:
                raise ValueError("an item cannot conflict with itself")
            checked_conflicts.append((left, right))
        explicit_conflicts = tuple(checked_conflicts)
        if (
            any(left >= right for left, right in explicit_conflicts)
            or tuple(sorted(set(explicit_conflicts))) != explicit_conflicts
        ):
            raise ValueError("conflicts must be unique canonical pairs in sorted order")
        object.__setattr__(self, "conflicts", explicit_conflicts)
        effective_conflicts = set(explicit_conflicts)

        try:
            raw_groups = tuple(self.all_different)
        except TypeError as exc:
            raise ValueError("all_different must be iterable") from exc
        checked_groups: list[tuple[int, ...]] = []
        for raw_group in raw_groups:
            try:
                group = tuple(raw_group)
            except TypeError as exc:
                raise ValueError("each all-different group must be iterable") from exc
            checked = tuple(_item_index(item, item_count, "all-different item") for item in group)
            if len(checked) < 2:
                raise ValueError("an all-different group needs at least two distinct items")
            checked_groups.append(checked)
            effective_conflicts.update(itertools.combinations(checked, 2))
        groups = tuple(checked_groups)
        if tuple(sorted(set(tuple(sorted(set(group))) for group in groups))) != groups:
            raise ValueError("all-different groups must be unique, internally sorted, and sorted")
        object.__setattr__(self, "all_different", groups)

        fixed_by_item: dict[int, int] = {}
        try:
            raw_fixed_colors = tuple(self.fixed_colors)
        except TypeError as exc:
            raise ValueError("fixed_colors must be iterable") from exc
        checked_fixed: list[tuple[int, int]] = []
        for raw_fixed in raw_fixed_colors:
            try:
                fixed_pair = tuple(raw_fixed)
            except TypeError as exc:
                raise ValueError("each fixed color must be a two-item pair") from exc
            if len(fixed_pair) != 2:
                raise ValueError("each fixed color must be a two-item pair")
            raw_item, raw_color = fixed_pair
            item = _item_index(raw_item, item_count, "fixed-color item")
            color = _color_index(raw_color, self.color_count)
            if item in fixed_by_item:
                raise ValueError(f"item {item} has more than one fixed color")
            fixed_by_item[item] = color
            checked_fixed.append((item, color))
        fixed_colors = tuple(checked_fixed)
        if tuple(sorted(fixed_by_item.items())) != fixed_colors:
            raise ValueError("fixed colors must be sorted by unique item index")
        object.__setattr__(self, "fixed_colors", fixed_colors)

        try:
            raw_metadata_entries = tuple(self.metadata)
        except TypeError as exc:
            raise ValueError("metadata must be iterable") from exc
        checked_metadata: list[tuple[str, str]] = []
        for raw_metadata in raw_metadata_entries:
            try:
                metadata_pair = tuple(raw_metadata)
            except TypeError as exc:
                raise ValueError("each metadata entry must be a key/value pair") from exc
            if len(metadata_pair) != 2:
                raise ValueError("each metadata entry must be a key/value pair")
            key, value = metadata_pair
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("metadata keys and values must be strings")
            checked_metadata.append((key, value))
        metadata = tuple(checked_metadata)
        metadata_keys = [key for key, _value in metadata]
        if len(set(metadata_keys)) != len(metadata_keys) or metadata_keys != sorted(metadata_keys):
            raise ValueError("metadata keys must be unique and sorted")
        object.__setattr__(self, "metadata", metadata)

        masks = [0] * item_count
        canonical_effective = tuple(sorted(effective_conflicts))
        for left, right in canonical_effective:
            masks[left] |= 1 << right
            masks[right] |= 1 << left
        object.__setattr__(self, "_neighbor_masks", tuple(masks))
        object.__setattr__(self, "_effective_conflicts", canonical_effective)
        object.__setattr__(self, "_semantic_digest", _canonical_digest(self.to_semantic_dict()))
        object.__setattr__(
            self, "_representation_digest", _canonical_digest(self.to_representation_dict())
        )

    @property
    def item_count(self) -> int:
        return len(self.item_names)

    @property
    def neighbor_masks(self) -> tuple[int, ...]:
        """Bit mask of every item's semantic conflict neighborhood."""

        return self._neighbor_masks

    @property
    def semantic_digest(self) -> str:
        """SHA-256 of the canonical, producer-independent problem semantics."""

        return self._semantic_digest

    @property
    def representation_digest(self) -> str:
        """SHA-256 of labels, encodings, metadata, and effective semantics."""

        return self._representation_digest

    def to_semantic_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "item_count": self.item_count,
            "color_count": self.color_count,
            "conflicts": [list(pair) for pair in self._effective_conflicts],
            "fixed_colors": [list(item) for item in self.fixed_colors],
        }

    def to_representation_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "item_names": list(self.item_names),
            "semantic_digest": self.semantic_digest,
            "explicit_conflicts": [list(pair) for pair in self.conflicts],
            "all_different": [list(group) for group in self.all_different],
            "metadata": {key: value for key, value in self.metadata},
        }

    def verify_assignment(self, assignment: tuple[int, ...]) -> tuple[str, ...]:
        """Return semantic assignment violations; an empty tuple means valid."""

        violations: list[str] = []
        if len(assignment) != self.item_count:
            return (f"expected {self.item_count} colors, found {len(assignment)}",)
        for item, color in enumerate(assignment):
            if isinstance(color, bool) or not isinstance(color, int):
                violations.append(f"item {item} has non-integer color {color!r}")
            elif not 0 <= color < self.color_count:
                violations.append(f"item {item} has out-of-range color {color}")
        if violations:
            return tuple(violations)

        for left in range(self.item_count):
            mask = self._neighbor_masks[left] & ~((1 << (left + 1)) - 1)
            while mask:
                least_bit = mask & -mask
                right = least_bit.bit_length() - 1
                if assignment[left] == assignment[right]:
                    violations.append(
                        f"conflicting items {left} and {right} both use color {assignment[left]}"
                    )
                mask ^= least_bit
        for item, color in self.fixed_colors:
            if assignment[item] != color:
                violations.append(
                    f"item {item} must use fixed color {color}, found {assignment[item]}"
                )
        return tuple(violations)


def _item_index(value: int, item_count: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < item_count:
        raise ValueError(f"{label} must be an integer in [0, {item_count})")
    return value


def _color_index(value: int, color_count: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < color_count:
        raise ValueError(f"fixed color must be an integer in [0, {color_count})")
    return value
