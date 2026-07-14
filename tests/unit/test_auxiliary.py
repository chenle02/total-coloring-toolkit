from __future__ import annotations

import pytest

from total_coloring.auxiliary import (
    AuxiliaryPartitionResult,
    EquitablePartition,
    check_all_auxiliary_partitions,
    construct_auxiliary_graph,
    decode_auxiliary_coloring,
    equitable_class_profile,
    iter_complement_matchings,
    iter_equitable_partitions,
    search_auxiliary_extensions,
    solve_auxiliary_partition,
)
from total_coloring.certificates import verify_total_coloring
from total_coloring.graph import SimpleGraph
from total_coloring.solver import SearchLimits, SearchStats, SolveResult, SolveStatus


def cycle(order: int) -> SimpleGraph:
    return SimpleGraph.from_edges(
        order, ((vertex, (vertex + 1) % order) for vertex in range(order))
    )


def test_complement_matching_enumerator_is_complete_and_deterministic() -> None:
    empty = SimpleGraph.from_edges(4, [])

    matchings = tuple(iter_complement_matchings(empty, 2))

    assert matchings == (
        ((0, 1), (2, 3)),
        ((0, 2), (1, 3)),
        ((0, 3), (1, 2)),
    )
    assert tuple(iter_complement_matchings(empty, 0)) == ((),)
    assert tuple(iter_complement_matchings(empty, 3)) == ()


def test_c4_has_exactly_two_equitable_three_class_partitions() -> None:
    graph = cycle(4)

    assert equitable_class_profile(graph) == (1, 2)
    partitions = tuple(iter_equitable_partitions(graph))

    assert partitions == (
        EquitablePartition(pairs=((0, 2),), singletons=(1, 3)),
        EquitablePartition(pairs=((1, 3),), singletons=(0, 2)),
    )


def test_auxiliary_construction_has_exact_invariants() -> None:
    graph = cycle(4)
    partition = next(iter_equitable_partitions(graph))

    construction = construct_auxiliary_graph(graph, partition)

    assert construction.added_vertex == 4
    assert construction.degree_parameter == 3
    assert construction.graph.order == 5
    assert construction.graph.max_degree == 3
    assert len(construction.distinguished_edges) == 3
    assert len(set(construction.class_edge_by_vertex)) == 3
    assert all(construction.graph.has_edge(*edge) for edge in construction.distinguished_edges)


def test_auxiliary_search_decodes_a_verified_total_coloring() -> None:
    graph = cycle(4)

    result = search_auxiliary_extensions(graph, graph.max_degree + 3)

    assert result.status is SolveStatus.WITNESS
    assert result.witness is not None
    assert result.partitions_started >= 1
    assert result.partitions_completed == 1
    assert verify_total_coloring(graph, result.witness.total_coloring).valid


def test_fixed_partition_api_agrees_with_unfixed_rainbow_formulation() -> None:
    graph = cycle(4)
    partition = next(iter_equitable_partitions(graph))

    fixed = solve_auxiliary_partition(
        graph, partition, graph.max_degree + 2, fix_distinguished_colors=True
    )
    unfixed = solve_auxiliary_partition(
        graph, partition, graph.max_degree + 2, fix_distinguished_colors=False
    )

    assert fixed.status is SolveStatus.WITNESS
    assert unfixed.status is SolveStatus.WITNESS
    assert fixed.total_coloring is not None
    assert unfixed.total_coloring is not None
    assert verify_total_coloring(graph, fixed.total_coloring).valid
    assert verify_total_coloring(graph, unfixed.total_coloring).valid


def test_fixed_partition_api_keeps_negative_search_as_candidate_only() -> None:
    graph = cycle(4)
    partition = next(iter_equitable_partitions(graph))

    result = solve_auxiliary_partition(graph, partition, graph.max_degree + 1)

    assert result.status is SolveStatus.CANDIDATE_UNSAT
    assert result.total_coloring is None


def test_universal_check_verifies_every_c4_partition_with_delta_plus_two() -> None:
    graph = cycle(4)

    result = check_all_auxiliary_partitions(graph, graph.max_degree + 2)

    assert result.status is SolveStatus.WITNESS
    assert result.partitions_started == 2
    assert result.verified_partitions == 2
    assert result.first_nonwitness is None


def test_universal_check_preserves_candidate_and_unknown_distinctions() -> None:
    graph = cycle(4)

    candidate = check_all_auxiliary_partitions(graph, graph.max_degree + 1)
    unknown = check_all_auxiliary_partitions(
        graph,
        graph.max_degree + 2,
        limits_per_partition=SearchLimits(max_nodes=1),
    )
    truncated = check_all_auxiliary_partitions(
        graph,
        graph.max_degree + 2,
        max_partitions=1,
    )

    assert candidate.status is SolveStatus.CANDIDATE_UNSAT
    assert candidate.first_nonwitness is not None
    assert unknown.status is SolveStatus.UNKNOWN
    assert unknown.unknown_partitions == 2
    assert unknown.first_nonwitness is not None
    assert truncated.status is SolveStatus.UNKNOWN
    assert truncated.verified_partitions == 1
    assert "partition limit" in truncated.detail


