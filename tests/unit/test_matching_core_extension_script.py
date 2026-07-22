from __future__ import annotations

import argparse
import importlib.util
import json
from itertools import pairwise
from pathlib import Path
from types import ModuleType

import pytest

from total_coloring.geng import GengIdentity
from total_coloring.graph import SimpleGraph
from total_coloring.solver import SearchStats, SolveResult, SolveStatus


def load_script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "research" / "matching_core_extension.py"
    spec = importlib.util.spec_from_file_location("matching_core_extension", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bounded_pair_partitions_are_canonical_and_bounded() -> None:
    module = load_script()
    partitions = list(module.bounded_pair_partitions(4, 3))
    assert partitions
    assert len(partitions) == len(set(partitions))
    for partition in partitions:
        assert partition[0] == 0
        assert max(partition) < 3
        assert all(partition.count(colour) <= 2 for colour in set(partition))
        for index in range(1, len(partition)):
            assert partition[index] <= max(partition[:index]) + 1

    assert len(list(module.bounded_pair_partitions(10, 5))) == 945


def test_matching_core_scope_excludes_independent_and_nonmatching_cores() -> None:
    module = load_script()
    path = SimpleGraph.from_edges(4, ((0, 1), (1, 2), (2, 3)))
    cycle = SimpleGraph.from_edges(4, ((0, 1), (1, 2), (2, 3), (0, 3)))
    star = SimpleGraph.from_edges(4, ((0, 1), (0, 2), (0, 3)))

    assert module.in_scope(path, "matching-nonempty")
    assert not module.in_scope(cycle, "matching-nonempty")
    assert not module.in_scope(star, "matching-nonempty")
    assert module.in_scope(path, "forest-nonempty")
    assert not module.in_scope(cycle, "forest-nonempty")


def test_two_sided_matching_envelope_starts_before_saturated_order_ten_template() -> None:
    module = load_script()
    minimum_envelope = SimpleGraph.from_edges(
        5,
        (
            (0, 1),
            (0, 2),
            (0, 3),
            (0, 4),
            (1, 2),
            (1, 3),
            (1, 4),
            (2, 3),
        ),
    )
    assert minimum_envelope.degrees == (4, 4, 3, 3, 2)
    assert module.in_scope(minimum_envelope, "two-sided-matching-envelope")

    one_sided = SimpleGraph.from_edges(
        7,
        (
            (0, 1),
            (0, 2),
            (0, 3),
            (0, 4),
            (1, 2),
            (1, 5),
            (1, 6),
            (2, 3),
            (3, 4),
            (4, 5),
        ),
    )
    assert one_sided.degrees == (4, 4, 3, 3, 3, 2, 1)
    assert not module.in_scope(one_sided, "two-sided-matching-envelope")

    nonmatching_core = SimpleGraph.from_edges(
        4,
        ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)),
    )
    assert not module.in_scope(nonmatching_core, "two-sided-matching-envelope")

    graph = SimpleGraph.from_edges(
        10,
        (
            (0, 1),
            (0, 2),
            (0, 3),
            (0, 4),
            (1, 5),
            (1, 6),
            (1, 7),
            (2, 5),
            (2, 8),
            (3, 6),
            (3, 9),
            (5, 7),
            (6, 8),
        ),
    )

    assert graph.degrees == (4, 4, 3, 3, 1, 3, 3, 2, 2, 1)
    assert module.in_scope(graph, "matching-nonempty")
    assert module.in_scope(graph, "two-sided-matching-envelope")

    vertex_colours = (3, 5, 4, 4, 5, 3, 1, 1, 2, 2)
    assert module.is_proper_vertex_coloring(graph, vertex_colours)
    problem = module.fixed_total_problem(graph, 6, vertex_colours)
    result = module.solve_dsatur(problem, limits=module.SearchLimits())
    assert result.status is module.SolveStatus.WITNESS
    assert result.assignment is not None
    assert module.independent_witness_issues(graph, 6, vertex_colours, result.assignment) == ()


