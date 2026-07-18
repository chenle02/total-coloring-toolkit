from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]

import total_coloring.paired_hole as paired_hole
from total_coloring.graph import SimpleGraph
from total_coloring.paired_hole import (
    CrossComponentRelation,
    PairedHoleFormatError,
    PairedHoleRoles,
    PairedHoleState,
    PairedHoleStatus,
    ProfileAlignment,
    terminal_degree_lower_bound,
    terminal_release_is_applicable,
    verify_paired_hole_json,
    verify_paired_hole_state,
)
from total_coloring.schema_resources import read_schema_json

SHARP_VERTEX_COLORS = (1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5, 6)
SHARP_EDGE_COLORS = {
    (0, 7): 0,
    (1, 6): 0,
    (2, 9): 0,
    (3, 8): 0,
    (4, 11): 0,
    (5, 10): 0,
    (1, 8): 1,
    (4, 5): 1,
    (7, 9): 1,
    (0, 10): 2,
    (2, 3): 2,
    (6, 11): 2,
    (1, 4): 3,
    (3, 7): 3,
    (9, 10): 3,
    (1, 5): 4,
    (2, 11): 4,
    (7, 8): 4,
    (0, 2): 5,
    (5, 6): 5,
    (8, 11): 5,
    (0, 3): 6,
    (4, 9): 6,
    (6, 10): 6,
}


def state_from_coloring(
    vertex_colors: tuple[int, ...], edge_colors: Mapping[tuple[int, int], int]
) -> PairedHoleState:
    graph = SimpleGraph.from_edges(len(vertex_colors), (*edge_colors, (0, 1)))
    return PairedHoleState.create(
        graph=graph,
        degree_parameter=5,
        palette_size=7,
        vertex_colors=vertex_colors,
        edge_colors=(edge_colors.get(edge) for edge in graph.edges),
        uncolored_edge=(0, 1),
        alpha=0,
        roles=PairedHoleRoles(
            x=0,
            y=1,
            x_fan_satellites=(2, 3),
            y_fan_satellites=(4, 5),
        ),
    )


def sharp_state() -> PairedHoleState:
    return state_from_coloring(SHARP_VERTEX_COLORS, SHARP_EDGE_COLORS)


def hard_residual_state() -> PairedHoleState:
    vertex_colors = (1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5, 6)
    color_classes = {
        0: ((0, 8), (1, 10), (2, 5), (3, 4), (6, 7), (9, 11)),
        1: ((1, 9), (4, 7), (5, 8)),
        2: ((0, 11), (2, 6), (3, 10)),
        3: ((1, 4), (3, 6), (10, 11)),
        4: ((1, 5), (2, 7), (8, 11)),
        5: ((0, 2), (5, 7), (8, 9)),
        6: ((0, 3), (4, 6), (9, 10)),
    }
    edge_colors = {
        tuple(sorted(edge)): color for color, edges in color_classes.items() for edge in edges
    }
    return state_from_coloring(vertex_colors, cast(dict[tuple[int, int], int], edge_colors))


def nonaligned_terminal_release_state() -> PairedHoleState:
    vertex_colors = (1, 2, 2, 3, 1, 5, 3, 4, 4, 5, 6, 6)
    color_classes = {
        0: ((0, 6), (1, 7), (2, 4), (3, 5), (8, 10), (9, 11)),
        1: ((1, 9), (5, 6)),
        2: ((0, 10), (3, 7)),
        3: ((1, 4), (2, 8)),
        4: ((1, 5), (2, 9), (3, 11), (6, 10)),
        5: ((0, 2), (4, 8), (7, 11)),
        6: ((0, 3), (4, 6), (5, 8), (7, 9)),
    }
    edge_colors = {
        tuple(sorted(edge)): color for color, edges in color_classes.items() for edge in edges
    }
    return state_from_coloring(vertex_colors, cast(dict[tuple[int, int], int], edge_colors))


