from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from itertools import product
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "research" / "paired_hole_orbit.py"
    spec = importlib.util.spec_from_file_location("paired_hole_orbit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_alpha_frontier_audit() -> ModuleType:
    path = (
        Path(__file__).parents[2] / "scripts" / "research" / "paired_hole_alpha_frontier_audit.py"
    )
    spec = importlib.util.spec_from_file_location("paired_hole_alpha_frontier_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_canonical_vertex_profiles_are_the_complete_normalized_sixty_four() -> None:
    module = load_script()
    profiles = module.canonical_vertex_colour_profiles()
    expected = set()
    for u, v, w, z in product((2, 3, 4), (2, 3, 4), (1, 5, 6), (1, 5, 6)):
        prefix = (1, 2, u, v, w, z)
        if any(prefix.count(colour) > 2 for colour in range(1, 7)):
            continue
        outside = tuple(colour for colour in range(1, 7) for _ in range(2 - prefix.count(colour)))
        expected.add((*prefix, *outside))

    assert len(profiles) == len(set(profiles)) == 64
    assert set(profiles) == expected
    assert all(profile[:2] == (1, 2) for profile in profiles)
    assert all(profile[6:] == tuple(sorted(profile[6:])) for profile in profiles)
    assert all(all(profile.count(colour) == 2 for colour in range(1, 7)) for profile in profiles)


def test_matching_enumerators_are_unique_and_complete_on_four_vertices() -> None:
    module = load_script()
    vertex_colours = (1, 2, 3, 4, 5, 6)
    perfect = tuple(module.perfect_matchings((2, 3, 4, 5), vertex_colours))
    partial = tuple(module.partial_matchings((2, 3, 4, 5), vertex_colours, frozenset()))

    assert len(perfect) == len(set(perfect)) == 3
    assert len(partial) == len(set(partial)) == 10
    assert set(perfect) <= set(partial)
    assert () in partial


def test_nonperfect_alpha_has_a_fixed_terminal_coverage_obstruction() -> None:
    module = load_script()
    alpha_matching = tuple(sorted({(0, 7), (1, 6), (2, 4), (3, 5), (10, 11)}))

    failure = module.alpha_terminal_coverage_failure(
        alpha_matching,
        module.FROZEN_VERTEX_COLOURS,
    )

    assert failure == module.AlphaTerminalCoverageFailure(
        terminal=8,
        fixed_colour=3,
        distinguished_holes=(0, 5),
    )
    assert all(
        not module.blocks_distinguished_holes(
            alpha_matching,
            failure.fixed_colour,
            beta_matching,
            module.FROZEN_VERTEX_COLOURS,
        )
        for beta_matching in module.direct_colour_matchings(
            failure.fixed_colour,
            alpha_matching,
            module.FROZEN_VERTEX_COLOURS,
        )
    )


def test_terminal_failure_histogram_is_sorted_typed_and_sum_checked() -> None:
    module = load_script()
    histogram = module.canonical_terminal_failure_histogram(
        {(8, 3): 2, (0, 1): 5},
        expected_total=7,
    )

    assert histogram == [
        {
            "count": 5,
            "distinguished_holes": [2, 3],
            "fixed_colour": 1,
            "terminal": 0,
        },
        {
            "count": 2,
            "distinguished_holes": [0, 5],
            "fixed_colour": 3,
            "terminal": 8,
        },
    ]
    with pytest.raises(RuntimeError, match="does not match prune count"):
        module.canonical_terminal_failure_histogram({(0, 1): 1}, expected_total=2)


def test_independent_alpha_frontier_counts_exact_stage_partition() -> None:
    audit = load_alpha_frontier_audit()
    receipt = audit.build_receipt()

    assert receipt["profile_count"] == 64
    assert receipt["stage_partition"] == {
        "admissible_for_edge_search": 95_500,
        "nonperfect_terminal_coverage_prunes": 5_181_504,
        "perfect_distinguished_hole_link_prunes": 116_500,
        "perfect_forced_fan_conflict_prunes": 135_904,
    }
    totals = receipt["totals"]
    assert totals["proper_partial_alpha_matchings"] == 5_529_408
    assert totals["proper_perfect_alpha_matchings"] == 347_904
    assert {row["proper_partial_alpha_matchings"] for row in receipt["profile_rows"]} == {86_397}
    assert {row["proper_perfect_alpha_matchings"] for row in receipt["profile_rows"]} == {5_436}


def test_one_profile_streaming_alpha_stages_agree_with_independent_audit() -> None:
    module = load_script()
    audit = load_alpha_frontier_audit()
    profiles = module.canonical_vertex_colour_profiles()
    profile_index = profiles.index(module.FROZEN_VERTEX_COLOURS)
    row = audit.build_receipt()["profile_rows"][profile_index]

    stages = Counter(
        module.classify_alpha_frontier_work_unit(matching, module.FROZEN_VERTEX_COLOURS).stage
        for matching in module.alpha_matchings_for_scope(
            "all-partial", module.FROZEN_VERTEX_COLOURS
        )
    )

    assert stages == {
        "nonperfect_terminal_coverage_prune": row["nonperfect_terminal_coverage_prunes"],
        "perfect_forced_fan_conflict_prune": row["perfect_forced_fan_conflict_prunes"],
        "perfect_distinguished_hole_link_prune": row["perfect_distinguished_hole_link_prunes"],
        "admissible_for_edge_search": row["admissible_for_edge_search"],
    }
    assert sum(stages.values()) == row["proper_partial_alpha_matchings"]
    assert (
        stages["perfect_forced_fan_conflict_prune"]
        + stages["perfect_distinguished_hole_link_prune"]
        + stages["admissible_for_edge_search"]
        == row["proper_perfect_alpha_matchings"]
    )
    assert (
        stages["nonperfect_terminal_coverage_prune"]
        == row["proper_partial_alpha_matchings"] - row["proper_perfect_alpha_matchings"]
    )


def test_all_partial_canary_ordinals_are_stable_within_frozen_profile() -> None:
    module = load_script()
    profiles = module.canonical_vertex_colour_profiles()
    profile_index = profiles.index(module.FROZEN_VERTEX_COLOURS)
    near_perfect = tuple(sorted({(0, 7), (1, 6), (2, 4), (3, 5), (10, 11)}))
    cross_first = tuple(sorted({(0, 8), (1, 10), (2, 11), (3, 6), (4, 9), (5, 7)}))
    local_ordinals: dict[tuple[tuple[int, int], ...], int] = {}
    for ordinal, matching in enumerate(
        module.alpha_matchings_for_scope("all-partial", module.FROZEN_VERTEX_COLOURS)
    ):
        canonical = tuple(sorted(matching))
        if canonical in {near_perfect, cross_first}:
            local_ordinals[canonical] = ordinal

    assert profile_index == 36
    assert local_ordinals[near_perfect] == 56_442
    assert local_ordinals[cross_first] == 65_190
    assert profile_index * 86_397 + local_ordinals[near_perfect] == 3_166_734
    assert profile_index * 86_397 + local_ordinals[cross_first] == 3_175_482


def test_pinned_one_work_unit_canary_ordinal_is_stable() -> None:
    module = load_script()
    profiles = module.canonical_vertex_colour_profiles()
    profile_index = profiles.index(module.FROZEN_VERTEX_COLOURS)
    target_alpha = tuple(sorted({(0, 8), (1, 10), (2, 5), (3, 4), (6, 7), (9, 11)}))
    alpha_profile_ordinal = next(
        index
        for index, matching in enumerate(
            module.perfect_matchings(tuple(range(12)), module.FROZEN_VERTEX_COLOURS)
        )
        if tuple(sorted(matching)) == target_alpha
    )

    assert profile_index == 36
    assert alpha_profile_ordinal == 3_490
    assert profile_index * 5_436 + alpha_profile_ordinal == 199_186


def test_pinned_cross_first_canary_ordinal_is_stable() -> None:
    module = load_script()
    profiles = module.canonical_vertex_colour_profiles()
    profile_index = profiles.index(module.FROZEN_VERTEX_COLOURS)
    target_alpha = tuple(sorted({(0, 8), (1, 10), (2, 11), (3, 6), (4, 9), (5, 7)}))
    alpha_profile_ordinal = next(
        index
        for index, matching in enumerate(
            module.perfect_matchings(tuple(range(12)), module.FROZEN_VERTEX_COLOURS)
        )
        if tuple(sorted(matching)) == target_alpha
    )

    assert profile_index == 36
    assert alpha_profile_ordinal == 3_542
    assert profile_index * 5_436 + alpha_profile_ordinal == 199_238


def test_configured_orbit_includes_every_role_pair_after_stable_prefix() -> None:
    module = load_script()
    preferred = tuple((module.ALPHA, beta) for beta in module.NON_ALPHA) + tuple(
        (a, b) for a in (3, 4) for b in (5, 6)
    )
    configured = module.configured_first_move_pairs()

    assert configured[: len(preferred)] == preferred
    assert set(configured) == set(module.combinations(module.PALETTE, 2))
    assert (1, 3) in configured
    assert (2, 6) in configured
    assert module.historical_move_pairs() == preferred


def test_vertex_hole_first_pattern_is_classified_explicitly() -> None:
    module = load_script()
    proposal = module.OrbitProposal(
        status="proposed_release",
        explored_states=3,
        depth=2,
        common_missing_colour=6,
        moves=(
            module.OrbitMove(colours=(1, 3), component_edges=(), component_walk=()),
            module.OrbitMove(colours=(3, 6), component_edges=(), component_walk=()),
        ),
    )

    assert module.orbit_pattern(proposal) == ("vertex_hole_role_then_cross_two_swap_release")


def test_frozen_missing_profile_and_parity_enumerator_are_exact() -> None:
    module = load_script()
    frozen = tuple(
        module.configured_missing_profiles(
            "frozen",
            module.FROZEN_VERTEX_COLOURS,
            (2,),
        )
    )
    assert frozen == (module.FROZEN_MISSING,)

    profiles = tuple(module.outside_missing_profiles(module.FROZEN_VERTEX_COLOURS, (2,)))
    assert len(profiles) == len(set(profiles)) == 31_360
    for profile in profiles:
        assert profile[:6] == module.SPECIAL_MISSING
        assert all(len(missing) == 2 for missing in profile)
        assert all(sum(colour in missing for missing in profile) % 2 == 0 for colour in range(1, 7))


def test_frozen_profile_reproduces_eight_unique_one_swap_states(tmp_path: Path) -> None:
    module = load_script()
    output = tmp_path / "frozen"
    result = module.run_search(
        module.SearchConfig(
            output_dir=output,
            profile_scope="frozen",
            orbit_max_depth=2,
            orbit_max_states=1_000,
        )
    )
    records = read_records(output / "candidates.jsonl")

    assert result["status"] == "complete_generation"
    assert result["input_exhausted"] is True
    assert (output / "completion.json").is_file()
    assert len(records) == 8
    assert len({record["candidate_fingerprint"] for record in records}) == 8
    assert all(record["orbit_proposal"]["status"] == "proposed_release" for record in records)
    assert all(record["orbit_proposal"]["depth"] == 1 for record in records)
    assert all(record["residual_classification"] == "easy_exit_present" for record in records)
    assert all(
        record["schema_version"] == "total-coloring.paired-hole-orbit-candidate.v2"
        for record in records
    )
    assert all(
        record["run_config_fingerprint"] == result["config_fingerprint"] for record in records
    )
    assert all(record["work_unit"]["alpha_scope"] == "perfect" for record in records)
    assert all(
        {cross_exit["root"] for cross_exit in record["easy_exits"]["direct_cross"]} == {0, 1}
        for record in records
    )
    raw_state = records[0]["raw_state"]
    assert set(raw_state) == {
        "alpha",
        "degree_parameter",
        "edge_colors",
        "graph",
        "kind",
        "palette_size",
        "roles",
        "schema_version",
        "uncolored_edge",
        "vertex_colors",
    }
    assert raw_state["schema_version"] == "total-coloring.paired-hole-state.v1"
    assert raw_state["graph"]["schema_version"] == "total-coloring.simple-graph.v1"
    assert raw_state["graph"]["edges"] == sorted(raw_state["graph"]["edges"])
    assert len(raw_state["edge_colors"]) == len(raw_state["graph"]["edges"])
    assert sum(colour is None for colour in raw_state["edge_colors"]) == 1
    assert records[0]["candidate_fingerprint"] == module.sha256_bytes(raw_state)
    assert (
        json.loads((output / "run.json").read_text())[
            "alpha_terminal_coverage_first_failure_histogram"
        ]
        == []
    )
    assert result["alpha_terminal_coverage_first_failure_histogram"] == []

    counts = result["counts"]
    assert counts["vertex_colour_profiles_seen"] == 1
    assert counts["alpha_matchings_seen"] == 5_436
    assert counts["alpha_matchings_assigned_to_shard"] == 5_436
    assert counts["alpha_work_units_completed"] == 5_436
    assert counts["explicit_missing_profiles_seen"] == 5_436
    assert counts["candidate_states_emitted"] == 8
    assert counts["proposed_releases"] == 8
    assert counts["states_with_direct_cross_exit"] == 8
    assert counts["hard_residual_candidates"] == 0


def test_single_alpha_edge_is_not_misclassified_as_terminal_release() -> None:
    module = load_script()
    state = {(2, 6): 0}
    assert (
        module.state_missing_sets(module.FROZEN_VERTEX_COLOURS, state)[2]
        & (module.state_missing_sets(module.FROZEN_VERTEX_COLOURS, state)[6])
    )
    assert module.terminal_releases(module.FROZEN_VERTEX_COLOURS, state) == ()


def test_alpha_shards_form_a_disjoint_union_of_frozen_states(tmp_path: Path) -> None:
    module = load_script()
    full_output = tmp_path / "full"
    module.run_search(
        module.SearchConfig(
            output_dir=full_output,
            profile_scope="frozen",
            orbit_max_depth=1,
            orbit_max_states=200,
        )
    )
    full = {
        record["candidate_fingerprint"] for record in read_records(full_output / "candidates.jsonl")
    }
    shard_sets = []
    assigned = []
    for shard_index in range(2):
        output = tmp_path / f"shard-{shard_index}"
        result = module.run_search(
            module.SearchConfig(
                output_dir=output,
                profile_scope="frozen",
                shard_index=shard_index,
                shard_count=2,
                orbit_max_depth=1,
                orbit_max_states=200,
            )
        )
        shard_sets.append(
            {
                record["candidate_fingerprint"]
                for record in read_records(output / "candidates.jsonl")
            }
        )
        assigned.append(result["counts"]["alpha_matchings_assigned_to_shard"])

    assert shard_sets[0].isdisjoint(shard_sets[1])
    assert shard_sets[0] | shard_sets[1] == full
    assert assigned == [2_718, 2_718]


def test_bounded_and_interrupted_runs_never_write_completion(tmp_path: Path) -> None:
    module = load_script()
    bounded_output = tmp_path / "bounded"
    bounded = module.run_search(
        module.SearchConfig(
            output_dir=bounded_output,
            profile_scope="frozen",
            max_alpha_work_units=1,
        )
    )
    assert bounded["status"] == "bounded_generation"
    assert bounded["stop_reason"] == "max_alpha_work_units"
    assert bounded["input_exhausted"] is False
    assert bounded["counts"]["alpha_work_units_completed"] == 1
    assert not (bounded_output / "completion.json").exists()

    initial_cap_output = tmp_path / "initial-cap"
    initial_cap = module.run_search(
        module.SearchConfig(
            output_dir=initial_cap_output,
            profile_scope="frozen",
            max_initial_states=1,
        )
    )
    assert initial_cap["status"] == "bounded_generation"
    assert initial_cap["stop_reason"] == "max_initial_states"
    assert initial_cap["counts"]["initially_blocked_states_generated"] == 1
    assert not (initial_cap_output / "completion.json").exists()

    interrupted_output = tmp_path / "interrupted"
    interrupted = module.run_search(
        module.SearchConfig(output_dir=interrupted_output, profile_scope="frozen"),
        should_interrupt=lambda: True,
    )
    assert interrupted["status"] == "interrupted"
    assert interrupted["stop_reason"] == "interrupt_requested"
    assert interrupted["input_exhausted"] is False
    assert interrupted["counts"]["alpha_work_units_completed"] == 0
    assert not (interrupted_output / "completion.json").exists()
    assert json.loads((interrupted_output / "checkpoint.json").read_text()) == interrupted


def test_all_partial_scope_records_terminal_coverage_prune_before_edge_search(
    tmp_path: Path,
) -> None:
    module = load_script()
    output = tmp_path / "all-partial-bounded"
    result = module.run_search(
        module.SearchConfig(
            output_dir=output,
            profile_scope="canonical-fan",
            alpha_scope="all-partial",
            max_alpha_work_units=1,
            checkpoint_interval=1,
        )
    )

    assert result["status"] == "bounded_generation"
    assert result["stop_reason"] == "max_alpha_work_units"
    counts = result["counts"]
    assert counts["alpha_matchings_assigned_to_shard"] == 1
    assert counts["alpha_work_units_completed"] == 1
    assert counts["alpha_nonperfect_terminal_coverage_prunes"] == 1
    assert counts["alpha_perfect_work_units"] == 0
    assert counts["alpha_admissible_for_edge_search"] == 0
    assert counts["edge_colour_matchings_generated"] == 0
    assert counts["candidate_states_emitted"] == 0
    histogram = result["alpha_terminal_coverage_first_failure_histogram"]
    assert histogram == [
        {
            "count": 1,
            "distinguished_holes": [2, 3],
            "fixed_colour": 1,
            "terminal": 0,
        }
    ]
    assert (
        sum(row["count"] for row in histogram)
        == counts["alpha_nonperfect_terminal_coverage_prunes"]
    )
    assert (
        json.loads((output / "checkpoint.json").read_text())[
            "alpha_terminal_coverage_first_failure_histogram"
        ]
        == histogram
    )
    assert (output / "candidates.jsonl").read_bytes() == b""
    assert not (output / "completion.json").exists()


def test_near_perfect_singleton_canary_records_only_aggregate_first_failure(
    tmp_path: Path,
) -> None:
    module = load_script()
    output = tmp_path / "near-perfect-canary"
    result = module.run_search(
        module.SearchConfig(
            output_dir=output,
            profile_scope="canonical-fan",
            alpha_scope="all-partial",
            shard_count=5_529_408,
            shard_index=3_166_734,
            checkpoint_interval=1,
        )
    )

    assert result["status"] == "complete_generation"
    assert result["counts"]["alpha_matchings_seen"] == 5_529_408
    assert result["counts"]["alpha_matchings_assigned_to_shard"] == 1
    assert result["counts"]["alpha_nonperfect_terminal_coverage_prunes"] == 1
    assert result["counts"]["candidate_states_emitted"] == 0
    assert result["alpha_terminal_coverage_first_failure_histogram"] == [
        {
            "count": 1,
            "distinguished_holes": [0, 5],
            "fixed_colour": 3,
            "terminal": 8,
        }
    ]
    assert (output / "candidates.jsonl").read_bytes() == b""
    assert (
        json.loads((output / "run.json").read_text())[
            "alpha_terminal_coverage_first_failure_histogram"
        ]
        == []
    )


def test_output_directory_must_be_new_and_scope_limits_are_explicit(tmp_path: Path) -> None:
    module = load_script()
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="must not already exist"):
        module.run_search(module.SearchConfig(output_dir=existing, profile_scope="frozen"))

    with pytest.raises(ValueError, match="frozen scope requires"):
        module.SearchConfig(
            output_dir=tmp_path / "invalid-frozen",
            profile_scope="frozen",
            outside_missing_sizes=(2, 3),
        ).validate()
    with pytest.raises(ValueError, match="applies only"):
        module.SearchConfig(
            output_dir=tmp_path / "invalid-direct",
            profile_scope="canonical-fan",
            max_missing_profiles=1,
        ).validate()
    with pytest.raises(ValueError, match="all-partial alpha scope requires"):
        module.SearchConfig(
            output_dir=tmp_path / "invalid-all-partial-frozen",
            profile_scope="frozen",
            alpha_scope="all-partial",
        ).validate()
    with pytest.raises(ValueError, match="all-partial alpha scope requires"):
        module.SearchConfig(
            output_dir=tmp_path / "invalid-all-partial-missing-sizes",
            profile_scope="canonical-fan",
            alpha_scope="all-partial",
            outside_missing_sizes=(2, 3),
        ).validate()


def _state_from_colour_classes(
    colour_classes: dict[int, set[tuple[int, int]]],
) -> dict[tuple[int, int], int]:
    return {
        candidate: colour
        for colour, candidates in colour_classes.items()
        for candidate in candidates
    }


def test_lower_degree_cross_blocked_fixture_is_pruned_by_terminal_release() -> None:
    module = load_script()
    vertex_colours = (1, 2, 2, 3, 1, 5, 3, 4, 4, 5, 6, 6)
    colour_classes = {
        0: {(0, 6), (1, 7), (2, 4), (3, 5), (8, 10), (9, 11)},
        1: {(1, 9), (5, 6)},
        2: {(0, 10), (3, 7)},
        3: {(1, 4), (2, 8)},
        4: {(1, 5), (2, 9), (3, 11), (6, 10)},
        5: {(0, 2), (4, 8), (7, 11)},
        6: {(0, 3), (4, 6), (5, 8), (7, 9)},
    }
    state = _state_from_colour_classes(colour_classes)
    alpha_matching = tuple(sorted(colour_classes[0]))

    assert module.partial_state_is_valid(vertex_colours, state)
    assert all(
        module.blocks_distinguished_holes(
            alpha_matching,
            beta,
            tuple(sorted(colour_classes[beta])),
            vertex_colours,
        )
        for beta in range(1, 7)
    )
    assert module.direct_cross_exits(vertex_colours, state) == ()
    releases = module.terminal_releases(vertex_colours, state)
    assert {(release.beta, release.recolour_to) for release in releases} >= {
        (4, 1),
        (5, 2),
        (6, 1),
    }
    missing = module.state_missing_sets(vertex_colours, state)
    assert missing[10] == {1, 3, 5}
    assert missing[11] == {1, 2, 3}
    restricted_choices = {beta: (tuple(sorted(colour_classes[beta])),) for beta in range(1, 7)}
    assert list(
        module.compatible_direct_states(
            alpha_matching,
            restricted_choices,
            (2, 3),
        )
    ) == [state]

    proposal = module.propose_release(vertex_colours, state, max_depth=2, max_states=1_000)
    assert proposal.status == "proposed_release"
    assert proposal.depth == 2
    assert proposal.common_missing_colour == 5
    assert tuple(move.colours for move in proposal.moves) == ((0, 3), (3, 5))


def test_cross_terminal_release_is_checked_after_both_direct_roots_lock() -> None:
    module = load_script()
    vertex_colours = (1, 2, 2, 3, 1, 5, 3, 4, 4, 5, 6, 6)
    state = _state_from_colour_classes(
        {
            0: {(0, 6), (1, 9), (2, 7), (3, 5), (4, 10), (8, 11)},
            1: {(1, 11), (5, 6), (7, 10)},
            2: {(0, 11), (3, 7), (9, 10)},
            3: {(1, 4), (2, 8)},
            4: {(1, 5), (2, 9), (3, 10), (6, 11)},
            5: {(0, 2), (4, 8)},
            6: {(0, 3), (4, 6), (5, 7), (8, 9)},
        }
    )

    assert module.partial_state_is_valid(vertex_colours, state)
    assert module.terminal_releases(vertex_colours, state) == ()
    assert module.direct_cross_exits(vertex_colours, state) == ()
    releases = module.cross_terminal_releases(vertex_colours, state)
    assert {(release.colours, release.root, release.recolour_to) for release in releases} == {
        ((3, 6), 1, 2),
        ((4, 5), 0, 1),
        ((4, 6), 0, 5),
        ((4, 6), 1, 3),
    }


def test_post_terminal_lock_saturated_fixture_has_two_swap_orbit_exit() -> None:
    module = load_script()
    vertex_colours = module.FROZEN_VERTEX_COLOURS
    colour_classes = {
        0: {(0, 8), (1, 10), (2, 5), (3, 4), (6, 7), (9, 11)},
        1: {(1, 9), (4, 7), (5, 8)},
        2: {(0, 11), (2, 6), (3, 10)},
        3: {(1, 4), (3, 6), (10, 11)},
        4: {(1, 5), (2, 7), (8, 11)},
        5: {(0, 2), (5, 7), (8, 9)},
        6: {(0, 3), (4, 6), (9, 10)},
    }
    state = _state_from_colour_classes(colour_classes)
    alpha_matching = tuple(sorted(colour_classes[0]))

    assert module.partial_state_is_valid(vertex_colours, state)
    assert all(len(missing) == 2 for missing in module.state_missing_sets(vertex_colours, state))
    assert all(
        module.blocks_distinguished_holes(
            alpha_matching,
            beta,
            tuple(sorted(colour_classes[beta])),
            vertex_colours,
        )
        for beta in range(1, 7)
    )
    direct_choices = module.direct_blocked_matching_choices(alpha_matching, vertex_colours)
    assert all(tuple(sorted(colour_classes[beta])) in direct_choices[beta] for beta in range(1, 7))
    restricted_choices = {beta: (tuple(sorted(colour_classes[beta])),) for beta in range(1, 7)}
    assert list(
        module.compatible_direct_states(
            alpha_matching,
            restricted_choices,
            (2,),
        )
    ) == [state]
    assert module.terminal_releases(vertex_colours, state) == ()
    assert module.cross_terminal_releases(vertex_colours, state) == ()
    assert module.direct_cross_exits(vertex_colours, state) == ()
    topology = {item.colours: item.relation for item in module.cross_topologies(state)}
    assert topology[(3, 6)] == "coincident_xy"
    assert topology[(4, 5)] == "coincident_xy"

    proposal = module.propose_release(vertex_colours, state, max_depth=2, max_states=1_000)
    assert proposal.status == "proposed_release"
    assert proposal.depth == 2
    assert proposal.common_missing_colour == 6
    assert tuple(move.colours for move in proposal.moves) == ((0, 3), (3, 6))
    assert proposal.moves[0].component_walk == (7, 6, 3, 4, 1, 10, 11, 9)
    assert proposal.moves[1].component_walk == (0, 3, 4, 6, 7)
    assert module.fan_alignment(vertex_colours) == "exact_role_alignment"
    assert module.orbit_pattern(proposal) == "alpha_hole_role_then_cross_two_swap_release"
    detachment = module.detachment_analysis(
        state,
        proposal,
        module.cross_topologies(state),
    )
    assert detachment is not None
    assert detachment["pre_first_move_relation"] == "coincident_xy"
    assert detachment["post_first_move_relation"] == "distinct"
    assert detachment["shared_role_edges"]


def test_cross_cross_release_is_not_misclassified_as_alpha_first() -> None:
    module = load_script()
    vertex_colours = module.FROZEN_VERTEX_COLOURS
    state = _state_from_colour_classes(
        {
            0: {(0, 8), (1, 10), (2, 11), (3, 6), (4, 9), (5, 7)},
            1: {(1, 8), (4, 5), (10, 11)},
            2: {(0, 9), (2, 3), (8, 10)},
            3: {(1, 4), (3, 7), (6, 11)},
            4: {(1, 5), (2, 6), (8, 11)},
            5: {(0, 2), (5, 6), (7, 9)},
            6: {(0, 3), (4, 7), (9, 10)},
        }
    )

    assert module.terminal_releases(vertex_colours, state) == ()
    assert module.cross_terminal_releases(vertex_colours, state) == ()
    assert module.direct_cross_exits(vertex_colours, state) == ()
    proposal = module.propose_release(vertex_colours, state, max_depth=2, max_states=1_000)
    assert proposal.status == "proposed_release"
    assert tuple(move.colours for move in proposal.moves) == ((3, 5), (3, 6))
    assert proposal.moves[0].component_walk == (3, 7, 9)
    assert proposal.moves[1].component_walk == (0, 3)
    assert module.orbit_pattern(proposal) == "cross_role_then_cross_two_swap_release"
    assert module.detachment_analysis(state, proposal, module.cross_topologies(state)) is None