def test_order_six_cyclic_fan_state_is_extendable() -> None:
    module = load_script()
    graph = SimpleGraph.from_edges(
        6,
        (
            (0, 2),
            (0, 3),
            (0, 4),
            (0, 5),
            (1, 3),
            (1, 4),
            (1, 5),
            (2, 4),
            (2, 5),
            (3, 5),
        ),
    )
    assert graph.to_graph6() == "EUzo"
    vertex_colours = (3, 1, 2, 4, 4, 5)
    assert set(vertex_colours) == {1, 2, 3, 4, 5}
    assert 0 not in vertex_colours
    partial_edge_colours = {
        (0, 2): 0,
        (0, 3): 1,
        (0, 4): 2,
        (1, 3): 5,
        (1, 4): 0,
        (1, 5): 3,
        (2, 4): 5,
        (2, 5): 4,
        (3, 5): 0,
    }

    def missing(vertex: int) -> set[int]:
        used = {vertex_colours[vertex]}
        used.update(colour for edge, colour in partial_edge_colours.items() if vertex in edge)
        return set(range(6)) - used

    assert missing(5) == {1, 2}
    assert missing(3) == {2, 3}
    assert missing(4) == {1, 3}

    completed_edge_colours = {
        **partial_edge_colours,
        (0, 5): 4,
        (2, 5): 1,
    }
    assignment = vertex_colours + tuple(completed_edge_colours[edge] for edge in graph.edges)
    assert module.independent_witness_issues(graph, 6, vertex_colours, assignment) == ()


