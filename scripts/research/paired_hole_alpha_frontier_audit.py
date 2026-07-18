#!/usr/bin/env python3
"""Independently count the exact R=5 partial-alpha matching frontier.

This dependency-free audit does not import the paired-hole generator.  It
uses a bit-mask dynamic program rather than the generator's recursive stream,
and it counts only alpha matchings.  The result is finite computational
evidence for the campaign envelope, not a graph-colouring theorem.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from functools import cache
from itertools import product

ORDER = 12
XY = (0, 1)
NON_ALPHA = tuple(range(1, 7))
FAN_EDGES = frozenset({(1, 4), (1, 5), (0, 2), (0, 3)})
DISTINGUISHED_HOLE_PAIRS = frozenset({(2, 3), (4, 5), (0, 5), (0, 4), (1, 3), (1, 2)})
SCHEMA = "total-coloring.paired-hole-alpha-frontier-audit.v1"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_profiles() -> tuple[tuple[int, ...], ...]:
    profiles: list[tuple[int, ...]] = []
    for u, v, w, z in product((2, 3, 4), (2, 3, 4), (1, 5, 6), (1, 5, 6)):
        prefix = (1, 2, u, v, w, z)
        if any(prefix.count(colour) > 2 for colour in NON_ALPHA):
            continue
        outside = tuple(colour for colour in NON_ALPHA for _ in range(2 - prefix.count(colour)))
        profiles.append((*prefix, *outside))
    return tuple(profiles)


def _allowed(
    profile: tuple[int, ...],
    left: int,
    right: int,
    forbidden: frozenset[tuple[int, int]],
) -> bool:
    candidate = (left, right)
    return candidate != XY and candidate not in forbidden and profile[left] != profile[right]


def count_all_matchings(
    profile: tuple[int, ...],
    forbidden: frozenset[tuple[int, int]] = frozenset(),
) -> int:
    """Count every matching in the allowed graph by unmatched-vertex DP."""

    @cache
    def visit(mask: int) -> int:
        if mask == 0:
            return 1
        left_bit = mask & -mask
        left = left_bit.bit_length() - 1
        remaining = mask ^ left_bit
        total = visit(remaining)
        for right in range(left + 1, ORDER):
            right_bit = 1 << right
            if remaining & right_bit and _allowed(profile, left, right, forbidden):
                total += visit(remaining ^ right_bit)
        return total

    return visit((1 << ORDER) - 1)


def count_perfect_matchings(
    profile: tuple[int, ...],
    forbidden: frozenset[tuple[int, int]] = frozenset(),
) -> int:
    """Count perfect matchings independently, with no unmatched branch."""

    @cache
    def visit(mask: int) -> int:
        if mask == 0:
            return 1
        if mask.bit_count() % 2:
            return 0
        left_bit = mask & -mask
        left = left_bit.bit_length() - 1
        remaining = mask ^ left_bit
        total = 0
        for right in range(left + 1, ORDER):
            right_bit = 1 << right
            if remaining & right_bit and _allowed(profile, left, right, forbidden):
                total += visit(remaining ^ right_bit)
        return total

    return visit((1 << ORDER) - 1)


def build_receipt() -> dict[str, object]:
    profiles = canonical_profiles()
    rows: list[dict[str, object]] = []
    totals: Counter[str] = Counter()
    both_forbidden = FAN_EDGES | DISTINGUISHED_HOLE_PAIRS
    for profile_index, profile in enumerate(profiles):
        all_partial = count_all_matchings(profile)
        perfect = count_perfect_matchings(profile)
        no_fan = count_perfect_matchings(profile, FAN_EDGES)
        admissible = count_perfect_matchings(profile, both_forbidden)
        row = {
            "admissible_for_edge_search": admissible,
            "fixed_profile": list(profile),
            "perfect_distinguished_hole_link_prunes": no_fan - admissible,
            "perfect_forced_fan_conflict_prunes": perfect - no_fan,
            "profile_index": profile_index,
            "proper_partial_alpha_matchings": all_partial,
            "nonperfect_terminal_coverage_prunes": all_partial - perfect,
            "proper_perfect_alpha_matchings": perfect,
        }
        rows.append(row)
        totals["admissible_for_edge_search"] += admissible
        totals["nonperfect_terminal_coverage_prunes"] += all_partial - perfect
        totals["perfect_distinguished_hole_link_prunes"] += no_fan - admissible
        totals["perfect_forced_fan_conflict_prunes"] += perfect - no_fan
        totals["proper_partial_alpha_matchings"] += all_partial
        totals["proper_perfect_alpha_matchings"] += perfect

    partition = {
        "admissible_for_edge_search": totals["admissible_for_edge_search"],
        "nonperfect_terminal_coverage_prunes": totals["nonperfect_terminal_coverage_prunes"],
        "perfect_distinguished_hole_link_prunes": totals["perfect_distinguished_hole_link_prunes"],
        "perfect_forced_fan_conflict_prunes": totals["perfect_forced_fan_conflict_prunes"],
    }
    total = totals["proper_partial_alpha_matchings"]
    if sum(partition.values()) != total:
        raise RuntimeError("alpha-frontier stages do not partition the input")
    payload: dict[str, object] = {
        "claim_boundary": (
            "independent finite alpha-frontier counts only; not a coloring census or proof"
        ),
        "independent_method": "bit-mask dynamic programming over unmatched vertex sets",
        "profile_count": len(profiles),
        "profile_rows": rows,
        "profiles_sha256": hashlib.sha256(canonical_bytes(profiles)).hexdigest(),
        "schema_version": SCHEMA,
        "stage_partition": partition,
        "totals": dict(sorted(totals.items())),
    }
    payload["payload_sha256"] = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return payload


def main() -> int:
    print(canonical_bytes(build_receipt()).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
