"""Replayable universal auxiliary-coloring census orchestration.

Unlike :mod:`total_coloring.census`, which searches for one successful
partition, this module records every canonical equitable partition and every
configured backend/palette check.  Positive solver output is treated as an
untrusted witness and is semantically replayed whenever a record is parsed.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import BinaryIO, Final, cast

from total_coloring.auxiliary import (
    EquitablePartition,
    auxiliary_coloring_problem,
    construct_auxiliary_graph,
    decode_auxiliary_coloring,
    iter_equitable_partitions,
)
from total_coloring.backends import SolverBackend, solve_with_backend
from total_coloring.census import (
    CensusError,
    CensusFormatError,
    CensusResumeError,
    ToolkitIdentity,
    _artifact_digest,
    _atomic_write,
    _exclusive_output_lock,
    _fsync_directory,
    _generator_dict,
    _load_canonical_json,
    _require_digest,
    _require_exact_keys,
    _require_nonnegative_int,
    _require_positive_int,
    _require_string,
    _write_all,
    detect_toolkit_identity,
)
from total_coloring.edge import verify_edge_coloring
from total_coloring.geng import GengIdentity, GengSpec, geng_identity, resolve_geng, stream_geng
from total_coloring.graph import (
    Edge,
    GraphFormatError,
    SimpleGraph,
    canonical_json_bytes,
    sha256_hex,
    strict_json_loads,
)
from total_coloring.graph6 import decode_graph6, encode_graph6
from total_coloring.solver import SearchLimits, SolveResult, SolveStatus

UNIVERSAL_RECORD_SCHEMA_VERSION: Final = "total-coloring.universal-census-record.v1"
UNIVERSAL_MANIFEST_SCHEMA_VERSION: Final = "total-coloring.universal-census-manifest.v1"
UNIVERSAL_COMPLETION_SCHEMA_VERSION: Final = "total-coloring.universal-census-completion.v1"
UNIVERSAL_OBJECTIVE: Final = "universal_auxiliary_extension"
PARTITION_ENUMERATOR_ID: Final = "complement-matchings-lexicographic-v1"
MAX_UNIVERSAL_RECORD_BYTES: Final = 16 * 1024 * 1024

_RECORDS_NAME: Final = "records.jsonl"
_PARTIAL_NAME: Final = ".records.jsonl.partial"
_MANIFEST_NAME: Final = "manifest.json"
_COMPLETION_NAME: Final = "completion.json"


class UniversalCensusStatus(StrEnum):
    """Terminal classification of one generated graph."""

    VERIFIED_ALL = "verified_all"
    CANDIDATE_UNSAT = "candidate_unsat"
    UNKNOWN = "unknown"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True, order=True)
class UniversalCheckSpec:
    """One backend/palette target applied to every equitable partition."""

    backend: SolverBackend
    palette_offset: int

    def __post_init__(self) -> None:
        if not isinstance(self.backend, SolverBackend):
            raise ValueError("backend must be a SolverBackend")
        _require_nonnegative_int(self.palette_offset, name="palette_offset")

    @property
    def identifier(self) -> str:
        return f"{self.backend.value}:D+{self.palette_offset}"

    def to_dict(self) -> dict[str, object]:
        return {
            "backend_id": self.backend.value,
            "palette_offset": self.palette_offset,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UniversalCheckSpec:
        _require_exact_keys(value, {"backend_id", "palette_offset"}, name="check spec")
        try:
            backend = SolverBackend(_require_string(value["backend_id"], "backend_id"))
        except ValueError as exc:
            raise CensusFormatError("unsupported solver backend") from exc
        return cls(
            backend=backend,
            palette_offset=_require_nonnegative_int(value["palette_offset"], name="palette_offset"),
        )


DEFAULT_UNIVERSAL_CHECKS: Final = (
    UniversalCheckSpec(SolverBackend.DSATUR, 1),
    UniversalCheckSpec(SolverBackend.DSATUR, 2),
    UniversalCheckSpec(SolverBackend.STATIC, 1),
)


def count_equitable_partitions_dp(graph: SimpleGraph) -> int:
    """Count relevant complement matchings by an independent vertex-mask DP.

    This does not call the canonical partition enumerator.  It therefore gives
    record validation an independent completeness count before sequence-level
    comparison with :func:`iter_equitable_partitions`.
    """

    if not isinstance(graph, SimpleGraph):
        raise ValueError("graph must be a SimpleGraph")
    if graph.order == 0:
        raise ValueError("the auxiliary construction is undefined for the null graph")
    degree_parameter = graph.max_degree + 1
    if not degree_parameter <= graph.order <= 2 * degree_parameter:
        raise ValueError("equitable classes are not restricted to sizes one and two")
    pairs_needed = graph.order - degree_parameter
    complement_masks = tuple(
        sum(
            1 << other
            for other in range(graph.order)
            if other != vertex and not graph.has_edge(vertex, other)
        )
        for vertex in range(graph.order)
    )

    @cache
    def count(available: int, remaining: int) -> int:
        if remaining == 0:
            return 1
        if available.bit_count() < 2 * remaining:
            return 0
        first_bit = available & -available
        first = first_bit.bit_length() - 1
        without_first = available ^ first_bit
        total = count(without_first, remaining)
        partners = complement_masks[first] & without_first
        while partners:
            partner_bit = partners & -partners
            total += count(without_first ^ partner_bit, remaining - 1)
            partners ^= partner_bit
        return total

    return count((1 << graph.order) - 1, pairs_needed)


@dataclass(frozen=True, slots=True)
class UniversalCensusConfig:
    """Complete scientific and checkpoint configuration for one shard."""

    geng: GengSpec
    checks: tuple[UniversalCheckSpec, ...] = DEFAULT_UNIVERSAL_CHECKS
    require_high_degree: bool = True
    limits_per_check: SearchLimits = field(default_factory=SearchLimits)
    checkpoint_interval: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.geng, GengSpec):
            raise ValueError("geng must be a GengSpec")
        try:
            checks = tuple(self.checks)
        except TypeError as exc:
            raise ValueError("checks must be iterable") from exc
        if not checks or not all(isinstance(check, UniversalCheckSpec) for check in checks):
            raise ValueError("checks must contain at least one UniversalCheckSpec")
        if len(set(checks)) != len(checks):
            raise ValueError("checks must be unique")
        object.__setattr__(self, "checks", tuple(sorted(checks)))
        if not isinstance(self.require_high_degree, bool):
            raise ValueError("require_high_degree must be a boolean")
        if not isinstance(self.limits_per_check, SearchLimits):
            raise ValueError("limits_per_check must be SearchLimits")
        timeout = self.limits_per_check.timeout_seconds
        if timeout is not None and not math.isfinite(timeout):
            raise ValueError("timeout_seconds must be finite")
        _require_positive_int(self.checkpoint_interval, name="checkpoint_interval")

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_interval": self.checkpoint_interval,
            "checks": [check.to_dict() for check in self.checks],
            "filters": {"require_high_degree": self.require_high_degree},
            "fix_distinguished_colors": True,
            "generator_spec": {
                "connected": self.geng.connected,
                "max_degree": self.geng.max_degree,
                "min_degree": self.geng.min_degree,
                "order": self.geng.order,
                "shard_count": self.geng.shard_count,
                "shard_index": self.geng.shard_index,
            },
            "partition_enumerator": PARTITION_ENUMERATOR_ID,
            "search_limits": {
                "max_nodes_per_check": self.limits_per_check.max_nodes,
                "timeout_seconds_per_check": self.limits_per_check.timeout_seconds,
            },
        }


@dataclass(frozen=True, slots=True)
class DeterministicSearchStats:
    """Machine-independent counters retained from a solver result."""

    nodes: int
    backtracks: int

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.nodes, name="nodes")
        _require_nonnegative_int(self.backtracks, name="backtracks")

    def to_dict(self) -> dict[str, int]:
        return {"backtracks": self.backtracks, "nodes": self.nodes}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DeterministicSearchStats:
        _require_exact_keys(value, {"backtracks", "nodes"}, name="search stats")
        return cls(
            nodes=_require_nonnegative_int(value["nodes"], name="nodes"),
            backtracks=_require_nonnegative_int(value["backtracks"], name="backtracks"),
        )


@dataclass(frozen=True, slots=True)
class UniversalCheckResult:
    """Replayable outcome of one backend/palette check."""

    backend: SolverBackend
    palette_offset: int
    color_count: int
    problem_digest: str
    status: SolveStatus
    stats: DeterministicSearchStats
    detail: str
    auxiliary_edge_colors: tuple[int, ...] | None

    def __post_init__(self) -> None:
        if not isinstance(self.backend, SolverBackend):
            raise ValueError("backend must be a SolverBackend")
        _require_nonnegative_int(self.palette_offset, name="palette_offset")
        _require_nonnegative_int(self.color_count, name="color_count")
        _require_digest(self.problem_digest, name="problem_digest")
        if not isinstance(self.status, SolveStatus):
            raise ValueError("status must be a SolveStatus")
        if not isinstance(self.stats, DeterministicSearchStats):
            raise ValueError("stats must be DeterministicSearchStats")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("detail must be a nonempty string")
        if self.auxiliary_edge_colors is not None:
            try:
                assignment = tuple(self.auxiliary_edge_colors)
            except TypeError as exc:
                raise ValueError("auxiliary_edge_colors must be iterable or None") from exc
            for index, color in enumerate(assignment):
                _require_nonnegative_int(color, name=f"auxiliary_edge_colors[{index}]")
            object.__setattr__(self, "auxiliary_edge_colors", assignment)
        if (self.status is SolveStatus.WITNESS) != (self.auxiliary_edge_colors is not None):
            raise ValueError("exactly witness checks must carry an auxiliary assignment")

    @property
    def spec(self) -> UniversalCheckSpec:
        return UniversalCheckSpec(self.backend, self.palette_offset)

    def to_dict(self) -> dict[str, object]:
        return {
            "auxiliary_edge_colors": (
                None if self.auxiliary_edge_colors is None else list(self.auxiliary_edge_colors)
            ),
            "backend_id": self.backend.value,
            "color_count": self.color_count,
            "detail": self.detail,
            "palette_offset": self.palette_offset,
            "problem_digest": self.problem_digest,
            "stats": self.stats.to_dict(),
            "status": self.status.value,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UniversalCheckResult:
        expected = {
            "auxiliary_edge_colors",
            "backend_id",
            "color_count",
            "detail",
            "palette_offset",
            "problem_digest",
            "stats",
            "status",
        }
        _require_exact_keys(value, expected, name="universal check result")
        try:
            backend = SolverBackend(_require_string(value["backend_id"], "backend_id"))
            status = SolveStatus(_require_string(value["status"], "status"))
        except ValueError as exc:
            raise CensusFormatError("unsupported backend or solve status") from exc
        stats_value = value["stats"]
        if not isinstance(stats_value, Mapping):
            raise CensusFormatError("stats must be an object")
        assignment_value = value["auxiliary_edge_colors"]
        assignment: tuple[int, ...] | None
        if assignment_value is None:
            assignment = None
        else:
            assignment = _parse_nonnegative_array(assignment_value, "auxiliary_edge_colors")
        try:
            return cls(
                backend=backend,
                palette_offset=_require_nonnegative_int(
                    value["palette_offset"], name="palette_offset"
                ),
                color_count=_require_nonnegative_int(value["color_count"], name="color_count"),
                problem_digest=_require_string(value["problem_digest"], "problem_digest"),
                status=status,
                stats=DeterministicSearchStats.from_mapping(
                    cast(Mapping[str, object], stats_value)
                ),
                detail=_require_string(value["detail"], "detail"),
                auxiliary_edge_colors=assignment,
            )
        except ValueError as exc:
            raise CensusFormatError(f"invalid universal check result: {exc}") from exc


@dataclass(frozen=True, slots=True)
class UniversalPartitionResult:
    """Canonical partition, reconstructed auxiliary graph, and all checks."""

    index: int
    partition_fingerprint: str
    pairs: tuple[Edge, ...]
    singletons: tuple[int, ...]
    auxiliary_graph6: str
    auxiliary_graph_fingerprint: str
    distinguished_edges: tuple[Edge, ...]
    checks: tuple[UniversalCheckResult, ...]

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.index, name="partition index")
        _require_digest(self.partition_fingerprint, name="partition_fingerprint")
        object.__setattr__(self, "pairs", _freeze_edges(self.pairs, "partition pairs"))
        object.__setattr__(
            self,
            "singletons",
            _freeze_sorted_integers(self.singletons, "partition singletons"),
        )
        object.__setattr__(
            self,
            "distinguished_edges",
            _freeze_edges(self.distinguished_edges, "distinguished_edges"),
        )
        _require_digest(self.auxiliary_graph_fingerprint, name="auxiliary_graph_fingerprint")
        auxiliary = decode_graph6(self.auxiliary_graph6)
        if encode_graph6(auxiliary) != self.auxiliary_graph6:
            raise ValueError("auxiliary_graph6 must be canonical and headerless")
        if auxiliary.fingerprint != self.auxiliary_graph_fingerprint:
            raise ValueError("auxiliary graph fingerprint does not match graph6")
        try:
            checks = tuple(self.checks)
        except TypeError as exc:
            raise ValueError("checks must be iterable") from exc
        if not checks or not all(isinstance(check, UniversalCheckResult) for check in checks):
            raise ValueError("partition must contain at least one check result")
        specs = tuple(check.spec for check in checks)
        if specs != tuple(sorted(set(specs))):
            raise ValueError("partition checks must be unique and canonically ordered")
        object.__setattr__(self, "checks", checks)

    @property
    def partition(self) -> EquitablePartition:
        return EquitablePartition(self.pairs, self.singletons)

    def require_valid_for(self, graph: SimpleGraph, expected_index: int) -> None:
        if self.index != expected_index:
            raise ValueError("partition index is discontinuous")
        partition = EquitablePartition.from_complement_matching(graph, self.pairs)
        if partition.singletons != self.singletons:
            raise ValueError("partition singleton list does not match its complement matching")
        partition_dict = _partition_dict(partition)
        if sha256_hex(partition_dict) != self.partition_fingerprint:
            raise ValueError("partition fingerprint does not match partition")
        construction = construct_auxiliary_graph(graph, partition)
        if encode_graph6(construction.graph) != self.auxiliary_graph6:
            raise ValueError("auxiliary graph does not match reconstructed partition")
        if construction.graph.fingerprint != self.auxiliary_graph_fingerprint:
            raise ValueError("auxiliary graph fingerprint does not match reconstruction")
        if construction.distinguished_edges != self.distinguished_edges:
            raise ValueError("distinguished edges do not match reconstruction")
        degree_parameter = graph.max_degree + 1
        for check in self.checks:
            expected_color_count = degree_parameter + check.palette_offset
            if check.color_count != expected_color_count:
                raise ValueError("check color count does not equal D plus its palette offset")
            problem = auxiliary_coloring_problem(construction, check.color_count)
            if problem.semantic_digest != check.problem_digest:
                raise ValueError("problem digest does not match reconstructed check")
            if check.status is SolveStatus.WITNESS:
                assert check.auxiliary_edge_colors is not None
                violations = problem.verify_assignment(check.auxiliary_edge_colors)
                if violations:
                    raise ValueError("stored witness violates the reconstructed coloring problem")
                verify_edge_coloring(
                    construction.graph,
                    check.color_count,
                    check.auxiliary_edge_colors,
                    distinguished_edges=construction.distinguished_edges,
                ).require_valid()
                decode_auxiliary_coloring(
                    construction,
                    check.color_count,
                    check.auxiliary_edge_colors,
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "auxiliary": {
                "distinguished_edges": [list(edge) for edge in self.distinguished_edges],
                "graph6": self.auxiliary_graph6,
                "graph_fingerprint": self.auxiliary_graph_fingerprint,
            },
            "checks": [check.to_dict() for check in self.checks],
            "index": self.index,
            "partition": {
                "fingerprint": self.partition_fingerprint,
                "pairs": [list(edge) for edge in self.pairs],
                "singletons": list(self.singletons),
            },
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UniversalPartitionResult:
        _require_exact_keys(
            value,
            {"auxiliary", "checks", "index", "partition"},
            name="universal partition result",
        )
        auxiliary = value["auxiliary"]
        partition = value["partition"]
        checks = value["checks"]
        if not isinstance(auxiliary, Mapping) or not isinstance(partition, Mapping):
            raise CensusFormatError("partition and auxiliary must be objects")
        if isinstance(checks, str | bytes) or not isinstance(checks, Sequence):
            raise CensusFormatError("checks must be an array")
        auxiliary_mapping = cast(Mapping[str, object], auxiliary)
        partition_mapping = cast(Mapping[str, object], partition)
        _require_exact_keys(
            auxiliary_mapping,
            {"distinguished_edges", "graph6", "graph_fingerprint"},
            name="auxiliary construction",
        )
        _require_exact_keys(
            partition_mapping,
            {"fingerprint", "pairs", "singletons"},
            name="partition",
        )
        parsed_checks: list[UniversalCheckResult] = []
        for index, raw_check in enumerate(checks):
            if not isinstance(raw_check, Mapping):
                raise CensusFormatError(f"checks[{index}] must be an object")
            parsed_checks.append(
                UniversalCheckResult.from_mapping(cast(Mapping[str, object], raw_check))
            )
        try:
            return cls(
                index=_require_nonnegative_int(value["index"], name="partition index"),
                partition_fingerprint=_require_string(
                    partition_mapping["fingerprint"], "partition fingerprint"
                ),
                pairs=_parse_edges(partition_mapping["pairs"], "partition pairs"),
                singletons=_parse_nonnegative_array(
                    partition_mapping["singletons"], "partition singletons"
                ),
                auxiliary_graph6=_require_string(auxiliary_mapping["graph6"], "graph6"),
                auxiliary_graph_fingerprint=_require_string(
                    auxiliary_mapping["graph_fingerprint"], "graph_fingerprint"
                ),
                distinguished_edges=_parse_edges(
                    auxiliary_mapping["distinguished_edges"], "distinguished_edges"
                ),
                checks=tuple(parsed_checks),
            )
        except (GraphFormatError, ValueError) as exc:
            raise CensusFormatError(f"invalid universal partition result: {exc}") from exc


@dataclass(frozen=True, slots=True)
class UniversalCensusRecord:
    """One generated graph and its complete universal-check transcript."""

    run_fingerprint: str
    index: int
    graph6: str
    graph_fingerprint: str
    order: int
    size: int
    min_degree: int
    max_degree: int
    degree_parameter: int
    eligible: bool
    status: UniversalCensusStatus
    outcome_code: str
    detail: str
    partition_count: int
    partitions: tuple[UniversalPartitionResult, ...]

    def __post_init__(self) -> None:
        _require_digest(self.run_fingerprint, name="run_fingerprint")
        _require_digest(self.graph_fingerprint, name="graph_fingerprint")
        for name in (
            "index",
            "order",
            "size",
            "min_degree",
            "max_degree",
            "degree_parameter",
            "partition_count",
        ):
            _require_nonnegative_int(getattr(self, name), name=name)
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")
        if not isinstance(self.status, UniversalCensusStatus):
            raise ValueError("status must be UniversalCensusStatus")
        if not _valid_identifier(self.outcome_code):
            raise ValueError("outcome_code must be a lowercase identifier")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("detail must be a nonempty string")
        try:
            partitions = tuple(self.partitions)
        except TypeError as exc:
            raise ValueError("partitions must be iterable") from exc
        if not all(isinstance(item, UniversalPartitionResult) for item in partitions):
            raise ValueError("partitions must contain UniversalPartitionResult values")
        object.__setattr__(self, "partitions", partitions)
        if self.partition_count != len(partitions):
            raise ValueError("partition_count must equal the nested partition list length")

        graph = decode_graph6(self.graph6)
        if encode_graph6(graph) != self.graph6:
            raise ValueError("graph6 must be canonical and headerless")
        metadata = (
            graph.fingerprint,
            graph.order,
            graph.size,
            graph.min_degree,
            graph.max_degree,
            graph.max_degree + 1,
        )
        if metadata != (
            self.graph_fingerprint,
            self.order,
            self.size,
            self.min_degree,
            self.max_degree,
            self.degree_parameter,
        ):
            raise ValueError("graph metadata does not match graph6")

        if not self.eligible:
            if partitions:
                raise ValueError("out-of-scope or failed-preflight graphs cannot have partitions")
            if self.status not in {UniversalCensusStatus.SKIPPED, UniversalCensusStatus.ERROR}:
                raise ValueError("ineligible graphs must be skipped or error records")
            return
        if self.status is UniversalCensusStatus.SKIPPED:
            raise ValueError("eligible graphs cannot be skipped")
        independent_partition_count = count_equitable_partitions_dp(graph)
        if self.partition_count != independent_partition_count:
            raise ValueError(
                "partition_count disagrees with the independent complement-matching DP"
            )
        partition_identities = tuple(partition.pairs for partition in partitions)
        if len(set(partition_identities)) != len(partition_identities):
            raise ValueError("stored canonical equitable partitions must be unique")
        expected_partitions = tuple(iter_equitable_partitions(graph))
        if not expected_partitions:
            raise ValueError("eligible graph has no canonical equitable partitions")
        if len(expected_partitions) != len(partitions):
            raise ValueError("record does not contain every canonical equitable partition")
        expected_specs: tuple[UniversalCheckSpec, ...] | None = None
        disagreement = False
        all_statuses: list[SolveStatus] = []
        for partition_index, (stored, expected) in enumerate(
            zip(partitions, expected_partitions, strict=True)
        ):
            if stored.partition != expected:
                raise ValueError("stored partitions are incomplete, duplicated, or reordered")
            stored.require_valid_for(graph, partition_index)
            specs = tuple(check.spec for check in stored.checks)
            if expected_specs is None:
                expected_specs = specs
            elif specs != expected_specs:
                raise ValueError("every partition must contain the same configured checks")
            offset_one = [check.status for check in stored.checks if check.palette_offset == 1]
            if len(offset_one) > 1 and len(set(offset_one)) != 1:
                disagreement = True
            all_statuses.extend(check.status for check in stored.checks)

        expected_status, expected_code = _aggregate_status(all_statuses, disagreement)
        if self.status is not expected_status:
            raise ValueError("record status does not match nested check outcomes")
        if self.outcome_code != expected_code:
            raise ValueError("outcome_code does not match nested check outcomes")
        if self.status is UniversalCensusStatus.VERIFIED_ALL and any(
            status is not SolveStatus.WITNESS for status in all_statuses
        ):
            raise ValueError("verified_all requires every configured check to be a witness")

    def require_valid_for_config(self, config: UniversalCensusConfig) -> None:
        """Bind structural record validation to one run's exact domain config."""

        if not isinstance(config, UniversalCensusConfig):
            raise ValueError("config must be UniversalCensusConfig")
        graph = decode_graph6(self.graph6)
        degree_parameter = graph.max_degree + 1
        if graph.order != config.geng.order:
            expected_eligible = False
            expected_status = UniversalCensusStatus.ERROR
            expected_outcome = "generator_order_mismatch"
        elif config.require_high_degree and 2 * graph.max_degree < graph.order:
            expected_eligible = False
            expected_status = UniversalCensusStatus.SKIPPED
            expected_outcome = "outside_high_degree_filter"
        elif graph.order == 0 or not degree_parameter <= graph.order <= 2 * degree_parameter:
            expected_eligible = False
            expected_status = UniversalCensusStatus.SKIPPED
            expected_outcome = "outside_auxiliary_regime"
        else:
            expected_eligible = True
            expected_status = self.status
            expected_outcome = self.outcome_code
        if self.eligible is not expected_eligible:
            raise ValueError("record eligibility does not match the configured domain")
        if self.status is not expected_status or self.outcome_code != expected_outcome:
            raise ValueError("record classification does not match the configured domain")
        if expected_eligible and self.check_specs != config.checks:
            raise ValueError("record checks do not match the configured check matrix")

    @property
    def check_specs(self) -> tuple[UniversalCheckSpec, ...]:
        return (
            () if not self.partitions else tuple(check.spec for check in self.partitions[0].checks)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "degree_parameter": self.degree_parameter,
            "detail": self.detail,
            "eligible": self.eligible,
            "graph6": self.graph6,
            "graph_fingerprint": self.graph_fingerprint,
            "index": self.index,
            "max_degree": self.max_degree,
            "min_degree": self.min_degree,
            "order": self.order,
            "outcome_code": self.outcome_code,
            "partition_count": self.partition_count,
            "partitions": [partition.to_dict() for partition in self.partitions],
            "run_fingerprint": self.run_fingerprint,
            "schema_version": UNIVERSAL_RECORD_SCHEMA_VERSION,
            "size": self.size,
            "status": self.status.value,
        }

    def to_json(self) -> str:
        return canonical_json_bytes(self.to_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> UniversalCensusRecord:
        expected = {
            "degree_parameter",
            "detail",
            "eligible",
            "graph6",
            "graph_fingerprint",
            "index",
            "max_degree",
            "min_degree",
            "order",
            "outcome_code",
            "partition_count",
            "partitions",
            "run_fingerprint",
            "schema_version",
            "size",
            "status",
        }
        _require_exact_keys(value, expected, name="universal census record")
        if value["schema_version"] != UNIVERSAL_RECORD_SCHEMA_VERSION:
            raise CensusFormatError("unsupported universal census record schema_version")
        try:
            status = UniversalCensusStatus(_require_string(value["status"], "status"))
        except ValueError as exc:
            raise CensusFormatError("invalid universal census status") from exc
        raw_partitions = value["partitions"]
        if isinstance(raw_partitions, str | bytes) or not isinstance(raw_partitions, Sequence):
            raise CensusFormatError("partitions must be an array")
        partitions: list[UniversalPartitionResult] = []
        for index, raw_partition in enumerate(raw_partitions):
            if not isinstance(raw_partition, Mapping):
                raise CensusFormatError(f"partitions[{index}] must be an object")
            partitions.append(
                UniversalPartitionResult.from_mapping(cast(Mapping[str, object], raw_partition))
            )
        try:
            return cls(
                run_fingerprint=_require_string(value["run_fingerprint"], "run_fingerprint"),
                index=_require_nonnegative_int(value["index"], name="index"),
                graph6=_require_string(value["graph6"], "graph6"),
                graph_fingerprint=_require_string(value["graph_fingerprint"], "graph_fingerprint"),
                order=_require_nonnegative_int(value["order"], name="order"),
                size=_require_nonnegative_int(value["size"], name="size"),
                min_degree=_require_nonnegative_int(value["min_degree"], name="min_degree"),
                max_degree=_require_nonnegative_int(value["max_degree"], name="max_degree"),
                degree_parameter=_require_nonnegative_int(
                    value["degree_parameter"], name="degree_parameter"
                ),
                eligible=value["eligible"],  # type: ignore[arg-type]
                status=status,
                outcome_code=_require_string(value["outcome_code"], "outcome_code"),
                detail=_require_string(value["detail"], "detail"),
                partition_count=_require_nonnegative_int(
                    value["partition_count"], name="partition_count"
                ),
                partitions=tuple(partitions),
            )
        except (GraphFormatError, ValueError) as exc:
            raise CensusFormatError(f"invalid universal census record: {exc}") from exc

    @classmethod
    def from_json(cls, data: str | bytes) -> UniversalCensusRecord:
        try:
            value = strict_json_loads(data)
        except GraphFormatError as exc:
            raise CensusFormatError(str(exc)) from exc
        if not isinstance(value, Mapping):
            raise CensusFormatError("universal census record must be a JSON object")
        return cls.from_dict(cast(Mapping[str, object], value))


@dataclass(frozen=True, slots=True)
class UniversalCensusCounts:
    verified_all: int = 0
    candidate_unsat: int = 0
    unknown: int = 0
    error: int = 0
    skipped: int = 0

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            _require_nonnegative_int(value, name=name)

    @property
    def total(self) -> int:
        return sum(self.to_dict().values())

    def increment(self, status: UniversalCensusStatus) -> UniversalCensusCounts:
        values = self.to_dict()
        values[status.value] += 1
        return UniversalCensusCounts(**values)

    def to_dict(self) -> dict[str, int]:
        return {
            "candidate_unsat": self.candidate_unsat,
            "error": self.error,
            "skipped": self.skipped,
            "unknown": self.unknown,
            "verified_all": self.verified_all,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UniversalCensusCounts:
        expected = {status.value for status in UniversalCensusStatus}
        _require_exact_keys(value, expected, name="universal counts")
        return cls(
            **{
                key: _require_nonnegative_int(raw, name=f"counts.{key}")
                for key, raw in value.items()
            }
        )


@dataclass(frozen=True, slots=True)
class UniversalCensusRunResult:
    run_fingerprint: str
    record_count: int
    partition_count: int
    counts: UniversalCensusCounts
    resumed_records: int
    records_path: Path
    manifest_path: Path
    completion_path: Path

    def __post_init__(self) -> None:
        _require_digest(self.run_fingerprint, name="run_fingerprint")
        _require_nonnegative_int(self.record_count, name="record_count")
        _require_nonnegative_int(self.partition_count, name="partition_count")
        _require_nonnegative_int(self.resumed_records, name="resumed_records")
        if self.record_count != self.counts.total:
            raise ValueError("record_count must equal status counts")
        if self.resumed_records > self.record_count:
            raise ValueError("resumed_records cannot exceed record_count")


@dataclass(frozen=True, slots=True)
class _RunIdentity:
    fingerprint: str
    descriptor: dict[str, object]


def _partition_dict(partition: EquitablePartition) -> dict[str, object]:
    return {
        "pairs": [list(edge) for edge in partition.pairs],
        "singletons": list(partition.singletons),
    }


def _valid_identifier(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return (
        value[0].islower()
        and value[0].isascii()
        and all(
            (character.islower() and character.isascii()) or character.isdigit() or character == "_"
            for character in value
        )
    )


def _parse_nonnegative_array(value: object, name: str) -> tuple[int, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise CensusFormatError(f"{name} must be an array")
    return tuple(
        _require_nonnegative_int(item, name=f"{name}[{index}]") for index, item in enumerate(value)
    )


def _parse_edges(value: object, name: str) -> tuple[Edge, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise CensusFormatError(f"{name} must be an array")
    edges: list[Edge] = []
    for index, raw_edge in enumerate(value):
        if isinstance(raw_edge, str | bytes) or not isinstance(raw_edge, Sequence):
            raise CensusFormatError(f"{name}[{index}] must be a two-item array")
        if len(raw_edge) != 2:
            raise CensusFormatError(f"{name}[{index}] must have two endpoints")
        edges.append(
            (
                _require_nonnegative_int(raw_edge[0], name=f"{name}[{index}][0]"),
                _require_nonnegative_int(raw_edge[1], name=f"{name}[{index}][1]"),
            )
        )
    return tuple(edges)


def _freeze_edges(value: object, name: str) -> tuple[Edge, ...]:
    try:
        edges = _parse_edges(tuple(value), name)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{name} must be iterable") from exc
    if any(left >= right for left, right in edges):
        raise ValueError(f"{name} endpoints must satisfy left < right")
    if edges != tuple(sorted(set(edges))):
        raise ValueError(f"{name} must be unique and lexicographically ordered")
    return edges


def _freeze_sorted_integers(value: object, name: str) -> tuple[int, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array-like sequence")
    integers: tuple[object, ...] = tuple(value)
    checked = tuple(
        _require_nonnegative_int(item, name=f"{name}[{index}]")
        for index, item in enumerate(integers)
    )
    if checked != tuple(sorted(set(checked))):
        raise ValueError(f"{name} must be unique and sorted")
    return checked


def _aggregate_status(
    statuses: Sequence[SolveStatus], disagreement: bool
) -> tuple[UniversalCensusStatus, str]:
    if disagreement:
        return UniversalCensusStatus.ERROR, "backend_status_disagreement"
    if any(status is SolveStatus.ERROR for status in statuses):
        return UniversalCensusStatus.ERROR, "partition_check_error"
    if any(status is SolveStatus.UNKNOWN for status in statuses):
        return UniversalCensusStatus.UNKNOWN, "incomplete_partition_check"
    if any(status is SolveStatus.CANDIDATE_UNSAT for status in statuses):
        return UniversalCensusStatus.CANDIDATE_UNSAT, "candidate_partition_nonextension"
    if statuses and all(status is SolveStatus.WITNESS for status in statuses):
        return UniversalCensusStatus.VERIFIED_ALL, "verified_all_partitions"
    return UniversalCensusStatus.ERROR, "empty_check_transcript"


def _build_run_identity(
    config: UniversalCensusConfig,
    generator: GengIdentity,
    toolkit: ToolkitIdentity,
) -> _RunIdentity:
    if generator.arguments != config.geng.arguments():
        raise ValueError("generator identity arguments do not match config")
    shard_index = config.geng.shard_index if config.geng.shard_index is not None else 0
    shard_count = config.geng.shard_count if config.geng.shard_count is not None else 1
    descriptor: dict[str, object] = {
        "config": config.to_dict(),
        "generator": _generator_dict(generator),
        "objective": UNIVERSAL_OBJECTIVE,
        "shard": {"count": shard_count, "index": shard_index},
        "toolkit": toolkit.to_dict(),
    }
    return _RunIdentity(sha256_hex(descriptor), descriptor)


def _empty_record(
    *,
    run_fingerprint: str,
    index: int,
    graph: SimpleGraph,
    status: UniversalCensusStatus,
    outcome_code: str,
    detail: str,
) -> UniversalCensusRecord:
    return UniversalCensusRecord(
        run_fingerprint=run_fingerprint,
        index=index,
        graph6=encode_graph6(graph),
        graph_fingerprint=graph.fingerprint,
        order=graph.order,
        size=graph.size,
        min_degree=graph.min_degree,
        max_degree=graph.max_degree,
        degree_parameter=graph.max_degree + 1,
        eligible=False,
        status=status,
        outcome_code=outcome_code,
        detail=detail,
        partition_count=0,
        partitions=(),
    )


def _solve_one_check(
    graph: SimpleGraph,
    partition: EquitablePartition,
    construction_graph: SimpleGraph,
    distinguished_edges: tuple[Edge, ...],
    spec: UniversalCheckSpec,
    limits: SearchLimits,
) -> UniversalCheckResult:
    degree_parameter = graph.max_degree + 1
    color_count = degree_parameter + spec.palette_offset
    construction = construct_auxiliary_graph(graph, partition)
    if (
        construction.graph != construction_graph
        or construction.distinguished_edges != distinguished_edges
    ):
        raise CensusError("auxiliary construction changed during one partition")
    problem = auxiliary_coloring_problem(construction, color_count)
    try:
        solved = solve_with_backend(problem, backend=spec.backend, limits=limits)
        if not isinstance(solved, SolveResult):
            raise TypeError("backend did not return SolveResult")
        if not isinstance(solved.status, SolveStatus):
            raise TypeError("backend returned an invalid solve status")
        if not isinstance(solved.detail, str) or not solved.detail:
            raise TypeError("backend returned an invalid detail string")
        deterministic_stats = DeterministicSearchStats(
            solved.stats.nodes,
            solved.stats.backtracks,
        )
    except Exception as exc:  # every configured check remains explicitly accounted
        return UniversalCheckResult(
            backend=spec.backend,
            palette_offset=spec.palette_offset,
            color_count=color_count,
            problem_digest=problem.semantic_digest,
            status=SolveStatus.ERROR,
            stats=DeterministicSearchStats(0, 0),
            detail=f"{type(exc).__name__}: {exc}",
            auxiliary_edge_colors=None,
        )
    status = solved.status
    assignment = solved.assignment
    detail = solved.detail
    if solved.problem_digest != problem.semantic_digest:
        status = SolveStatus.ERROR
        assignment = None
        detail = "solver returned a foreign problem digest"
    elif status is SolveStatus.WITNESS:
        if assignment is None:
            status = SolveStatus.ERROR
            detail = "solver reported a witness without an assignment"
        else:
            try:
                verify_edge_coloring(
                    construction.graph,
                    color_count,
                    assignment,
                    distinguished_edges=construction.distinguished_edges,
                ).require_valid()
                decode_auxiliary_coloring(construction, color_count, assignment)
            except ValueError as exc:
                status = SolveStatus.ERROR
                assignment = None
                detail = f"independent witness verification failed: {exc}"
    elif assignment is not None:
        status = SolveStatus.ERROR
        assignment = None
        detail = "nonwitness solver result carried an assignment"
    return UniversalCheckResult(
        backend=spec.backend,
        palette_offset=spec.palette_offset,
        color_count=color_count,
        problem_digest=problem.semantic_digest,
        status=status,
        stats=deterministic_stats,
        detail=detail,
        auxiliary_edge_colors=assignment,
    )


def _process_graph(
    *,
    config: UniversalCensusConfig,
    run_fingerprint: str,
    index: int,
    graph: SimpleGraph,
) -> UniversalCensusRecord:
    if graph.order != config.geng.order:
        return _empty_record(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            status=UniversalCensusStatus.ERROR,
            outcome_code="generator_order_mismatch",
            detail=f"generator produced order {graph.order}; expected {config.geng.order}",
        )
    if config.require_high_degree and 2 * graph.max_degree < graph.order:
        return _empty_record(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            status=UniversalCensusStatus.SKIPPED,
            outcome_code="outside_high_degree_filter",
            detail="graph does not satisfy 2*Delta(G) >= |V(G)|",
        )
    degree_parameter = graph.max_degree + 1
    if graph.order == 0 or not degree_parameter <= graph.order <= 2 * degree_parameter:
        return _empty_record(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            status=UniversalCensusStatus.SKIPPED,
            outcome_code="outside_auxiliary_regime",
            detail="equitable classes are not restricted to sizes one and two",
        )
    try:
        canonical_partitions = tuple(iter_equitable_partitions(graph))
        if not canonical_partitions:
            raise CensusError("partition enumeration produced no equitable partitions")
        partition_results: list[UniversalPartitionResult] = []
        for partition_index, partition in enumerate(canonical_partitions):
            construction = construct_auxiliary_graph(graph, partition)
            checks = tuple(
                _solve_one_check(
                    graph,
                    partition,
                    construction.graph,
                    construction.distinguished_edges,
                    spec,
                    config.limits_per_check,
                )
                for spec in config.checks
            )
            partition_results.append(
                UniversalPartitionResult(
                    index=partition_index,
                    partition_fingerprint=sha256_hex(_partition_dict(partition)),
                    pairs=partition.pairs,
                    singletons=partition.singletons,
                    auxiliary_graph6=encode_graph6(construction.graph),
                    auxiliary_graph_fingerprint=construction.graph.fingerprint,
                    distinguished_edges=construction.distinguished_edges,
                    checks=checks,
                )
            )
        disagreement = any(
            len({check.status for check in partition.checks if check.palette_offset == 1}) > 1
            for partition in partition_results
        )
        statuses = [check.status for partition in partition_results for check in partition.checks]
        status, outcome_code = _aggregate_status(statuses, disagreement)
        detail = {
            UniversalCensusStatus.VERIFIED_ALL: (
                "every configured check on every canonical equitable partition produced an "
                "independently replayed witness"
            ),
            UniversalCensusStatus.CANDIDATE_UNSAT: (
                "at least one search exhausted without a witness; no independent UNSAT proof "
                "is attached"
            ),
            UniversalCensusStatus.UNKNOWN: "at least one configured check was incomplete",
            UniversalCensusStatus.ERROR: (
                "cross-backend D+1 statuses disagree"
                if disagreement
                else "at least one configured check failed"
            ),
            UniversalCensusStatus.SKIPPED: "unreachable",
        }[status]
        return UniversalCensusRecord(
            run_fingerprint=run_fingerprint,
            index=index,
            graph6=encode_graph6(graph),
            graph_fingerprint=graph.fingerprint,
            order=graph.order,
            size=graph.size,
            min_degree=graph.min_degree,
            max_degree=graph.max_degree,
            degree_parameter=degree_parameter,
            eligible=True,
            status=status,
            outcome_code=outcome_code,
            detail=detail,
            partition_count=len(partition_results),
            partitions=tuple(partition_results),
        )
    except Exception:
        # A construction/enumeration failure is an orchestration failure, not
        # evidence that an in-scope graph became ineligible.  Leave the prior
        # graph-level checkpoint intact and withhold completion so resume can
        # retry under repaired software.  Backend/check exceptions are already
        # represented above as complete ERROR checks inside a full transcript.
        raise


def _scan_partial(
    path: Path,
    *,
    run_fingerprint: str,
    config: UniversalCensusConfig,
) -> tuple[int, int, UniversalCensusCounts]:
    if path.is_symlink():
        raise CensusFormatError("checkpoint path must not be a symbolic link")
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.fsync(descriptor)
        os.close(descriptor)
        _fsync_directory(path.parent)
        return 0, 0, UniversalCensusCounts()
    if not path.is_file():
        raise CensusFormatError("checkpoint path must be a regular file")
    count = 0
    partition_count = 0
    counts = UniversalCensusCounts()
    with path.open("r+b") as stream:
        line_start = 0
        while True:
            raw = _read_record_line(stream, count)
            if not raw:
                break
            if not raw.endswith(b"\n"):
                stream.truncate(line_start)
                stream.flush()
                os.fsync(stream.fileno())
                break
            try:
                record = UniversalCensusRecord.from_json(raw[:-1])
            except CensusFormatError as exc:
                raise CensusFormatError(f"invalid checkpoint record {count}: {exc}") from exc
            if record.to_json().encode("utf-8") + b"\n" != raw:
                raise CensusFormatError(f"checkpoint record {count} is not canonical JSON")
            if record.run_fingerprint != run_fingerprint:
                raise CensusResumeError("checkpoint belongs to a different run configuration")
            if record.index != count:
                raise CensusResumeError("checkpoint index is discontinuous")
            try:
                record.require_valid_for_config(config)
            except ValueError as exc:
                raise CensusFormatError(
                    f"checkpoint record {count} violates run classification: {exc}"
                ) from exc
            counts = counts.increment(record.status)
            partition_count += record.partition_count
            count += 1
            line_start = stream.tell()
    return count, partition_count, counts


def _iter_checkpoint_records(
    path: Path,
    count: int,
    config: UniversalCensusConfig,
) -> Iterator[UniversalCensusRecord]:
    with path.open("rb") as stream:
        for index in range(count):
            raw = _read_record_line(stream, index)
            if not raw.endswith(b"\n"):
                raise CensusResumeError(f"checkpoint record {index} disappeared during resume")
            record = UniversalCensusRecord.from_json(raw[:-1])
            try:
                record.require_valid_for_config(config)
            except ValueError as exc:
                raise CensusResumeError(
                    f"checkpoint record {index} violates run classification: {exc}"
                ) from exc
            yield record


def _read_record_line(stream: BinaryIO, index: int) -> bytes:
    raw = stream.readline(MAX_UNIVERSAL_RECORD_BYTES + 1)
    if len(raw) > MAX_UNIVERSAL_RECORD_BYTES:
        raise CensusFormatError(
            f"universal census record {index} exceeds {MAX_UNIVERSAL_RECORD_BYTES} bytes"
        )
    return raw


def _recover_interrupted_publication(directory: Path) -> None:
    records = directory / _RECORDS_NAME
    partial = directory / _PARTIAL_NAME
    completion = directory / _COMPLETION_NAME
    if completion.exists():
        return
    if records.exists():
        if records.is_symlink() or not records.is_file():
            raise CensusFormatError("records path must be a regular non-symlink file")
        if partial.exists():
            raise CensusFormatError("both completed and partial records exist without completion")
        os.replace(records, partial)
        _fsync_directory(directory)


def _manifest_dict(
    *,
    run: _RunIdentity,
    record_count: int,
    partition_count: int,
    counts: UniversalCensusCounts,
    records_sha256: str,
    records_bytes: int,
) -> dict[str, object]:
    return {
        "artifacts": {
            "records_bytes": records_bytes,
            "records_path": _RECORDS_NAME,
            "records_sha256": records_sha256,
        },
        "complete": True,
        "counts": counts.to_dict(),
        "partition_count": partition_count,
        "provenance": run.descriptor,
        "record_count": record_count,
        "run_fingerprint": run.fingerprint,
        "schema_version": UNIVERSAL_MANIFEST_SCHEMA_VERSION,
    }


def _completion_dict(
    *,
    run_fingerprint: str,
    manifest_sha256: str,
    records_sha256: str,
    record_count: int,
) -> dict[str, object]:
    return {
        "manifest_sha256": manifest_sha256,
        "record_count": record_count,
        "records_sha256": records_sha256,
        "run_fingerprint": run_fingerprint,
        "schema_version": UNIVERSAL_COMPLETION_SCHEMA_VERSION,
    }


def _validate_completed_run(
    directory: Path,
    run: _RunIdentity,
    config: UniversalCensusConfig,
    *,
    executable: str,
) -> UniversalCensusRunResult:
    records_path = directory / _RECORDS_NAME
    manifest_path = directory / _MANIFEST_NAME
    completion_path = directory / _COMPLETION_NAME
    partial_path = directory / _PARTIAL_NAME
    if partial_path.exists() or partial_path.is_symlink():
        raise CensusFormatError(
            "completed universal census must not coexist with a partial record stream"
        )
    for path in (records_path, manifest_path, completion_path):
        if path.is_symlink() or not path.is_file():
            raise CensusFormatError(
                f"completed universal census requires a regular non-symlink {path.name}"
            )
    manifest = _load_canonical_json(manifest_path)
    completion = _load_canonical_json(completion_path)
    _require_exact_keys(
        manifest,
        {
            "artifacts",
            "complete",
            "counts",
            "partition_count",
            "provenance",
            "record_count",
            "run_fingerprint",
            "schema_version",
        },
        name="universal manifest",
    )
    _require_exact_keys(
        completion,
        {
            "manifest_sha256",
            "record_count",
            "records_sha256",
            "run_fingerprint",
            "schema_version",
        },
        name="universal completion",
    )
    if (
        manifest["schema_version"] != UNIVERSAL_MANIFEST_SCHEMA_VERSION
        or manifest["complete"] is not True
    ):
        raise CensusFormatError("invalid universal manifest version or completion state")
    if completion["schema_version"] != UNIVERSAL_COMPLETION_SCHEMA_VERSION:
        raise CensusFormatError("invalid universal completion schema_version")
    if manifest["run_fingerprint"] != run.fingerprint:
        raise CensusResumeError("completed census belongs to a different run configuration")
    if canonical_json_bytes(manifest["provenance"]) != canonical_json_bytes(run.descriptor):
        raise CensusFormatError("manifest provenance does not match run fingerprint")
    records_digest, records_bytes = _artifact_digest(records_path)
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    artifacts = manifest["artifacts"]
    counts_value = manifest["counts"]
    if not isinstance(artifacts, Mapping) or not isinstance(counts_value, Mapping):
        raise CensusFormatError("manifest artifacts and counts must be objects")
    expected_artifacts = {
        "records_bytes": records_bytes,
        "records_path": _RECORDS_NAME,
        "records_sha256": records_digest,
    }
    if canonical_json_bytes(artifacts) != canonical_json_bytes(expected_artifacts):
        raise CensusFormatError("record artifact size or digest does not match manifest")
    record_count = _require_nonnegative_int(manifest["record_count"], name="record_count")
    expected_completion = _completion_dict(
        run_fingerprint=run.fingerprint,
        manifest_sha256=manifest_digest,
        records_sha256=records_digest,
        record_count=record_count,
    )
    if canonical_json_bytes(completion) != canonical_json_bytes(expected_completion):
        raise CensusFormatError("completion marker does not match manifest and records")
    counts = UniversalCensusCounts.from_mapping(cast(Mapping[str, object], counts_value))
    partition_count = _require_nonnegative_int(manifest["partition_count"], name="partition_count")
    scanned_count = 0
    scanned_partition_count = 0
    scanned_counts = UniversalCensusCounts()
    regenerated = iter(stream_geng(config.geng, executable=executable))
    regenerated_graphs: set[str] = set()
    with records_path.open("rb") as stream:
        while True:
            raw = _read_record_line(stream, scanned_count)
            if not raw:
                break
            if not raw.endswith(b"\n"):
                raise CensusFormatError("completed JSONL has an unterminated record")
            record = UniversalCensusRecord.from_json(raw[:-1])
            if record.index != scanned_count or record.run_fingerprint != run.fingerprint:
                raise CensusFormatError("completed JSONL has a discontinuous or foreign record")
            if record.to_json().encode("utf-8") + b"\n" != raw:
                raise CensusFormatError("completed JSONL is not canonical")
            try:
                record.require_valid_for_config(config)
            except ValueError as exc:
                raise CensusFormatError(
                    f"completed record {scanned_count} violates run classification: {exc}"
                ) from exc
            try:
                regenerated_graph = next(regenerated)
            except StopIteration as exc:
                raise CensusFormatError(
                    "configured generator ended before the completed record transcript"
                ) from exc
            if not isinstance(regenerated_graph, SimpleGraph):
                raise CensusFormatError("configured generator yielded a non-graph item")
            regenerated_graph6 = encode_graph6(regenerated_graph)
            if regenerated_graph6 in regenerated_graphs:
                raise CensusFormatError("configured generator yielded a duplicate graph6 record")
            regenerated_graphs.add(regenerated_graph6)
            if (
                record.graph6 != regenerated_graph6
                or record.graph_fingerprint != regenerated_graph.fingerprint
            ):
                raise CensusFormatError(
                    f"configured generator disagrees with completed record {scanned_count}"
                )
            scanned_counts = scanned_counts.increment(record.status)
            scanned_partition_count += record.partition_count
            scanned_count += 1
    try:
        next(regenerated)
    except StopIteration:
        pass
    else:
        raise CensusFormatError("configured generator has an extra graph after the transcript")
    if (
        scanned_count != record_count
        or scanned_partition_count != partition_count
        or scanned_counts != counts
        or counts.total != record_count
    ):
        raise CensusFormatError("manifest counts do not match completed JSONL")
    return UniversalCensusRunResult(
        run_fingerprint=run.fingerprint,
        record_count=record_count,
        partition_count=partition_count,
        counts=counts,
        resumed_records=record_count,
        records_path=records_path,
        manifest_path=manifest_path,
        completion_path=completion_path,
    )


def run_universal_census(
    config: UniversalCensusConfig,
    output_directory: str | Path,
    *,
    executable: str = "geng",
    toolkit_identity: ToolkitIdentity | None = None,
) -> UniversalCensusRunResult:
    """Run or resume a complete all-partition transcript for one ``geng`` shard."""

    if not isinstance(config, UniversalCensusConfig):
        raise ValueError("config must be UniversalCensusConfig")
    directory = Path(output_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    resolved_executable = str(resolve_geng(executable))
    generator = geng_identity(config.geng, executable=resolved_executable)
    toolkit_was_detected = toolkit_identity is None
    toolkit = toolkit_identity or detect_toolkit_identity()
    run = _build_run_identity(config, generator, toolkit)
    with _exclusive_output_lock(directory):
        completion_path = directory / _COMPLETION_NAME
        if completion_path.exists():
            result = _validate_completed_run(
                directory,
                run,
                config,
                executable=resolved_executable,
            )
            if geng_identity(config.geng, executable=resolved_executable) != generator:
                raise CensusError(
                    "geng executable identity changed during completed-run validation"
                )
            if toolkit_was_detected and detect_toolkit_identity() != toolkit:
                raise CensusError("toolkit source identity changed during completed-run validation")
            return result
        _recover_interrupted_publication(directory)
        partial_path = directory / _PARTIAL_NAME
        resumed_records, partition_count, counts = _scan_partial(
            partial_path,
            run_fingerprint=run.fingerprint,
            config=config,
        )
        checkpoint_iterator = iter(
            _iter_checkpoint_records(
                partial_path,
                resumed_records,
                config,
            )
        )
        append_descriptor = os.open(partial_path, os.O_WRONLY | os.O_APPEND)
        processed = resumed_records
        since_sync = 0
        generated_graphs: set[str] = set()
        try:
            for index, graph in enumerate(stream_geng(config.geng, executable=resolved_executable)):
                if not isinstance(graph, SimpleGraph):
                    raise CensusError(f"generator item {index} is not a SimpleGraph")
                graph6 = encode_graph6(graph)
                if graph6 in generated_graphs:
                    raise CensusError(f"generator yielded duplicate graph6 at index {index}")
                generated_graphs.add(graph6)
                if index < resumed_records:
                    checkpoint = next(checkpoint_iterator)
                    if (
                        checkpoint.graph_fingerprint != graph.fingerprint
                        or checkpoint.graph6 != graph6
                    ):
                        raise CensusResumeError(
                            f"regenerated graph stream does not match checkpoint at index {index}"
                        )
                    continue
                record = _process_graph(
                    config=config,
                    run_fingerprint=run.fingerprint,
                    index=index,
                    graph=graph,
                )
                record.require_valid_for_config(config)
                _write_all(append_descriptor, canonical_json_bytes(record.to_dict()) + b"\n")
                counts = counts.increment(record.status)
                partition_count += record.partition_count
                processed += 1
                since_sync += 1
                if since_sync >= config.checkpoint_interval:
                    os.fsync(append_descriptor)
                    since_sync = 0
        finally:
            os.fsync(append_descriptor)
            os.close(append_descriptor)
        try:
            next(checkpoint_iterator)
        except StopIteration:
            pass
        else:
            raise CensusResumeError("generator ended before the checkpoint prefix")
        if processed != counts.total:
            raise CensusError("internal graph accounting invariant failed")
        if geng_identity(config.geng, executable=resolved_executable) != generator:
            raise CensusError("geng executable identity changed while the census was running")
        if toolkit_was_detected and detect_toolkit_identity() != toolkit:
            raise CensusError("toolkit source identity changed while the census was running")
        records_sha256, records_bytes = _artifact_digest(partial_path)
        records_path = directory / _RECORDS_NAME
        os.replace(partial_path, records_path)
        _fsync_directory(directory)
        manifest = _manifest_dict(
            run=run,
            record_count=processed,
            partition_count=partition_count,
            counts=counts,
            records_sha256=records_sha256,
            records_bytes=records_bytes,
        )
        manifest_path = directory / _MANIFEST_NAME
        _atomic_write(manifest_path, canonical_json_bytes(manifest) + b"\n")
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        completion = _completion_dict(
            run_fingerprint=run.fingerprint,
            manifest_sha256=manifest_sha256,
            records_sha256=records_sha256,
            record_count=processed,
        )
        _atomic_write(completion_path, canonical_json_bytes(completion) + b"\n")
        return UniversalCensusRunResult(
            run_fingerprint=run.fingerprint,
            record_count=processed,
            partition_count=partition_count,
            counts=counts,
            resumed_records=resumed_records,
            records_path=records_path,
            manifest_path=manifest_path,
            completion_path=completion_path,
        )


__all__ = [
    "DEFAULT_UNIVERSAL_CHECKS",
    "MAX_UNIVERSAL_RECORD_BYTES",
    "PARTITION_ENUMERATOR_ID",
    "UNIVERSAL_COMPLETION_SCHEMA_VERSION",
    "UNIVERSAL_MANIFEST_SCHEMA_VERSION",
    "UNIVERSAL_OBJECTIVE",
    "UNIVERSAL_RECORD_SCHEMA_VERSION",
    "DeterministicSearchStats",
    "UniversalCensusConfig",
    "UniversalCensusCounts",
    "UniversalCensusRecord",
    "UniversalCensusRunResult",
    "UniversalCensusStatus",
    "UniversalCheckResult",
    "UniversalCheckSpec",
    "UniversalPartitionResult",
    "count_equitable_partitions_dp",
    "run_universal_census",
]