def test_state_round_trip_fingerprint_and_schema() -> None:
    state = sharp_state()
    assert PairedHoleState.from_json(state.to_json()) == state
    assert PairedHoleState.from_dict(state.to_dict()).fingerprint == state.fingerprint
    assert len(state.fingerprint) == 64

    schema = read_schema_json("paired-hole-state-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(state.to_dict())
    malformed = state.to_dict()
    malformed["extra"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(malformed)


def test_constructor_defensively_freezes_nested_color_arrays() -> None:
    baseline = sharp_state()
    vertex_colors = list(baseline.vertex_colors)
    edge_colors = list(baseline.edge_colors)
    state = PairedHoleState.create(
        graph=baseline.graph,
        degree_parameter=5,
        palette_size=7,
        vertex_colors=vertex_colors,
        edge_colors=edge_colors,
        uncolored_edge=(1, 0),
        alpha=0,
        roles=baseline.roles,
    )
    fingerprint = state.fingerprint
    vertex_colors[0] = 6
    edge_colors[0] = 6
    assert state == baseline
    assert state.fingerprint == fingerprint


def test_sharp_state_derives_all_facts_and_four_independent_exit_certificates() -> None:
    state = sharp_state()
    result = verify_paired_hole_state(state)
    assert result.status is PairedHoleStatus.VERIFIED_ONE_SWAP_EXIT
    assert result.issues == ()
    assert result.profile_alignment is ProfileAlignment.ALIGNED
    assert result.missing_colors[:6] == (
        (3, 4),
        (5, 6),
        (1, 6),
        (1, 5),
        (2, 4),
        (2, 3),
    )
    assert tuple((fan.center, fan.root, fan.satellites, fan.surplus) for fan in result.fans) == (
        (0, 1, (2, 3), 0),
        (1, 0, (4, 5), 0),
    )

    assert len(result.alpha_beta_blockages) == 12
    assert all(item.blocked_by_fixed_beta for item in result.alpha_beta_blockages)
    assert all(item.terminal is not None for item in result.alpha_beta_blockages)
    assert {
        (item.beta, item.hole): tuple(state.graph.edges[index] for index in item.path_edge_indices)
        for item in result.alpha_beta_blockages
    }[(1, 2)] == ((2, 9), (7, 9), (0, 7))

    assert len(result.cross_components) == 4
    assert all(item.relation is CrossComponentRelation.DISTINCT for item in result.cross_components)
    by_pair = {(item.a, item.b): item for item in result.cross_components}
    assert all(not attempt.verified for attempt in by_pair[(3, 5)].attempts)
    assert all(not attempt.verified for attempt in by_pair[(4, 6)].attempts)
    assert all(attempt.verified for attempt in by_pair[(3, 6)].attempts)
    assert all(attempt.verified for attempt in by_pair[(4, 5)].attempts)
    assert len(result.verified_exits) == 4
    assert result.cross_terminal_release_exits == ()
    for exit_attempt in result.verified_exits:
        certificate = exit_attempt.completion_certificate
        assert certificate is not None
        assert certificate.graph_fingerprint == state.graph.fingerprint
        assert certificate.verify(state.graph).valid


def test_terminal_bounds_distinguish_one_edge_from_longer_paths() -> None:
    assert terminal_degree_lower_bound(5, 1, 0, 0) == 6
    assert terminal_degree_lower_bound(5, 2, 0, 0) == 7
    assert terminal_degree_lower_bound(5, 7, 1, 2) == 10
    with pytest.raises(PairedHoleFormatError, match="positive"):
        terminal_degree_lower_bound(5, 0, 0, 0)
    with pytest.raises(PairedHoleFormatError, match="integer"):
        terminal_degree_lower_bound(5, True, 0, 0)
    assert not terminal_release_is_applicable(1, (3,))
    assert not terminal_release_is_applicable(2, (3,))
    assert not terminal_release_is_applicable(3, ())
    assert terminal_release_is_applicable(3, (3,))


def test_hard_residual_has_independently_verified_two_swap_orbit_exit() -> None:
    state = hard_residual_state()
    result = verify_paired_hole_state(state)
    assert result.status is PairedHoleStatus.VERIFIED_TWO_SWAP_ORBIT_EXIT
    assert result.issues == ()
    assert result.profile_alignment is ProfileAlignment.ALIGNED
    assert len(result.alpha_beta_blockages) == 12
    assert all(item.blocked_by_fixed_beta for item in result.alpha_beta_blockages)
    terminal_locks = tuple(item.terminal for item in result.alpha_beta_blockages)
    assert all(item is not None for item in terminal_locks)
    assert all(item is not None and item.common_release_colors == () for item in terminal_locks)
    assert all(item is not None and item.bound_satisfied for item in terminal_locks)
    assert len(result.cross_components) == 4
    assert result.verified_exits == ()
    assert result.cross_terminal_release_exits == ()
    assert all(
        analysis.terminal is not None and analysis.terminal.common_release_colors == ()
        for pair in result.cross_components
        for analysis in pair.root_terminal_analyses
    )
    orbit = result.two_swap_orbit_exit
    assert orbit is not None
    assert tuple(move.colors for move in orbit.moves) == ((0, 3), (3, 6))
    assert orbit.moves[0].component.edges == (
        (1, 4),
        (1, 10),
        (3, 4),
        (3, 6),
        (6, 7),
        (9, 11),
        (10, 11),
    )
    assert orbit.moves[0].component.walk == (7, 6, 3, 4, 1, 10, 11, 9)
    assert orbit.moves[1].component.edges == ((0, 3), (3, 4), (4, 6), (6, 7))
    assert orbit.moves[1].component.walk == (0, 3, 4, 6, 7)
    assert orbit.topology.first_role_color == 3
    assert orbit.topology.cross_colors == (3, 6)
    assert orbit.topology.cross_root == 0
    assert orbit.topology.relation_before_first_move is CrossComponentRelation.LINKED
    assert orbit.topology.relation_after_first_move is CrossComponentRelation.DISTINCT
    assert orbit.topology.cross_component_before.edges == (
        (0, 3),
        (1, 4),
        (3, 6),
        (4, 6),
    )
    assert orbit.topology.cross_component_before.walk == (0, 3, 6, 4, 1)
    assert orbit.topology.first_role_color_edges == ((1, 4), (3, 6), (10, 11))
    assert orbit.topology.intersection_edges == ((1, 4), (3, 6))
    assert orbit.fill_color == 6
    assert orbit.completion_certificate.verify(state.graph).valid


def test_nonaligned_profile_exposes_terminal_and_cross_release_certificates() -> None:
    state = nonaligned_terminal_release_state()
    result = verify_paired_hole_state(state)
    assert result.status is PairedHoleStatus.VERIFIED_CROSS_TERMINAL_RELEASE_EXIT
    assert result.profile_alignment is ProfileAlignment.NONALIGNED
    assert tuple(
        (item.beta, item.hole, item.release_color, item.terminal_edge)
        for item in result.terminal_release_witnesses
    ) == (
        (4, 0, 1, (8, 10)),
        (5, 1, 2, (9, 11)),
        (5, 1, 3, (9, 11)),
        (6, 1, 2, (9, 11)),
        (6, 1, 3, (9, 11)),
        (6, 2, 1, (8, 10)),
    )
    assert all(
        len(item.resulting_partial_fingerprint) == 64 for item in result.terminal_release_witnesses
    )
    assert all(
        item.terminal is None
        or not item.terminal.terminal_release_applicable
        or item.terminal.path_length >= 3
        for item in result.alpha_beta_blockages
    )
    by_pair = {(item.a, item.b): item.relation for item in result.cross_components}
    assert by_pair == {
        (3, 5): CrossComponentRelation.LINKED,
        (3, 6): CrossComponentRelation.DISTINCT,
        (4, 5): CrossComponentRelation.DISTINCT,
        (4, 6): CrossComponentRelation.DISTINCT,
    }
    assert result.two_swap_orbit_exit is None
    release = next(
        item
        for item in result.cross_terminal_release_exits
        if (item.a, item.b, item.side, item.path_vertices) == (3, 6, "y", (1, 4, 6))
    )
    assert release.release_color == 2
    assert release.fill_color == 3
    assert release.completion_certificate.verify(state.graph).valid


def test_candidate_status_branch_does_not_claim_a_negative_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = sharp_state()
    monkeypatch.setattr(
        paired_hole,
        "_derive_cross_components",
        lambda _state, _missing: ((), (), (), ()),
    )
    monkeypatch.setattr(
        paired_hole,
        "_derive_two_swap_orbit_exit",
        lambda _state, _missing: None,
    )
    result = verify_paired_hole_state(state)
    assert result.status is PairedHoleStatus.FULLY_BLOCKED_CANDIDATE
    assert not result.has_verified_exit


def test_alpha_terminal_release_remains_local_not_a_completion_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = nonaligned_terminal_release_state()
    derive_cross = paired_hole._derive_cross_components

    def without_cross_releases(
        candidate: PairedHoleState, missing: tuple[frozenset[int], ...]
    ) -> tuple[Any, ...]:
        analyses, direct_exits, _terminal_exits, issues = derive_cross(candidate, missing)
        return analyses, direct_exits, (), issues

    monkeypatch.setattr(paired_hole, "_derive_cross_components", without_cross_releases)
    monkeypatch.setattr(
        paired_hole,
        "_derive_two_swap_orbit_exit",
        lambda *_args: pytest.fail("bounded orbit must not run before alpha releases vanish"),
    )
    result = verify_paired_hole_state(state)
    assert result.status is PairedHoleStatus.VERIFIED_ALPHA_TERMINAL_RELEASE
    assert result.terminal_release_witnesses
    assert not result.has_verified_exit
    assert result.two_swap_orbit_exit is None


def _mutated_document() -> dict[str, Any]:
    return cast(dict[str, Any], copy.deepcopy(sharp_state().to_dict()))


@pytest.mark.parametrize(
    ("mutate", "status", "code"),
    [
        (
            lambda value: value["edge_colors"].__setitem__(1, None),
            PairedHoleStatus.INVALID_STATE,
            "uncolored_edge_set",
        ),
        (
            lambda value: value["edge_colors"].__setitem__(2, value["edge_colors"][1]),
            PairedHoleStatus.INVALID_STATE,
            "adjacent_edges_same_color",
        ),
        (
            lambda value: value["edge_colors"].__setitem__(1, value["vertex_colors"][0]),
            PairedHoleStatus.INVALID_STATE,
            "incident_vertex_edge_same_color",
        ),
        (
            lambda value: value["vertex_colors"].__setitem__(0, value["vertex_colors"][1]),
            PairedHoleStatus.INVALID_STATE,
            "adjacent_vertices_same_color",
        ),
        (
            lambda value: value["vertex_colors"].pop(),
            PairedHoleStatus.INVALID_STATE,
            "vertex_assignment_count",
        ),
        (
            lambda value: value["edge_colors"].pop(),
            PairedHoleStatus.INVALID_STATE,
            "edge_assignment_count",
        ),
        (
            lambda value: value.update(uncolored_edge=[0, 6]),
            PairedHoleStatus.INVALID_STATE,
            "uncolored_edge_absent",
        ),
        (
            lambda value: value["roles"].update(y=6),
            PairedHoleStatus.INVALID_STATE,
            "endpoint_role_mismatch",
        ),
        (
            lambda value: value["roles"].update(y=99),
            PairedHoleStatus.INVALID_STATE,
            "role_vertex_out_of_range",
        ),
        (
            lambda value: value["roles"].update(x_fan_satellites=[2, 6]),
            PairedHoleStatus.INVALID_STATE,
            "fan_role_pointer_mismatch",
        ),
        (
            lambda value: value.update(palette_size=8),
            PairedHoleStatus.UNSUPPORTED_OUT_OF_SCOPE,
            "palette_not_r_plus_two",
        ),
        (
            lambda value: value.update(alpha=1),
            PairedHoleStatus.UNSUPPORTED_OUT_OF_SCOPE,
            "alpha_used_on_vertex",
        ),
    ],
)
def test_fail_closed_semantic_mutations(mutate: Any, status: PairedHoleStatus, code: str) -> None:
    value = _mutated_document()
    mutate(value)
    result = verify_paired_hole_json(json.dumps(value))
    assert result.status is status
    assert code in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    "data",
    [
        "[]",
        "{}",
        '{"schema_version":1,"schema_version":2}',
        '{"schema_version":NaN}',
        b"\xff",
    ],
)
def test_hostile_json_is_invalid_without_escaping_the_api(data: str | bytes) -> None:
    result = verify_paired_hole_json(data)
    assert result.status is PairedHoleStatus.INVALID_STATE
    assert tuple(issue.code for issue in result.issues) == ("format_error",)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("palette_size", True, "integer"),
        ("alpha", 99, "outside palette"),
        ("uncolored_edge", [1, 0], "canonical"),
        ("roles.x_fan_satellites", [2, 2], "strictly increasing"),
    ],
)
def test_strict_parser_rejects_noncanonical_fields(path: str, value: object, message: str) -> None:
    document = _mutated_document()
    if path.startswith("roles."):
        cast(dict[str, Any], document["roles"])[path.removeprefix("roles.")] = value
    else:
        document[path] = value
    with pytest.raises(PairedHoleFormatError, match=message):
        PairedHoleState.from_dict(document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(extra=True), "unknown keys"),
        (lambda value: value.pop("alpha"), "missing keys"),
        (lambda value: value.update(schema_version="paired-hole.v0"), "schema_version"),
        (lambda value: value.update(kind="trusted-proof"), "kind"),
        (lambda value: value["roles"].update(extra=True), "unknown keys"),
        (
            lambda value: value["graph"]["edges"].reverse(),
            "graph JSON is not canonical",
        ),
    ],
)
def test_strict_parser_rejects_unknown_versioned_or_noncanonical_data(
    mutate: Any, message: str
) -> None:
    document = _mutated_document()
    mutate(document)
    with pytest.raises(PairedHoleFormatError, match=message):
        PairedHoleState.from_dict(document)


