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


def test_fixed_problem_requires_the_prescribed_vertex_colours() -> None:
    module = load_script()
    graph = SimpleGraph.from_edges(3, ((0, 1), (1, 2)))
    problem = module.fixed_total_problem(graph, 4, (0, 1, 0))
    assert problem.fixed_colors == ((0, 0), (1, 1), (2, 0))
    assert problem.verify_assignment((0, 1, 0, 2, 3)) == ()