def test_complete_triangle_boundary_uses_no_pair_classes() -> None:
    graph = SimpleGraph.from_edges(3, [(0, 1), (0, 2), (1, 2)])
    partition = next(iter_equitable_partitions(graph))
    construction = construct_auxiliary_graph(graph, partition)

    assert partition.pairs == ()
    assert partition.singletons == (0, 1, 2)
    assert construction.graph.order == 4
    result = search_auxiliary_extensions(graph, 3)
    assert result.status is SolveStatus.WITNESS
    assert result.witness is not None
    assert result.witness.total_coloring.palette_size == 3


def test_search_reports_unknown_when_solver_limits_interrupt_every_branch() -> None:
    graph = cycle(4)

    result = search_auxiliary_extensions(
        graph,
        graph.max_degree + 3,
        limits_per_partition=SearchLimits(max_nodes=1),
    )

    assert result.status is SolveStatus.UNKNOWN
    assert result.witness is None
    assert result.unknown_partitions == 2
    assert result.partitions_completed == 0


def test_search_reports_exhausted_candidate_negative_without_overclaiming() -> None:
    graph = cycle(4)

    result = search_auxiliary_extensions(graph, graph.max_degree + 1)

    assert result.status is SolveStatus.CANDIDATE_UNSAT
    assert result.witness is None
    assert result.partitions_started == 2
    assert result.partitions_completed == 2
    assert result.candidate_failures == 2
    assert "no independent UNSAT proofs" in result.detail


def test_partition_limit_and_solver_limit_compose_as_unknown() -> None:
    graph = cycle(4)

    result = search_auxiliary_extensions(
        graph,
        graph.max_degree + 3,
        limits_per_partition=SearchLimits(max_nodes=1),
        max_partitions=1,
    )

    assert result.status is SolveStatus.UNKNOWN
    assert result.partitions_started == 1
    assert result.unknown_partitions == 1
    assert "partition limit reached" in result.detail
    assert "solver branches were incomplete" in result.detail


def test_decode_rejects_an_invalid_auxiliary_assignment() -> None:
    graph = cycle(4)
    construction = construct_auxiliary_graph(graph, next(iter_equitable_partitions(graph)))

    with pytest.raises(ValueError, match="invalid edge coloring"):
        decode_auxiliary_coloring(
            construction,
            graph.max_degree + 3,
            (0,) * construction.graph.size,
        )


def test_partition_and_domain_validation_fail_closed() -> None:
    graph = cycle(4)
    with pytest.raises(ValueError, match="expected 1 pairs"):
        EquitablePartition.from_complement_matching(graph, ())
    with pytest.raises(ValueError, match="original graph"):
        EquitablePartition.from_complement_matching(graph, ((0, 1),))
    with pytest.raises(ValueError, match="sizes one and two"):
        equitable_class_profile(SimpleGraph.from_edges(6, []))
    with pytest.raises(ValueError, match="null graph"):
        equitable_class_profile(SimpleGraph.from_edges(0, []))
    with pytest.raises(ValueError, match="nonnegative"):
        tuple(iter_complement_matchings(graph, -1))
    with pytest.raises(ValueError, match="at least D"):
        search_auxiliary_extensions(graph, graph.max_degree)
    with pytest.raises(ValueError, match="positive integer"):
        search_auxiliary_extensions(graph, 5, max_partitions=0)
    with pytest.raises(ValueError, match="positive integer"):
        search_auxiliary_extensions(graph, 5, max_partitions=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        search_auxiliary_extensions(graph, 5.0)  # type: ignore[arg-type]


def test_construct_rejects_noncanonical_singleton_list() -> None:
    with pytest.raises(ValueError, match="unique and sorted"):
        EquitablePartition(pairs=((0, 2),), singletons=(3, 1))


def test_partition_defensively_freezes_nested_inputs() -> None:
    pairs = [[0, 2]]
    singletons = [1, 3]

    partition = EquitablePartition(
        pairs=pairs,  # type: ignore[arg-type]
        singletons=singletons,  # type: ignore[arg-type]
    )
    pairs[0][1] = 3
    singletons.append(4)

    assert partition.pairs == ((0, 2),)
    assert partition.singletons == (1, 3)


def test_empty_partition_stream_fails_as_error_not_candidate_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = cycle(4)
    monkeypatch.setattr(
        "total_coloring.auxiliary.iter_equitable_partitions", lambda _graph: iter(())
    )

    result = search_auxiliary_extensions(graph, 5)

    assert result.status is SolveStatus.ERROR
    assert "no equitable partitions" in result.detail


def test_search_fails_closed_on_an_incomplete_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = cycle(4)
    partition = next(iter_equitable_partitions(graph))
    construction = construct_auxiliary_graph(graph, partition)
    malformed = AuxiliaryPartitionResult(
        construction,
        SolveResult(
            SolveStatus.WITNESS,
            "synthetic",
            None,
            SearchStats(nodes=1, backtracks=0, elapsed_seconds=0.0),
            "synthetic malformed witness",
        ),
        None,
    )
    monkeypatch.setattr(
        "total_coloring.auxiliary.solve_auxiliary_partition",
        lambda *_args, **_kwargs: malformed,
    )

    result = search_auxiliary_extensions(graph, 5)

    assert result.status is SolveStatus.ERROR
    assert result.witness is None
    assert "incomplete witness" in result.detail