def test_alpha_blocked_two_sided_order_twelve_state_has_cross_colour_repair() -> None:
    module = load_script()
    # Labels: x,y,u,v,w,z,P,Q,A,B,C,D.
    vertex_colours = (1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5, 6)
    initial_edge_colours = {
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
    graph = SimpleGraph.from_edges(12, (*initial_edge_colours, (0, 1)))
    assert graph.degrees == (5, 5, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4)
    assert 0 not in vertex_colours

    def missing(vertex: int) -> set[int]:
        used = {vertex_colours[vertex]}
        used.update(colour for edge, colour in initial_edge_colours.items() if vertex in edge)
        return set(range(7)) - used

    assert missing(0) == {3, 4}
    assert missing(1) == {5, 6}
    assert missing(2) == {1, 6}
    assert missing(3) == {1, 5}
    assert missing(4) == {2, 4}
    assert missing(5) == {2, 3}

    blocked_paths = (
        (1, (2, 9, 7, 0)),
        (1, (3, 8, 1, 6)),
        (2, (4, 11, 6, 1)),
        (2, (5, 10, 0, 7)),
        (3, (0, 7, 3, 8)),
        (3, (5, 10, 9, 2)),
        (4, (0, 7, 8, 3)),
        (4, (4, 11, 2, 9)),
        (5, (1, 6, 5, 10)),
        (5, (3, 8, 11, 4)),
        (6, (1, 6, 10, 5)),
        (6, (2, 9, 4, 11)),
    )
    for beta, path in blocked_paths:
        colors = tuple(
            initial_edge_colours[(min(left, right), max(left, right))]
            for left, right in pairwise(path)
        )
        assert colors == (0, beta, 0)
        assert beta in missing(path[0])
        assert vertex_colours[path[-1]] == beta

    completed_edge_colours = dict(initial_edge_colours)

    def swap_path(path: tuple[int, ...], first: int, second: int) -> None:
        for left, right in pairwise(path):
            edge = (min(left, right), max(left, right))
            colour = completed_edge_colours[edge]
            assert colour in {first, second}
            completed_edge_colours[edge] = second if colour == first else first

    # Although all twelve displayed alpha-beta paths are blocked, a cross-colour
    # Kempe swap frees colour 6 at x, whereupon 6 is missing at both x and y.
    swap_path((0, 3, 7), 6, 3)
    completed_edge_colours[(0, 1)] = 6
    assignment = vertex_colours + tuple(completed_edge_colours[edge] for edge in graph.edges)
    assert module.independent_witness_issues(graph, 7, vertex_colours, assignment) == ()


def test_fixed_problem_requires_the_prescribed_vertex_colours() -> None:
    module = load_script()
    graph = SimpleGraph.from_edges(3, ((0, 1), (1, 2)))
    problem = module.fixed_total_problem(graph, 4, (0, 1, 0))
    assert problem.fixed_colors == ((0, 0), (1, 1), (2, 0))
    assert problem.verify_assignment((0, 1, 0, 2, 3)) == ()

    valid_assignment = (0, 1, 0, 2, 3)
    assert module.independent_witness_issues(graph, 4, (0, 1, 0), valid_assignment) == ()
    assert module.independent_witness_issues(graph, 4, (1, 0, 1), valid_assignment) == (
        "fixed_vertex_mismatch:vertices[0]",
        "fixed_vertex_mismatch:vertices[1]",
        "fixed_vertex_mismatch:vertices[2]",
    )
    assert "adjacent_edges_same_color:edge_colors[1]" in module.independent_witness_issues(
        graph,
        4,
        (0, 1, 0),
        (0, 1, 0, 2, 2),
    )


def test_non_witness_statuses_map_to_receipt_counters() -> None:
    module = load_script()

    assert module.non_witness_count_key(module.SolveStatus.CANDIDATE_UNSAT) == "candidate_unsat"
    assert module.non_witness_count_key(module.SolveStatus.UNKNOWN) == "unknown"
    assert module.non_witness_count_key(module.SolveStatus.ERROR) == "errors"


def _script_arguments(output: Path, *, max_graphs: int | None) -> argparse.Namespace:
    return argparse.Namespace(
        connected=False,
        core_kind="matching-nonempty",
        geng="geng",
        max_degree=None,
        max_graphs=max_graphs,
        max_nodes_per_search=None,
        min_degree=None,
        order=4,
        output=output,
        required_maximum_degree=None,
        shard_count=1,
        shard_index=0,
        split_depth=0,
        timeout_per_search=None,
    )


def test_capped_main_marks_receipt_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    graph = SimpleGraph.from_edges(4, ((0, 1), (1, 2), (2, 3)))
    output = tmp_path / "bounded.json"
    monkeypatch.setattr(module, "parse_arguments", lambda: _script_arguments(output, max_graphs=1))
    monkeypatch.setattr(
        module,
        "geng_identity",
        lambda _spec, executable: GengIdentity(executable, "0" * 64, ("4",)),
    )
    monkeypatch.setattr(module, "stream_geng", lambda _spec, executable: iter((graph, graph)))

    assert module.main() == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(capsys.readouterr().out) == receipt
    assert receipt["status"] == "bounded_positive"
    assert receipt["input_exhausted"] is False
    assert receipt["stop_reason"] == "max_graphs"


def test_solver_error_writes_fail_closed_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    graph = SimpleGraph.from_edges(4, ((0, 1), (1, 2), (2, 3)))
    output = tmp_path / "error.json"
    monkeypatch.setattr(
        module, "parse_arguments", lambda: _script_arguments(output, max_graphs=None)
    )
    monkeypatch.setattr(
        module,
        "geng_identity",
        lambda _spec, executable: GengIdentity(executable, "0" * 64, ("4",)),
    )
    monkeypatch.setattr(module, "stream_geng", lambda _spec, executable: iter((graph,)))
    monkeypatch.setattr(
        module,
        "solve_dsatur",
        lambda _problem, limits: SolveResult(
            status=SolveStatus.ERROR,
            problem_digest="0" * 64,
            assignment=None,
            stats=SearchStats(nodes=1, backtracks=0, elapsed_seconds=0.0),
            detail="synthetic solver error",
        ),
    )

    assert module.main() == 2
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(capsys.readouterr().out) == receipt
    assert receipt["status"] == "error"
    assert receipt["input_exhausted"] is False
    assert receipt["stop_reason"] == "first_non_witness"
    assert receipt["counts"]["errors"] == 1


def test_witness_without_assignment_writes_fail_closed_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    graph = SimpleGraph.from_edges(4, ((0, 1), (1, 2), (2, 3)))
    output = tmp_path / "missing-assignment.json"
    monkeypatch.setattr(
        module, "parse_arguments", lambda: _script_arguments(output, max_graphs=None)
    )
    monkeypatch.setattr(
        module,
        "geng_identity",
        lambda _spec, executable: GengIdentity(executable, "0" * 64, ("4",)),
    )
    monkeypatch.setattr(module, "stream_geng", lambda _spec, executable: iter((graph,)))
    monkeypatch.setattr(
        module,
        "solve_dsatur",
        lambda _problem, limits: SolveResult(
            status=SolveStatus.WITNESS,
            problem_digest="0" * 64,
            assignment=None,
            stats=SearchStats(nodes=1, backtracks=0, elapsed_seconds=0.0),
            detail="synthetic malformed witness",
        ),
    )

    assert module.main() == 2
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(capsys.readouterr().out) == receipt
    assert receipt["status"] == "invalid_solver_witness"
    assert receipt["input_exhausted"] is False
    assert receipt["stop_reason"] == "first_non_witness"
    assert receipt["counts"]["errors"] == 1
