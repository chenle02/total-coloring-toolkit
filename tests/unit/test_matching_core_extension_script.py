from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from total_coloring.graph import SimpleGraph


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


def test_tight_matching_residue_recognizes_first_order_ten_template() -> None:
    module = load_script()
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
    assert module.in_scope(graph, "tight-matching-residue")

    vertex_colours = (3, 5, 4, 4, 5, 3, 1, 1, 2, 2)
    assert module.is_proper_vertex_coloring(graph, vertex_colours)
    problem = module.fixed_total_problem(graph, 6, vertex_colours)
    result = module.solve_dsatur(problem, limits=module.SearchLimits())
    assert result.status is module.SolveStatus.WITNESS
    assert result.assignment is not None
    assert problem.verify_assignment(result.assignment) == ()


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
