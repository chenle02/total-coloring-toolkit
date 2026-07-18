#!/usr/bin/env python3
"""Falsify a fixed-precoloring extension beyond Chew's independent core.

The candidate statement tested here is deliberately narrow.  For a simple
graph ``G`` of maximum degree ``R`` whose degree-``R`` core is nonempty and
belongs to the selected structural class, every proper vertex precoloring
with colour classes of size at most two and at least one unused colour is
tested for extension to an ``(R + 2)``-total-colouring.

An exhausted DSATUR search is recorded as ``candidate_unsat``.  It is not a
mathematical counterexample until an independent negative proof checker is
available.  Every positive assignment is checked through the semantic model
before it is counted.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from total_coloring.geng import GengSpec, geng_identity, stream_geng
from total_coloring.graph import SimpleGraph
from total_coloring.model import ColoringProblem
from total_coloring.solver import SearchLimits, SolveStatus, solve_dsatur
from total_coloring.total import total_coloring_problem

SCHEMA_VERSION = "total-coloring.matching-core-extension-run.v1"


def bounded_pair_partitions(order: int, max_blocks: int) -> Iterator[tuple[int, ...]]:
    """Yield canonical restricted-growth strings with block size at most two."""

    if order == 0:
        yield ()
        return
    if max_blocks <= 0:
        return

    colours = [0] * order
    block_sizes = [1]

    def visit(index: int, current_maximum: int) -> Iterator[tuple[int, ...]]:
        if index == order:
            yield tuple(colours)
            return
        largest = min(current_maximum + 1, max_blocks - 1)
        for colour in range(largest + 1):
            is_new = colour > current_maximum
            if is_new:
                block_sizes.append(0)
            if block_sizes[colour] < 2:
                colours[index] = colour
                block_sizes[colour] += 1
                yield from visit(index + 1, max(current_maximum, colour))
                block_sizes[colour] -= 1
            if is_new:
                block_sizes.pop()

    yield from visit(1, 0)


def is_proper_vertex_coloring(graph: SimpleGraph, colours: tuple[int, ...]) -> bool:
    return all(colours[left] != colours[right] for left, right in graph.edges)


def core_edges(graph: SimpleGraph) -> tuple[tuple[int, int], ...]:
    maximum = graph.max_degree
    core = {vertex for vertex, degree in enumerate(graph.degrees) if degree == maximum}
    return tuple(edge for edge in graph.edges if edge[0] in core and edge[1] in core)


def is_forest(order: int, edges: tuple[tuple[int, int], ...]) -> bool:
    parent = list(range(order))

    def find(vertex: int) -> int:
        while parent[vertex] != vertex:
            parent[vertex] = parent[parent[vertex]]
            vertex = parent[vertex]
        return vertex

    for left, right in edges:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return False
        parent[left_root] = right_root
    return True


def in_scope(graph: SimpleGraph, core_kind: str) -> bool:
    edges = core_edges(graph)
    if not edges:
        return False
    if core_kind == "any-nonempty":
        return True
    if core_kind == "forest-nonempty":
        return is_forest(graph.order, edges)
    if core_kind == "matching-nonempty":
        degree = [0] * graph.order
        for left, right in edges:
            degree[left] += 1
            degree[right] += 1
        return max(degree, default=0) <= 1
    raise ValueError(f"unsupported core kind: {core_kind}")


def fixed_total_problem(
    graph: SimpleGraph, palette_size: int, vertex_colours: tuple[int, ...]
) -> ColoringProblem:
    base = total_coloring_problem(graph, palette_size)
    return ColoringProblem(
        item_names=base.item_names,
        color_count=base.color_count,
        conflicts=base.conflicts,
        all_different=base.all_different,
        fixed_colors=tuple(enumerate(vertex_colours)),
        metadata=base.metadata,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order", required=True, type=int)
    parser.add_argument("--shard-index", required=True, type=int)
    parser.add_argument("--shard-count", required=True, type=int)
    parser.add_argument("--split-depth", type=int, default=2)
    parser.add_argument(
        "--core-kind",
        choices=("matching-nonempty", "forest-nonempty", "any-nonempty"),
        default="matching-nonempty",
    )
    parser.add_argument("--geng", default="geng")
    parser.add_argument("--max-graphs", type=int)
    parser.add_argument("--max-nodes-per-search", type=int)
    parser.add_argument("--timeout-per-search", type=float)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    payload = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    arguments = parse_arguments()
    if arguments.order < 1:
        raise SystemExit("--order must be positive")
    if arguments.shard_count <= 0:
        raise SystemExit("--shard-count must be positive")
    if not 0 <= arguments.shard_index < arguments.shard_count:
        raise SystemExit("--shard-index must lie in [0, --shard-count)")
    if arguments.max_graphs is not None and arguments.max_graphs <= 0:
        raise SystemExit("--max-graphs must be positive")

    spec = GengSpec(
        order=arguments.order,
        shard_index=arguments.shard_index,
        shard_count=arguments.shard_count,
        split_depth=arguments.split_depth,
    )
    identity = geng_identity(spec, executable=arguments.geng)
    limits = SearchLimits(
        max_nodes=arguments.max_nodes_per_search,
        timeout_seconds=arguments.timeout_per_search,
    )
    started = time.monotonic()
    counts = {
        "generated_graphs": 0,
        "scope_graphs": 0,
        "proper_precolorings": 0,
        "verified_witnesses": 0,
        "candidate_unsat": 0,
        "unknown": 0,
        "errors": 0,
    }
    first_non_witness: dict[str, object] | None = None

    for graph in stream_geng(spec, executable=arguments.geng):
        counts["generated_graphs"] += 1
        if not in_scope(graph, arguments.core_kind):
            continue
        counts["scope_graphs"] += 1
        palette_size = graph.max_degree + 2
        # At most R + 1 used vertex colours leaves one palette colour unused.
        for vertex_colours in bounded_pair_partitions(graph.order, palette_size - 1):
            if not is_proper_vertex_coloring(graph, vertex_colours):
                continue
            counts["proper_precolorings"] += 1
            problem = fixed_total_problem(graph, palette_size, vertex_colours)
            result = solve_dsatur(problem, limits=limits)
            if result.status is SolveStatus.WITNESS:
                assert result.assignment is not None
                violations = problem.verify_assignment(result.assignment)
                if violations:
                    counts["errors"] += 1
                    first_non_witness = {
                        "classification": "invalid_solver_witness",
                        "detail": list(violations),
                        "graph6": graph.to_graph6(),
                        "graph_fingerprint": graph.fingerprint,
                        "palette_size": palette_size,
                        "problem_digest": problem.semantic_digest,
                        "vertex_colours": list(vertex_colours),
                    }
                    break
                counts["verified_witnesses"] += 1
                continue

            counts[result.status.value] += 1
            first_non_witness = {
                "classification": result.status.value,
                "detail": result.detail,
                "graph6": graph.to_graph6(),
                "graph_fingerprint": graph.fingerprint,
                "palette_size": palette_size,
                "problem_digest": problem.semantic_digest,
                "search_stats": asdict(result.stats),
                "vertex_colours": list(vertex_colours),
            }
            break
        if first_non_witness is not None:
            break
        if arguments.max_graphs is not None and counts["scope_graphs"] >= arguments.max_graphs:
            break

    status = (
        "complete_positive" if first_non_witness is None else first_non_witness["classification"]
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "scope_note": (
            "bounded finite falsification only; candidate_unsat is not a mathematical "
            "counterexample without an independent negative proof"
        ),
        "config": {
            "core_kind": arguments.core_kind,
            "max_graphs": arguments.max_graphs,
            "max_nodes_per_search": arguments.max_nodes_per_search,
            "order": arguments.order,
            "shard_count": arguments.shard_count,
            "shard_index": arguments.shard_index,
            "split_depth": arguments.split_depth,
            "timeout_per_search": arguments.timeout_per_search,
        },
        "counts": counts,
        "elapsed_seconds": time.monotonic() - started,
        "first_non_witness": first_non_witness,
        "generator": asdict(identity),
        "python": {
            "executable_basename": Path(sys.executable).name,
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
    }
    atomic_json_write(arguments.output, receipt)
    print(json.dumps(receipt, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0 if first_non_witness is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
