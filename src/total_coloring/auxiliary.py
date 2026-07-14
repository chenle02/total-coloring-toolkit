"""Equitable-partition auxiliary graphs and rainbow extension search.

For ``k = Delta(G) + 1`` with ``k <= n <= 2k``, every equitable proper
``k``-coloring consists of singleton and two-vertex classes. The two-vertex
classes are exactly a matching in the complement. This module enumerates all
such matchings, constructs the Chen--Shan auxiliary graph, and decodes verified
rainbow edge colorings into total colorings of the original graph.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from total_coloring.backends import DEFAULT_SOLVER_BACKEND, SolverBackend, solve_with_backend
from total_coloring.certificates import TotalColoringCertificate, verify_total_coloring
from total_coloring.edge import edge_coloring_problem, verify_edge_coloring
from total_coloring.graph import Edge, SimpleGraph
from total_coloring.model import ColoringProblem
from total_coloring.solver import SearchLimits, SolveResult, SolveStatus


@dataclass(frozen=True, slots=True)
class EquitablePartition:
    """One unlabeled singleton/pair partition in canonical vertex order."""

    pairs: tuple[Edge, ...]
    singletons: tuple[int, ...]

    def __post_init__(self) -> None:
        try:
            raw_pairs = tuple(tuple(edge) for edge in self.pairs)
            singletons = tuple(self.singletons)
        except TypeError as exc:
            raise ValueError("partition pairs and singletons must be iterable") from exc
        checked_pairs: list[Edge] = []
        used: set[int] = set()
        for index, edge in enumerate(raw_pairs):
            if len(edge) != 2:
                raise ValueError(f"pair {index} must contain exactly two endpoints")
            left, right = edge
            if any(isinstance(value, bool) or not isinstance(value, int) for value in edge):
                raise ValueError(f"pair {index} endpoints must be integers")
            if left < 0 or left >= right:
                raise ValueError(f"pair {index} must satisfy 0 <= left < right")
            if left in used or right in used:
                raise ValueError("pairs must form a matching")
            used.update((left, right))
            checked_pairs.append((left, right))
        pairs = tuple(checked_pairs)
        if pairs != tuple(sorted(pairs)):
            raise ValueError("pairs must be lexicographically sorted")

        if any(
            isinstance(vertex, bool) or not isinstance(vertex, int) or vertex < 0
            for vertex in singletons
        ):
            raise ValueError("singletons must be nonnegative integers")
        if singletons != tuple(sorted(set(singletons))):
            raise ValueError("singletons must be unique and sorted")
        if used.intersection(singletons):
            raise ValueError("paired vertices and singletons must be disjoint")
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "singletons", singletons)

    @property
    def class_count(self) -> int:
        return len(self.pairs) + len(self.singletons)

    @classmethod
    def from_complement_matching(
        cls, graph: SimpleGraph, matching: tuple[Edge, ...]
    ) -> EquitablePartition:
        """Validate and convert one complement matching into a partition."""

        expected_pairs, expected_singletons = equitable_class_profile(graph)
        normalized = tuple(
            sorted((left, right) if left < right else (right, left) for left, right in matching)
        )
        if len(normalized) != expected_pairs:
            raise ValueError(f"expected {expected_pairs} pairs, found {len(normalized)}")
        if len(set(normalized)) != len(normalized):
            raise ValueError("matching contains a duplicate edge")

        used: set[int] = set()
        for left, right in normalized:
            if not 0 <= left < right < graph.order:
                raise ValueError(f"invalid pair {(left, right)}")
            if left in used or right in used:
                raise ValueError("pairs do not form a matching")
            if graph.has_edge(left, right):
                raise ValueError(f"pair {(left, right)} is an edge of the original graph")
            used.update((left, right))
        singletons = tuple(vertex for vertex in range(graph.order) if vertex not in used)
        if len(singletons) != expected_singletons:
            raise RuntimeError("internal equitable-partition profile mismatch")
        return cls(pairs=normalized, singletons=singletons)


@dataclass(frozen=True, slots=True)
class AuxiliaryConstruction:
    """Auxiliary graph plus the data needed for lossless certificate decoding."""

    original_graph: SimpleGraph
    partition: EquitablePartition
    graph: SimpleGraph
    distinguished_edges: tuple[Edge, ...]
    class_edge_by_vertex: tuple[Edge, ...]
    added_vertex: int | None

    @property
    def degree_parameter(self) -> int:
        """The parameter ``D = Delta(G) + 1 = |J|``."""

        return self.original_graph.max_degree + 1

    @property
    def distinguished_indices(self) -> tuple[int, ...]:
        edge_to_index = {edge: index for index, edge in enumerate(self.graph.edges)}
        return tuple(edge_to_index[edge] for edge in self.distinguished_edges)


@dataclass(frozen=True, slots=True)
class AuxiliaryWitness:
    """A verified auxiliary edge coloring and its decoded total certificate."""

    partition: EquitablePartition
    auxiliary_graph: SimpleGraph
    distinguished_edges: tuple[Edge, ...]
    auxiliary_edge_colors: tuple[int, ...]
    total_coloring: TotalColoringCertificate


@dataclass(frozen=True, slots=True)
class AuxiliarySearchResult:
    """Outcome of existential search across every relevant partition."""

    status: SolveStatus
    graph_fingerprint: str
    color_count: int
    partitions_started: int
    partitions_completed: int
    candidate_failures: int
    unknown_partitions: int
    witness: AuxiliaryWitness | None
    detail: str


@dataclass(frozen=True, slots=True)
class AuxiliaryPartitionResult:
    """Solver evidence for one fixed equitable partition."""

    construction: AuxiliaryConstruction
    solve_result: SolveResult
    total_coloring: TotalColoringCertificate | None

    @property
    def status(self) -> SolveStatus:
        return self.solve_result.status


@dataclass(frozen=True, slots=True)
class UniversalAuxiliaryResult:
    """Summary of the stronger extension claim over every partition."""

    status: SolveStatus
    graph_fingerprint: str
    color_count: int
    partitions_started: int
    verified_partitions: int
    unknown_partitions: int
    first_nonwitness: AuxiliaryPartitionResult | None
    detail: str


def equitable_class_profile(graph: SimpleGraph) -> tuple[int, int]:
    """Return counts ``(pairs, singletons)`` for ``Delta(G)+1`` classes."""

    if graph.order == 0:
        raise ValueError("the auxiliary construction is undefined for the null graph")
    class_count = graph.max_degree + 1
    if not class_count <= graph.order <= 2 * class_count:
        raise ValueError("Delta(G)+1 equitable classes are not restricted to sizes one and two")
    pair_count = graph.order - class_count
    singleton_count = 2 * class_count - graph.order
    return pair_count, singleton_count


def iter_complement_matchings(graph: SimpleGraph, size: int) -> Iterator[tuple[Edge, ...]]:
    """Enumerate all complement matchings of ``size`` deterministically."""

    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("matching size must be a nonnegative integer")
    if 2 * size > graph.order:
        return
    complement_edges = tuple(
        (left, right)
        for left in range(graph.order)
        for right in range(left + 1, graph.order)
        if not graph.has_edge(left, right)
    )
    chosen: list[Edge] = []
    used: set[int] = set()

    def visit(start: int) -> Iterator[tuple[Edge, ...]]:
        remaining = size - len(chosen)
        if remaining == 0:
            yield tuple(chosen)
            return
        if graph.order - len(used) < 2 * remaining:
            return
        if len(complement_edges) - start < remaining:
            return
        for index in range(start, len(complement_edges)):
            left, right = complement_edges[index]
            if left in used or right in used:
                continue
            chosen.append((left, right))
            used.update((left, right))
            yield from visit(index + 1)
            used.remove(left)
            used.remove(right)
            chosen.pop()

    yield from visit(0)


def iter_equitable_partitions(graph: SimpleGraph) -> Iterator[EquitablePartition]:
    """Enumerate every singleton/pair equitable ``Delta(G)+1`` partition."""

    pair_count, _singleton_count = equitable_class_profile(graph)
    for matching in iter_complement_matchings(graph, pair_count):
        yield EquitablePartition.from_complement_matching(graph, matching)


def construct_auxiliary_graph(
    graph: SimpleGraph, partition: EquitablePartition
) -> AuxiliaryConstruction:
    """Build the matching-plus-star auxiliary graph for one partition."""

    validated = EquitablePartition.from_complement_matching(graph, partition.pairs)
    if validated != partition:
        raise ValueError("partition singleton list is not canonical for its pairs")

    added_vertex = graph.order if partition.singletons else None
    star_edges: tuple[Edge, ...] = (
        tuple((vertex, added_vertex) for vertex in partition.singletons)
        if added_vertex is not None
        else ()
    )
    distinguished = tuple(sorted((*partition.pairs, *star_edges)))
    auxiliary = SimpleGraph.from_edges(
        graph.order + (added_vertex is not None), (*graph.edges, *distinguished)
    )

    class_edge: list[Edge | None] = [None] * graph.order
    for edge in partition.pairs:
        left, right = edge
        class_edge[left] = edge
        class_edge[right] = edge
    for edge in star_edges:
        vertex, _center = edge
        class_edge[vertex] = edge
    if any(edge is None for edge in class_edge):
        raise RuntimeError("internal auxiliary construction left a vertex without a class edge")

    degree_parameter = graph.max_degree + 1
    if len(distinguished) != degree_parameter:
        raise RuntimeError("distinguished family has the wrong size")
    if auxiliary.max_degree != degree_parameter:
        raise RuntimeError("auxiliary graph has the wrong maximum degree")

    return AuxiliaryConstruction(
        original_graph=graph,
        partition=partition,
        graph=auxiliary,
        distinguished_edges=distinguished,
        class_edge_by_vertex=tuple(edge for edge in class_edge if edge is not None),
        added_vertex=added_vertex,
    )


def auxiliary_coloring_problem(
    construction: AuxiliaryConstruction,
    color_count: int,
    *,
    fix_distinguished_colors: bool = True,
) -> ColoringProblem:
    """Construct the rainbow edge-coloring problem for an auxiliary graph."""

    return edge_coloring_problem(
        construction.graph,
        color_count,
        distinguished_edges=construction.distinguished_edges,
        fix_distinguished_colors=fix_distinguished_colors,
    )


def decode_auxiliary_coloring(
    construction: AuxiliaryConstruction,
    color_count: int,
    auxiliary_edge_colors: tuple[int, ...],
) -> TotalColoringCertificate:
    """Independently verify and decode an auxiliary witness."""

    edge_verification = verify_edge_coloring(
        construction.graph,
        color_count,
        auxiliary_edge_colors,
        distinguished_edges=construction.distinguished_edges,
    )
    edge_verification.require_valid()
    auxiliary_index = {edge: index for index, edge in enumerate(construction.graph.edges)}
    vertex_colors = tuple(
        auxiliary_edge_colors[auxiliary_index[class_edge]]
        for class_edge in construction.class_edge_by_vertex
    )
    original_edge_colors = tuple(
        auxiliary_edge_colors[auxiliary_index[edge]] for edge in construction.original_graph.edges
    )
    certificate = TotalColoringCertificate.create(
        construction.original_graph,
        color_count,
        vertex_colors,
        original_edge_colors,
    )
    verify_total_coloring(construction.original_graph, certificate).require_valid()
    return certificate


def solve_auxiliary_partition(
    graph: SimpleGraph,
    partition: EquitablePartition,
    color_count: int,
    *,
    limits: SearchLimits | None = None,
    fix_distinguished_colors: bool = True,
    backend: SolverBackend = DEFAULT_SOLVER_BACKEND,
) -> AuxiliaryPartitionResult:
    """Solve and independently verify one fixed-partition extension problem."""

    construction = construct_auxiliary_graph(graph, partition)
    problem = auxiliary_coloring_problem(
        construction,
        color_count,
        fix_distinguished_colors=fix_distinguished_colors,
    )
    solved = solve_with_backend(problem, backend=backend, limits=limits)
    if solved.status is not SolveStatus.WITNESS:
        return AuxiliaryPartitionResult(construction, solved, None)
    if solved.assignment is None:
        failed = SolveResult(
            status=SolveStatus.ERROR,
            problem_digest=solved.problem_digest,
            assignment=None,
            stats=solved.stats,
            detail="solver reported a witness without an assignment",
        )
        return AuxiliaryPartitionResult(construction, failed, None)
    try:
        certificate = decode_auxiliary_coloring(construction, color_count, solved.assignment)
    except ValueError as exc:
        failed = SolveResult(
            status=SolveStatus.ERROR,
            problem_digest=solved.problem_digest,
            assignment=None,
            stats=solved.stats,
            detail=f"independent witness verification failed: {exc}",
        )
        return AuxiliaryPartitionResult(construction, failed, None)
    return AuxiliaryPartitionResult(construction, solved, certificate)


def check_all_auxiliary_partitions(
    graph: SimpleGraph,
    color_count: int,
    *,
    limits_per_partition: SearchLimits | None = None,
    max_partitions: int | None = None,
    fix_distinguished_colors: bool = True,
    backend: SolverBackend = DEFAULT_SOLVER_BACKEND,
) -> UniversalAuxiliaryResult:
    """Test the stronger universal extension statement for one graph.

    ``WITNESS`` means every enumerated partition produced a semantically
    verified auxiliary witness. It is a bounded computational result, not an
    unbounded theorem or a compact proof artifact.
    """

    degree_parameter = graph.max_degree + 1
    if isinstance(color_count, bool) or not isinstance(color_count, int):
        raise ValueError("color_count must be an integer")
    if color_count < degree_parameter:
        raise ValueError(f"color_count must be at least D={degree_parameter}")
    if max_partitions is not None and (
        isinstance(max_partitions, bool)
        or not isinstance(max_partitions, int)
        or max_partitions <= 0
    ):
        raise ValueError("max_partitions must be a positive integer or None")
    if not isinstance(fix_distinguished_colors, bool):
        raise ValueError("fix_distinguished_colors must be a boolean")
    if not isinstance(backend, SolverBackend):
        raise ValueError("backend must be a SolverBackend")

    started = 0
    verified = 0
    unknown = 0
    first_unknown: AuxiliaryPartitionResult | None = None
    truncated = False
    for partition in iter_equitable_partitions(graph):
        if max_partitions is not None and started >= max_partitions:
            truncated = True
            break
        started += 1
        result = solve_auxiliary_partition(
            graph,
            partition,
            color_count,
            limits=limits_per_partition,
            fix_distinguished_colors=fix_distinguished_colors,
            backend=backend,
        )
        if result.status is SolveStatus.WITNESS:
            verified += 1
        elif result.status is SolveStatus.CANDIDATE_UNSAT:
            return UniversalAuxiliaryResult(
                SolveStatus.CANDIDATE_UNSAT,
                graph.fingerprint,
                color_count,
                started,
                verified,
                unknown,
                result,
                "one partition exhausted without an extension; no independent UNSAT proof attached",
            )
        elif result.status is SolveStatus.UNKNOWN:
            unknown += 1
            if first_unknown is None:
                first_unknown = result
        else:
            return UniversalAuxiliaryResult(
                SolveStatus.ERROR,
                graph.fingerprint,
                color_count,
                started,
                verified,
                unknown,
                result,
                f"partition solver failed: {result.solve_result.detail}",
            )

    if started == 0:
        return UniversalAuxiliaryResult(
            SolveStatus.ERROR,
            graph.fingerprint,
            color_count,
            0,
            0,
            0,
            None,
            "partition enumeration produced no equitable partitions",
        )
    if truncated or unknown:
        reasons: list[str] = []
        if truncated:
            reasons.append("partition limit reached")
        if unknown:
            reasons.append(f"{unknown} solver branches were incomplete")
        return UniversalAuxiliaryResult(
            SolveStatus.UNKNOWN,
            graph.fingerprint,
            color_count,
            started,
            verified,
            unknown,
            first_unknown,
            "; ".join(reasons),
        )
    return UniversalAuxiliaryResult(
        SolveStatus.WITNESS,
        graph.fingerprint,
        color_count,
        started,
        verified,
        0,
        None,
        "every equitable partition produced an independently verified auxiliary witness",
    )


def search_auxiliary_extensions(
    graph: SimpleGraph,
    color_count: int,
    *,
    limits_per_partition: SearchLimits | None = None,
    max_partitions: int | None = None,
    backend: SolverBackend = DEFAULT_SOLVER_BACKEND,
) -> AuxiliarySearchResult:
    """Search all partitions until a verified extension is found.

    An exhausted DSATUR branch contributes only ``CANDIDATE_UNSAT`` evidence.
    If any branch or the partition enumeration is truncated, a witness-free
    global result is ``UNKNOWN``.
    """

    degree_parameter = graph.max_degree + 1
    if isinstance(color_count, bool) or not isinstance(color_count, int):
        raise ValueError("color_count must be an integer")
    if color_count < degree_parameter:
        raise ValueError(f"color_count must be at least D={degree_parameter}")
    if max_partitions is not None and (
        isinstance(max_partitions, bool)
        or not isinstance(max_partitions, int)
        or max_partitions <= 0
    ):
        raise ValueError("max_partitions must be a positive integer or None")
    if not isinstance(backend, SolverBackend):
        raise ValueError("backend must be a SolverBackend")

    started = 0
    completed = 0
    candidate_failures = 0
    unknown = 0
    truncated = False
    for partition in iter_equitable_partitions(graph):
        if max_partitions is not None and started >= max_partitions:
            truncated = True
            break
        started += 1
        partition_result = solve_auxiliary_partition(
            graph,
            partition,
            color_count,
            limits=limits_per_partition,
            backend=backend,
        )
        construction = partition_result.construction
        solved = partition_result.solve_result
        if solved.status is SolveStatus.WITNESS:
            if solved.assignment is None or partition_result.total_coloring is None:
                return AuxiliarySearchResult(
                    SolveStatus.ERROR,
                    graph.fingerprint,
                    color_count,
                    started,
                    completed,
                    candidate_failures,
                    unknown,
                    None,
                    "partition solver returned an incomplete witness",
                )
            completed += 1
            return AuxiliarySearchResult(
                SolveStatus.WITNESS,
                graph.fingerprint,
                color_count,
                started,
                completed,
                candidate_failures,
                unknown,
                AuxiliaryWitness(
                    partition,
                    construction.graph,
                    construction.distinguished_edges,
                    solved.assignment,
                    partition_result.total_coloring,
                ),
                "verified auxiliary witness decoded to a valid total coloring",
            )
        if solved.status is SolveStatus.CANDIDATE_UNSAT:
            candidate_failures += 1
            completed += 1
        elif solved.status is SolveStatus.UNKNOWN:
            unknown += 1
        else:
            return AuxiliarySearchResult(
                SolveStatus.ERROR,
                graph.fingerprint,
                color_count,
                started,
                completed,
                candidate_failures,
                unknown,
                None,
                f"solver backend failed: {solved.detail}",
            )

    if truncated or unknown:
        reasons: list[str] = []
        if truncated:
            reasons.append("partition limit reached")
        if unknown:
            reasons.append(f"{unknown} solver branches were incomplete")
        return AuxiliarySearchResult(
            SolveStatus.UNKNOWN,
            graph.fingerprint,
            color_count,
            started,
            completed,
            candidate_failures,
            unknown,
            None,
            "; ".join(reasons),
        )
    if started == 0:
        return AuxiliarySearchResult(
            SolveStatus.ERROR,
            graph.fingerprint,
            color_count,
            0,
            0,
            0,
            0,
            None,
            "partition enumeration produced no equitable partitions",
        )
    return AuxiliarySearchResult(
        SolveStatus.CANDIDATE_UNSAT,
        graph.fingerprint,
        color_count,
        started,
        completed,
        candidate_failures,
        0,
        None,
        "every enumerated partition exhausted without an extension; no independent "
        "UNSAT proofs attached",
    )