def test_valid_partial_state_outside_exact_envelope_is_unsupported() -> None:
    baseline = sharp_state()
    graph = SimpleGraph.from_edges(13, baseline.graph.edges)
    state = PairedHoleState.create(
        graph=graph,
        degree_parameter=5,
        palette_size=7,
        vertex_colors=(*baseline.vertex_colors, 1),
        edge_colors=baseline.edge_colors,
        uncolored_edge=baseline.uncolored_edge,
        alpha=0,
        roles=baseline.roles,
    )
    result = verify_paired_hole_state(state)
    assert result.status is PairedHoleStatus.UNSUPPORTED_OUT_OF_SCOPE
    assert "vertex_color_class_too_large" in {issue.code for issue in result.issues}


def test_topology_only_all_cross_blocked_connector_is_not_admitted() -> None:
    # This connector realizes all four desired cross-color path shapes, but it
    # deliberately lacks the alpha, degree, and zero-surplus fan envelope.  The
    # semantic verifier must not promote topology alone into an exact residue.
    vertex_colors = (1, 2, 3, 4, 5, 3, 4, 5, 6, 7, 8, 6, 7, 8)
    edge_colors = {
        (0, 2): 6,
        (2, 3): 5,
        (3, 4): 6,
        (0, 5): 8,
        (5, 6): 5,
        (6, 7): 8,
        (1, 8): 3,
        (8, 9): 8,
        (9, 10): 3,
        (1, 11): 5,
        (11, 12): 8,
        (12, 13): 5,
    }
    graph = SimpleGraph.from_edges(14, (*edge_colors, (0, 1)))
    state = PairedHoleState.create(
        graph=graph,
        degree_parameter=7,
        palette_size=9,
        vertex_colors=vertex_colors,
        edge_colors=(edge_colors.get(edge) for edge in graph.edges),
        uncolored_edge=(0, 1),
        alpha=0,
        roles=PairedHoleRoles(
            x=0,
            y=1,
            x_fan_satellites=(2, 5),
            y_fan_satellites=(8, 11),
        ),
    )
    result = verify_paired_hole_state(state)
    assert result.status is PairedHoleStatus.UNSUPPORTED_OUT_OF_SCOPE
    codes = {issue.code for issue in result.issues}
    assert "maximum_degree_mismatch" in codes
    assert "fan_surplus_not_zero" in codes
