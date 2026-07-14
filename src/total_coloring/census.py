"""Deterministic, resumable orchestration for auxiliary-coloring censuses.

The JSONL prefix is the checkpoint.  On resume, every checkpointed record is
parsed strictly and matched against a freshly generated graph stream before it
is skipped.  A run is published only after the generator is fully consumed:
``records.jsonl`` first, then its manifest, and finally ``completion.json``.
Each promotion uses fsync plus atomic replacement in the destination directory.
"""

from __future__ import annotations

import hashlib
import math
import os
import platform
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from string import ascii_lowercase, digits
from typing import Final, cast

from total_coloring import __version__
from total_coloring.auxiliary import (
    AuxiliarySearchResult,
    AuxiliaryWitness,
    EquitablePartition,
    construct_auxiliary_graph,
    decode_auxiliary_coloring,
    search_auxiliary_extensions,
)
from total_coloring.certificates import TotalColoringCertificate, verify_total_coloring
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
from total_coloring.solver import SearchLimits, SolveStatus

CENSUS_RECORD_SCHEMA_VERSION: Final = "total-coloring.census-record.v1"
CENSUS_MANIFEST_SCHEMA_VERSION: Final = "total-coloring.census-manifest.v1"
CENSUS_COMPLETION_SCHEMA_VERSION: Final = "total-coloring.census-completion.v1"
CENSUS_BACKEND_ID: Final = "dsatur-iterative-v1"
AUXILIARY_WITNESS_SCHEMA_VERSION: Final = "total-coloring.census-auxiliary-witness.v1"

_RECORDS_NAME: Final = "records.jsonl"
_PARTIAL_NAME: Final = ".records.jsonl.partial"
_MANIFEST_NAME: Final = "manifest.json"
_COMPLETION_NAME: Final = "completion.json"
_LOCK_NAME: Final = ".census.lock"
_HEX_DIGITS: Final = frozenset("0123456789abcdef")


class CensusError(RuntimeError):
    """Base class for census orchestration failures."""


class CensusFormatError(CensusError):
    """Raised when a census artifact is malformed or noncanonical."""


class CensusResumeError(CensusError):
    """Raised when a checkpoint is incompatible with the regenerated stream."""


class CensusLockError(CensusError):
    """Raised when another process may be writing the same output directory."""


class CensusStatus(StrEnum):
    """Mutually exclusive terminal classification for one input graph."""

    WITNESS = "witness"
    CANDIDATE_UNSAT = "candidate_unsat"
    UNKNOWN = "unknown"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ToolkitIdentity:
    """Exact identity of the Python implementation executing a census."""

    distribution_version: str
    source_sha256: str
    python_implementation: str
    python_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.distribution_version, str) or not self.distribution_version:
            raise ValueError("distribution_version must be nonempty")
        _require_digest(self.source_sha256, name="source_sha256")
        if not isinstance(self.python_implementation, str) or not self.python_implementation:
            raise ValueError("python_implementation must be nonempty")
        if not isinstance(self.python_version, str) or not self.python_version:
            raise ValueError("python_version must be nonempty")

    def to_dict(self) -> dict[str, object]:
        return {
            "distribution_version": self.distribution_version,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "source_sha256": self.source_sha256,
        }


def detect_toolkit_identity() -> ToolkitIdentity:
    """Hash the installed package sources and record the interpreter identity."""

    package_root = Path(__file__).resolve().parent
    members = tuple(
        path
        for path in sorted(package_root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed")
    )
    inventory = [
        {
            "path": path.relative_to(package_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for path in members
    ]
    return ToolkitIdentity(
        distribution_version=__version__,
        source_sha256=sha256_hex(inventory),
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
    )


@dataclass(frozen=True, slots=True)
class CensusConfig:
    """Complete scientific and operational configuration for one census shard."""

    geng: GengSpec
    color_offset_from_degree_parameter: int = 2
    require_high_degree: bool = True
    limits_per_partition: SearchLimits = field(default_factory=SearchLimits)
    max_partitions: int | None = None
    checkpoint_interval: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.geng, GengSpec):
            raise ValueError("geng must be a GengSpec")
        _require_nonnegative_int(
            self.color_offset_from_degree_parameter,
            name="color_offset_from_degree_parameter",
        )
        if not isinstance(self.require_high_degree, bool):
            raise ValueError("require_high_degree must be a boolean")
        if not isinstance(self.limits_per_partition, SearchLimits):
            raise ValueError("limits_per_partition must be SearchLimits")
        timeout = self.limits_per_partition.timeout_seconds
        if timeout is not None and not math.isfinite(timeout):
            raise ValueError("timeout_seconds must be finite")
        if self.max_partitions is not None:
            _require_positive_int(self.max_partitions, name="max_partitions")
        _require_positive_int(self.checkpoint_interval, name="checkpoint_interval")

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_interval": self.checkpoint_interval,
            "color_offset_from_degree_parameter": self.color_offset_from_degree_parameter,
            "filters": {"require_high_degree": self.require_high_degree},
            "generator_spec": {
                "connected": self.geng.connected,
                "max_degree": self.geng.max_degree,
                "min_degree": self.geng.min_degree,
                "order": self.geng.order,
                "shard_count": self.geng.shard_count,
                "shard_index": self.geng.shard_index,
            },
            "search": {
                "backend": CENSUS_BACKEND_ID,
                "max_nodes_per_partition": self.limits_per_partition.max_nodes,
                "max_partitions": self.max_partitions,
                "timeout_seconds_per_partition": self.limits_per_partition.timeout_seconds,
            },
        }


@dataclass(frozen=True, slots=True)
class CensusCounts:
    """Exact partition of processed input records by terminal status."""

    witness: int = 0
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

    def increment(self, status: CensusStatus) -> CensusCounts:
        values = self.to_dict()
        values[status.value] += 1
        return CensusCounts(**values)

    def to_dict(self) -> dict[str, int]:
        return {
            "candidate_unsat": self.candidate_unsat,
            "error": self.error,
            "skipped": self.skipped,
            "unknown": self.unknown,
            "witness": self.witness,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CensusCounts:
        expected = {status.value for status in CensusStatus}
        _require_exact_keys(value, expected, name="counts")
        checked = {
            key: _require_nonnegative_int(raw, name=f"counts.{key}") for key, raw in value.items()
        }
        return cls(**checked)


@dataclass(frozen=True, slots=True)
class CensusAuxiliaryWitness:
    """Self-contained auxiliary rainbow-edge-coloring witness.

    Redundant graph and distinguished-edge fields are intentional.  They make
    the artifact inspectable while verification reconstructs both from the
    partition and rejects any disagreement.
    """

    partition_pairs: tuple[Edge, ...]
    partition_singletons: tuple[int, ...]
    auxiliary_graph6: str
    distinguished_edges: tuple[Edge, ...]
    auxiliary_edge_colors: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_canonical_edges(self.partition_pairs, name="partition_pairs")
        _require_canonical_edges(self.distinguished_edges, name="distinguished_edges")
        if not isinstance(self.partition_singletons, tuple):
            raise ValueError("partition_singletons must be a tuple")
        checked_singletons = tuple(
            _require_nonnegative_int(value, name=f"partition_singletons[{index}]")
            for index, value in enumerate(self.partition_singletons)
        )
        if checked_singletons != tuple(sorted(set(checked_singletons))):
            raise ValueError("partition_singletons must be strictly increasing")
        if not isinstance(self.auxiliary_edge_colors, tuple):
            raise ValueError("auxiliary_edge_colors must be a tuple")
        for index, color in enumerate(self.auxiliary_edge_colors):
            _require_nonnegative_int(color, name=f"auxiliary_edge_colors[{index}]")
        auxiliary_graph = decode_graph6(self.auxiliary_graph6)
        if encode_graph6(auxiliary_graph) != self.auxiliary_graph6:
            raise ValueError("auxiliary_graph6 must be a canonical headerless record")

    @classmethod
    def from_search_witness(cls, witness: AuxiliaryWitness) -> CensusAuxiliaryWitness:
        """Copy the complete independently verifiable part of a search witness."""

        return cls(
            partition_pairs=witness.partition.pairs,
            partition_singletons=witness.partition.singletons,
            auxiliary_graph6=encode_graph6(witness.auxiliary_graph),
            distinguished_edges=witness.distinguished_edges,
            auxiliary_edge_colors=witness.auxiliary_edge_colors,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CensusAuxiliaryWitness:
        expected = {
            "auxiliary_edge_colors",
            "auxiliary_graph6",
            "distinguished_edges",
            "partition",
            "schema_version",
        }
        _require_exact_keys(value, expected, name="auxiliary witness")
        if value["schema_version"] != AUXILIARY_WITNESS_SCHEMA_VERSION:
            raise CensusFormatError("unsupported auxiliary witness schema_version")
        partition = value["partition"]
        if not isinstance(partition, Mapping):
            raise CensusFormatError("auxiliary witness partition must be an object")
        partition_mapping = cast(Mapping[str, object], partition)
        _require_exact_keys(
            partition_mapping,
            {"pairs", "singletons"},
            name="auxiliary witness partition",
        )
        try:
            return cls(
                partition_pairs=_parse_edges(partition_mapping["pairs"], name="partition.pairs"),
                partition_singletons=_parse_nonnegative_integers(
                    partition_mapping["singletons"],
                    name="partition.singletons",
                ),
                auxiliary_graph6=_require_string(value["auxiliary_graph6"], "auxiliary_graph6"),
                distinguished_edges=_parse_edges(
                    value["distinguished_edges"],
                    name="distinguished_edges",
                ),
                auxiliary_edge_colors=_parse_nonnegative_integers(
                    value["auxiliary_edge_colors"],
                    name="auxiliary_edge_colors",
                ),
            )
        except (GraphFormatError, ValueError) as exc:
            raise CensusFormatError(f"invalid auxiliary witness: {exc}") from exc

    def require_valid_for(
        self,
        original_graph: SimpleGraph,
        color_count: int,
        total_certificate: TotalColoringCertificate,
    ) -> None:
        """Reconstruct, verify, and bind this witness to its decoded certificate."""

        partition = EquitablePartition.from_complement_matching(
            original_graph,
            self.partition_pairs,
        )
        if partition.singletons != self.partition_singletons:
            raise ValueError("stored singleton classes do not match the complement matching")
        construction = construct_auxiliary_graph(original_graph, partition)
        if encode_graph6(construction.graph) != self.auxiliary_graph6:
            raise ValueError("stored auxiliary graph does not match the partition reconstruction")
        if construction.distinguished_edges != self.distinguished_edges:
            raise ValueError("stored distinguished edges do not match the partition reconstruction")
        verify_edge_coloring(
            construction.graph,
            color_count,
            self.auxiliary_edge_colors,
            distinguished_edges=construction.distinguished_edges,
        ).require_valid()
        decoded = decode_auxiliary_coloring(
            construction,
            color_count,
            self.auxiliary_edge_colors,
        )
        if decoded != total_certificate:
            raise ValueError("decoded auxiliary coloring does not equal the total certificate")

    def to_dict(self) -> dict[str, object]:
        return {
            "auxiliary_edge_colors": list(self.auxiliary_edge_colors),
            "auxiliary_graph6": self.auxiliary_graph6,
            "distinguished_edges": [list(edge) for edge in self.distinguished_edges],
            "partition": {
                "pairs": [list(edge) for edge in self.partition_pairs],
                "singletons": list(self.partition_singletons),
            },
            "schema_version": AUXILIARY_WITNESS_SCHEMA_VERSION,
        }


@dataclass(frozen=True, slots=True)
class CensusRecord:
    """One canonical result record, including a semantic witness when present."""

    run_fingerprint: str
    index: int
    graph6: str
    graph_fingerprint: str
    order: int
    size: int
    min_degree: int
    max_degree: int
    degree_parameter: int
    color_count: int
    status: CensusStatus
    outcome_code: str
    detail: str
    partitions_started: int
    partitions_completed: int
    candidate_failures: int
    unknown_partitions: int
    certificate: TotalColoringCertificate | None = None
    auxiliary_witness: CensusAuxiliaryWitness | None = None

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
            "color_count",
            "partitions_started",
            "partitions_completed",
            "candidate_failures",
            "unknown_partitions",
        ):
            _require_nonnegative_int(getattr(self, name), name=name)
        if not isinstance(self.status, CensusStatus):
            raise ValueError("status must be a CensusStatus")
        if not self.outcome_code or not _is_identifier(self.outcome_code):
            raise ValueError("outcome_code must be a nonempty lowercase identifier")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("detail must be a nonempty string")
        if self.degree_parameter != self.max_degree + 1:
            raise ValueError("degree_parameter must equal max_degree + 1")
        if self.partitions_completed > self.partitions_started:
            raise ValueError("partitions_completed cannot exceed partitions_started")
        if self.candidate_failures > self.partitions_completed:
            raise ValueError("candidate_failures cannot exceed partitions_completed")
        if self.unknown_partitions > self.partitions_started:
            raise ValueError("unknown_partitions cannot exceed partitions_started")
        carries_complete_witness = (
            self.certificate is not None and self.auxiliary_witness is not None
        )
        if (self.status is CensusStatus.WITNESS) != carries_complete_witness:
            raise ValueError(
                "exactly WITNESS records must carry both an auxiliary witness and certificate"
            )
        if (self.certificate is None) != (self.auxiliary_witness is None):
            raise ValueError("auxiliary witness and certificate must occur together")
        if self.status is CensusStatus.SKIPPED and any(
            (
                self.partitions_started,
                self.partitions_completed,
                self.candidate_failures,
                self.unknown_partitions,
            )
        ):
            raise ValueError("SKIPPED records cannot contain search counters")

        graph = decode_graph6(self.graph6)
        if encode_graph6(graph) != self.graph6:
            raise ValueError("graph6 must be a canonical headerless record")
        expected_metadata = (
            graph.fingerprint,
            graph.order,
            graph.size,
            graph.min_degree,
            graph.max_degree,
        )
        if expected_metadata != (
            self.graph_fingerprint,
            self.order,
            self.size,
            self.min_degree,
            self.max_degree,
        ):
            raise ValueError("graph metadata does not match graph6")
        if self.certificate is not None:
            assert self.auxiliary_witness is not None
            if self.certificate.palette_size != self.color_count:
                raise ValueError("certificate palette does not match color_count")
            verify_total_coloring(graph, self.certificate).require_valid()
            self.auxiliary_witness.require_valid_for(
                graph,
                self.color_count,
                self.certificate,
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CensusRecord:
        expected = {
            "auxiliary_witness",
            "candidate_failures",
            "certificate",
            "color_count",
            "degree_parameter",
            "detail",
            "graph6",
            "graph_fingerprint",
            "index",
            "max_degree",
            "min_degree",
            "order",
            "outcome_code",
            "partitions_completed",
            "partitions_started",
            "run_fingerprint",
            "schema_version",
            "size",
            "status",
            "unknown_partitions",
        }
        _require_exact_keys(value, expected, name="census record")
        if value["schema_version"] != CENSUS_RECORD_SCHEMA_VERSION:
            raise CensusFormatError("unsupported census record schema_version")
        try:
            status = CensusStatus(_require_string(value["status"], "status"))
        except (TypeError, ValueError) as exc:
            raise CensusFormatError("invalid census record status") from exc
        certificate_value = value["certificate"]
        if certificate_value is None:
            certificate = None
        elif isinstance(certificate_value, Mapping):
            try:
                certificate = TotalColoringCertificate.from_dict(
                    cast(Mapping[str, object], certificate_value)
                )
            except ValueError as exc:
                raise CensusFormatError(f"invalid embedded certificate: {exc}") from exc
        else:
            raise CensusFormatError("certificate must be an object or null")
        auxiliary_witness_value = value["auxiliary_witness"]
        if auxiliary_witness_value is None:
            auxiliary_witness = None
        elif isinstance(auxiliary_witness_value, Mapping):
            auxiliary_witness = CensusAuxiliaryWitness.from_dict(
                cast(Mapping[str, object], auxiliary_witness_value)
            )
        else:
            raise CensusFormatError("auxiliary_witness must be an object or null")

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
                color_count=_require_nonnegative_int(value["color_count"], name="color_count"),
                status=status,
                outcome_code=_require_string(value["outcome_code"], "outcome_code"),
                detail=_require_string(value["detail"], "detail"),
                partitions_started=_require_nonnegative_int(
                    value["partitions_started"], name="partitions_started"
                ),
                partitions_completed=_require_nonnegative_int(
                    value["partitions_completed"], name="partitions_completed"
                ),
                candidate_failures=_require_nonnegative_int(
                    value["candidate_failures"], name="candidate_failures"
                ),
                unknown_partitions=_require_nonnegative_int(
                    value["unknown_partitions"], name="unknown_partitions"
                ),
                certificate=certificate,
                auxiliary_witness=auxiliary_witness,
            )
        except (GraphFormatError, ValueError) as exc:
            raise CensusFormatError(f"invalid census record: {exc}") from exc

    @classmethod
    def from_json(cls, data: str | bytes) -> CensusRecord:
        try:
            value = strict_json_loads(data)
        except GraphFormatError as exc:
            raise CensusFormatError(str(exc)) from exc
        if not isinstance(value, Mapping):
            raise CensusFormatError("census record must be a JSON object")
        return cls.from_dict(cast(Mapping[str, object], value))

    def to_dict(self) -> dict[str, object]:
        return {
            "auxiliary_witness": (
                None if self.auxiliary_witness is None else self.auxiliary_witness.to_dict()
            ),
            "candidate_failures": self.candidate_failures,
            "certificate": None if self.certificate is None else self.certificate.to_dict(),
            "color_count": self.color_count,
            "degree_parameter": self.degree_parameter,
            "detail": self.detail,
            "graph6": self.graph6,
            "graph_fingerprint": self.graph_fingerprint,
            "index": self.index,
            "max_degree": self.max_degree,
            "min_degree": self.min_degree,
            "order": self.order,
            "outcome_code": self.outcome_code,
            "partitions_completed": self.partitions_completed,
            "partitions_started": self.partitions_started,
            "run_fingerprint": self.run_fingerprint,
            "schema_version": CENSUS_RECORD_SCHEMA_VERSION,
            "size": self.size,
            "status": self.status.value,
            "unknown_partitions": self.unknown_partitions,
        }

    def to_json(self) -> str:
        return canonical_json_bytes(self.to_dict()).decode("utf-8")


@dataclass(frozen=True, slots=True)
class CensusRunResult:
    """Paths and exact counts for a successfully completed census."""

    run_fingerprint: str
    record_count: int
    counts: CensusCounts
    resumed_records: int
    records_path: Path
    manifest_path: Path
    completion_path: Path

    def __post_init__(self) -> None:
        _require_digest(self.run_fingerprint, name="run_fingerprint")
        _require_nonnegative_int(self.record_count, name="record_count")
        _require_nonnegative_int(self.resumed_records, name="resumed_records")
        if self.record_count != self.counts.total:
            raise ValueError("record_count must equal the sum of status counts")
        if self.resumed_records > self.record_count:
            raise ValueError("resumed_records cannot exceed record_count")


@dataclass(frozen=True, slots=True)
class _RunIdentity:
    fingerprint: str
    descriptor: dict[str, object]


def _require_nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _require_positive_int(value: object, *, name: str) -> int:
    checked = _require_nonnegative_int(value, name=name)
    if checked == 0:
        raise ValueError(f"{name} must be positive")
    return checked


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise CensusFormatError(f"{name} must be a string")
    return value


def _require_digest(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError(f"{name} must be a 64-character lowercase SHA-256 digest")
    return value


def _parse_nonnegative_integers(value: object, *, name: str) -> tuple[int, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise CensusFormatError(f"{name} must be an array")
    return tuple(
        _require_nonnegative_int(item, name=f"{name}[{index}]") for index, item in enumerate(value)
    )


def _parse_edges(value: object, *, name: str) -> tuple[Edge, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise CensusFormatError(f"{name} must be an array")
    edges: list[Edge] = []
    for index, raw_edge in enumerate(value):
        if isinstance(raw_edge, str | bytes) or not isinstance(raw_edge, Sequence):
            raise CensusFormatError(f"{name}[{index}] must be a two-item array")
        if len(raw_edge) != 2:
            raise CensusFormatError(f"{name}[{index}] must contain exactly two endpoints")
        edges.append(
            (
                _require_nonnegative_int(raw_edge[0], name=f"{name}[{index}][0]"),
                _require_nonnegative_int(raw_edge[1], name=f"{name}[{index}][1]"),
            )
        )
    return tuple(edges)


def _require_canonical_edges(value: object, *, name: str) -> tuple[Edge, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    checked = _parse_edges(value, name=name)
    if any(left >= right for left, right in checked):
        raise ValueError(f"{name} endpoints must satisfy left < right")
    if checked != tuple(sorted(set(checked))):
        raise ValueError(f"{name} must be strictly lexicographically increasing")
    return checked


def _is_identifier(value: str) -> bool:
    alphabet = ascii_lowercase + digits + "_"
    return value[0] in ascii_lowercase and all(character in alphabet for character in value)


def _require_exact_keys(value: Mapping[str, object], expected: set[str], *, name: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing keys {missing}")
    if extra:
        details.append(f"unknown keys {extra}")
    raise CensusFormatError(f"invalid {name}: " + "; ".join(details))


def _generator_dict(identity: GengIdentity) -> dict[str, object]:
    _require_digest(identity.sha256, name="generator sha256")
    if (
        not isinstance(identity.executable, str)
        or not identity.executable
        or identity.executable in {".", ".."}
        or "/" in identity.executable
        or "\\" in identity.executable
        or any(ord(character) < 32 for character in identity.executable)
    ):
        raise ValueError("generator executable must be a portable basename")
    if not isinstance(identity.arguments, tuple) or not all(
        isinstance(argument, str) for argument in identity.arguments
    ):
        raise ValueError("generator arguments must be strings")
    return {
        "arguments": list(identity.arguments),
        "executable": identity.executable,
        "name": "nauty-geng",
        "sha256": identity.sha256,
    }


def _build_run_identity(
    config: CensusConfig,
    generator: GengIdentity,
    toolkit: ToolkitIdentity,
) -> _RunIdentity:
    if generator.arguments != config.geng.arguments():
        raise ValueError("generator identity arguments do not match CensusConfig.geng")
    shard_index = config.geng.shard_index if config.geng.shard_index is not None else 0
    shard_count = config.geng.shard_count if config.geng.shard_count is not None else 1
    descriptor: dict[str, object] = {
        "config": config.to_dict(),
        "generator": _generator_dict(generator),
        "shard": {"count": shard_count, "index": shard_index},
        "toolkit": toolkit.to_dict(),
    }
    return _RunIdentity(sha256_hex(descriptor), descriptor)


def _status_from_solve(status: SolveStatus) -> CensusStatus:
    return {
        SolveStatus.WITNESS: CensusStatus.WITNESS,
        SolveStatus.CANDIDATE_UNSAT: CensusStatus.CANDIDATE_UNSAT,
        SolveStatus.UNKNOWN: CensusStatus.UNKNOWN,
        SolveStatus.ERROR: CensusStatus.ERROR,
    }[status]


def _record_without_search(
    *,
    run_fingerprint: str,
    index: int,
    graph: SimpleGraph,
    color_count: int,
    status: CensusStatus,
    outcome_code: str,
    detail: str,
) -> CensusRecord:
    return CensusRecord(
        run_fingerprint=run_fingerprint,
        index=index,
        graph6=encode_graph6(graph),
        graph_fingerprint=graph.fingerprint,
        order=graph.order,
        size=graph.size,
        min_degree=graph.min_degree,
        max_degree=graph.max_degree,
        degree_parameter=graph.max_degree + 1,
        color_count=color_count,
        status=status,
        outcome_code=outcome_code,
        detail=detail,
        partitions_started=0,
        partitions_completed=0,
        candidate_failures=0,
        unknown_partitions=0,
    )


def _result_record(
    *,
    run_fingerprint: str,
    index: int,
    graph: SimpleGraph,
    expected_color_count: int,
    result: AuxiliarySearchResult,
) -> CensusRecord:
    if result.color_count != expected_color_count:
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=expected_color_count,
            status=CensusStatus.ERROR,
            outcome_code="result_color_count_mismatch",
            detail="search result color count does not match the configured target",
        )
    if result.graph_fingerprint != graph.fingerprint:
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=expected_color_count,
            status=CensusStatus.ERROR,
            outcome_code="result_graph_mismatch",
            detail="search result is bound to a different numbered graph",
        )
    try:
        status = _status_from_solve(result.status)
    except KeyError:
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=expected_color_count,
            status=CensusStatus.ERROR,
            outcome_code="invalid_search_status",
            detail="search backend returned an unsupported status",
        )
    outcome_code = {
        CensusStatus.WITNESS: "verified_auxiliary_witness",
        CensusStatus.CANDIDATE_UNSAT: "candidate_exhaustion_without_proof",
        CensusStatus.UNKNOWN: "incomplete_search",
        CensusStatus.ERROR: "search_backend_error",
        CensusStatus.SKIPPED: "unreachable",
    }[status]
    detail = (
        "every enumerated partition search exhausted; this is candidate evidence only and "
        "has no independently checked UNSAT proof"
        if status is CensusStatus.CANDIDATE_UNSAT
        else result.detail
    )
    try:
        certificate = None if result.witness is None else result.witness.total_coloring
        auxiliary_witness = (
            None
            if result.witness is None
            else CensusAuxiliaryWitness.from_search_witness(result.witness)
        )
        return CensusRecord(
            run_fingerprint=run_fingerprint,
            index=index,
            graph6=encode_graph6(graph),
            graph_fingerprint=graph.fingerprint,
            order=graph.order,
            size=graph.size,
            min_degree=graph.min_degree,
            max_degree=graph.max_degree,
            degree_parameter=graph.max_degree + 1,
            color_count=expected_color_count,
            status=status,
            outcome_code=outcome_code,
            detail=detail,
            partitions_started=result.partitions_started,
            partitions_completed=result.partitions_completed,
            candidate_failures=result.candidate_failures,
            unknown_partitions=result.unknown_partitions,
            certificate=certificate,
            auxiliary_witness=auxiliary_witness,
        )
    except ValueError as exc:
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=expected_color_count,
            status=CensusStatus.ERROR,
            outcome_code="invalid_search_result",
            detail=f"independent census validation rejected the search result: {exc}",
        )


def _process_graph(
    *,
    config: CensusConfig,
    run_fingerprint: str,
    index: int,
    graph: SimpleGraph,
) -> CensusRecord:
    degree_parameter = graph.max_degree + 1
    color_count = degree_parameter + config.color_offset_from_degree_parameter
    if graph.order != config.geng.order:
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=color_count,
            status=CensusStatus.ERROR,
            outcome_code="generator_order_mismatch",
            detail=f"generator produced order {graph.order}; expected {config.geng.order}",
        )
    if config.require_high_degree and 2 * graph.max_degree < graph.order:
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=color_count,
            status=CensusStatus.SKIPPED,
            outcome_code="outside_high_degree_filter",
            detail="graph does not satisfy 2*Delta(G) >= |V(G)|",
        )
    if graph.order == 0 or not degree_parameter <= graph.order <= 2 * degree_parameter:
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=color_count,
            status=CensusStatus.SKIPPED,
            outcome_code="outside_auxiliary_regime",
            detail="Delta(G)+1 equitable classes are not restricted to sizes one and two",
        )
    try:
        result = search_auxiliary_extensions(
            graph,
            color_count,
            limits_per_partition=config.limits_per_partition,
            max_partitions=config.max_partitions,
        )
    except Exception as exc:  # one bad graph must not disappear from accounting
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=color_count,
            status=CensusStatus.ERROR,
            outcome_code="search_exception",
            detail=f"{type(exc).__name__}: {exc}",
        )
    try:
        return _result_record(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            expected_color_count=color_count,
            result=result,
        )
    except Exception as exc:  # malformed backend output is still one accounted graph
        return _record_without_search(
            run_fingerprint=run_fingerprint,
            index=index,
            graph=graph,
            color_count=color_count,
            status=CensusStatus.ERROR,
            outcome_code="result_processing_exception",
            detail=f"{type(exc).__name__}: {exc}",
        )


def _write_all(file_descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise OSError("write returned no progress")
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o644)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


@contextmanager
def _exclusive_output_lock(directory: Path) -> Iterator[None]:
    path = directory / _LOCK_NAME
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CensusLockError(
            f"census output is locked by {path}; remove it only after confirming no writer exists"
        ) from exc
    try:
        _write_all(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _fsync_directory(directory)
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            path.unlink()
        _fsync_directory(directory)


def _load_canonical_json(path: Path) -> Mapping[str, object]:
    raw = path.read_bytes()
    if not raw.endswith(b"\n"):
        raise CensusFormatError(f"{path.name} must end with one LF")
    try:
        value = strict_json_loads(raw[:-1])
    except GraphFormatError as exc:
        raise CensusFormatError(f"invalid {path.name}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise CensusFormatError(f"{path.name} must contain a JSON object")
    if raw != canonical_json_bytes(value) + b"\n":
        raise CensusFormatError(f"{path.name} is not canonical JSON")
    return cast(Mapping[str, object], value)


def _scan_partial(
    path: Path,
    *,
    run_fingerprint: str,
) -> tuple[int, CensusCounts]:
    if path.is_symlink():
        raise CensusFormatError("checkpoint path must not be a symbolic link")
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.fsync(descriptor)
        os.close(descriptor)
        _fsync_directory(path.parent)
        return 0, CensusCounts()
    if not path.is_file():
        raise CensusFormatError("checkpoint path must be a regular file")

    count = 0
    counts = CensusCounts()
    with path.open("r+b") as stream:
        line_start = 0
        while True:
            raw = stream.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                stream.truncate(line_start)
                stream.flush()
                os.fsync(stream.fileno())
                break
            try:
                record = CensusRecord.from_json(raw[:-1])
            except CensusFormatError as exc:
                raise CensusFormatError(f"invalid checkpoint record {count}: {exc}") from exc
            if record.to_json().encode("utf-8") + b"\n" != raw:
                raise CensusFormatError(f"checkpoint record {count} is not canonical JSON")
            if record.run_fingerprint != run_fingerprint:
                raise CensusResumeError("checkpoint belongs to a different run configuration")
            if record.index != count:
                raise CensusResumeError(
                    f"checkpoint index discontinuity: expected {count}, got {record.index}"
                )
            counts = counts.increment(record.status)
            count += 1
            line_start = stream.tell()
    return count, counts


def _iter_checkpoint_records(path: Path, count: int) -> Iterator[CensusRecord]:
    with path.open("rb") as stream:
        for index in range(count):
            raw = stream.readline()
            if not raw.endswith(b"\n"):
                raise CensusResumeError(f"checkpoint record {index} disappeared during resume")
            yield CensusRecord.from_json(raw[:-1])


def _artifact_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _manifest_dict(
    *,
    run: _RunIdentity,
    record_count: int,
    counts: CensusCounts,
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
        "provenance": run.descriptor,
        "record_count": record_count,
        "run_fingerprint": run.fingerprint,
        "schema_version": CENSUS_MANIFEST_SCHEMA_VERSION,
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
        "schema_version": CENSUS_COMPLETION_SCHEMA_VERSION,
    }


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


def _validate_completed_run(directory: Path, run: _RunIdentity) -> CensusRunResult:
    records_path = directory / _RECORDS_NAME
    manifest_path = directory / _MANIFEST_NAME
    completion_path = directory / _COMPLETION_NAME
    for path in (records_path, manifest_path, completion_path):
        if not path.is_file():
            raise CensusFormatError(f"completed census is missing {path.name}")

    manifest = _load_canonical_json(manifest_path)
    completion = _load_canonical_json(completion_path)
    manifest_expected = {
        "artifacts",
        "complete",
        "counts",
        "provenance",
        "record_count",
        "run_fingerprint",
        "schema_version",
    }
    completion_expected = {
        "manifest_sha256",
        "record_count",
        "records_sha256",
        "run_fingerprint",
        "schema_version",
    }
    _require_exact_keys(manifest, manifest_expected, name="manifest")
    _require_exact_keys(completion, completion_expected, name="completion")
    if (
        manifest["schema_version"] != CENSUS_MANIFEST_SCHEMA_VERSION
        or manifest["complete"] is not True
    ):
        raise CensusFormatError("invalid manifest version or completion state")
    if completion["schema_version"] != CENSUS_COMPLETION_SCHEMA_VERSION:
        raise CensusFormatError("invalid completion schema_version")
    if manifest["run_fingerprint"] != run.fingerprint:
        raise CensusResumeError("completed census belongs to a different run configuration")
    if canonical_json_bytes(manifest["provenance"]) != canonical_json_bytes(run.descriptor):
        raise CensusFormatError("manifest provenance does not match its run fingerprint")

    records_digest, records_bytes = _artifact_digest(records_path)
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    artifacts = manifest["artifacts"]
    counts_value = manifest["counts"]
    if not isinstance(artifacts, Mapping) or not isinstance(counts_value, Mapping):
        raise CensusFormatError("manifest artifacts and counts must be objects")
    _require_exact_keys(
        cast(Mapping[str, object], artifacts),
        {"records_bytes", "records_path", "records_sha256"},
        name="manifest artifacts",
    )
    expected_artifacts = {
        "records_bytes": records_bytes,
        "records_path": _RECORDS_NAME,
        "records_sha256": records_digest,
    }
    if canonical_json_bytes(artifacts) != canonical_json_bytes(expected_artifacts):
        raise CensusFormatError("record artifact size or digest does not match manifest")
    expected_completion = _completion_dict(
        run_fingerprint=run.fingerprint,
        manifest_sha256=manifest_digest,
        records_sha256=records_digest,
        record_count=_require_nonnegative_int(manifest["record_count"], name="record_count"),
    )
    if canonical_json_bytes(completion) != canonical_json_bytes(expected_completion):
        raise CensusFormatError("completion marker does not match manifest and records")

    counts = CensusCounts.from_mapping(cast(Mapping[str, object], counts_value))
    record_count = _require_nonnegative_int(manifest["record_count"], name="record_count")
    scanned_count = 0
    scanned_counts = CensusCounts()
    with records_path.open("rb") as stream:
        for raw in stream:
            if not raw.endswith(b"\n"):
                raise CensusFormatError("completed JSONL has an unterminated record")
            record = CensusRecord.from_json(raw[:-1])
            if record.index != scanned_count or record.run_fingerprint != run.fingerprint:
                raise CensusFormatError("completed JSONL has a discontinuous or foreign record")
            if record.to_json().encode("utf-8") + b"\n" != raw:
                raise CensusFormatError("completed JSONL is not canonical")
            scanned_counts = scanned_counts.increment(record.status)
            scanned_count += 1
    if scanned_count != record_count or scanned_counts != counts or counts.total != record_count:
        raise CensusFormatError("manifest counts do not match completed JSONL")
    return CensusRunResult(
        run_fingerprint=run.fingerprint,
        record_count=record_count,
        counts=counts,
        resumed_records=record_count,
        records_path=records_path,
        manifest_path=manifest_path,
        completion_path=completion_path,
    )


def run_census(
    config: CensusConfig,
    output_directory: str | Path,
    *,
    executable: str = "geng",
    toolkit_identity: ToolkitIdentity | None = None,
) -> CensusRunResult:
    """Run or resume one deterministic ``geng`` census shard.

    Generator-level failure leaves a validated JSONL prefix and no completion
    marker.  Per-graph search failures become ``ERROR`` records and processing
    continues, so every graph yielded by a successful stream is counted once.
    """

    if not isinstance(config, CensusConfig):
        raise ValueError("config must be CensusConfig")
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
            return _validate_completed_run(directory, run)
        _recover_interrupted_publication(directory)

        partial_path = directory / _PARTIAL_NAME
        resumed_records, counts = _scan_partial(
            partial_path,
            run_fingerprint=run.fingerprint,
        )
        checkpoint_iterator = iter(_iter_checkpoint_records(partial_path, resumed_records))
        append_descriptor = os.open(partial_path, os.O_WRONLY | os.O_APPEND)
        processed = resumed_records
        since_sync = 0
        try:
            for index, graph in enumerate(stream_geng(config.geng, executable=resolved_executable)):
                if index < resumed_records:
                    checkpoint = next(checkpoint_iterator)
                    if checkpoint.graph_fingerprint != graph.fingerprint:
                        raise CensusResumeError(
                            f"regenerated graph stream does not match checkpoint at index {index}"
                        )
                    continue
                if not isinstance(graph, SimpleGraph):
                    raise CensusError(f"generator item {index} is not a SimpleGraph")
                record = _process_graph(
                    config=config,
                    run_fingerprint=run.fingerprint,
                    index=index,
                    graph=graph,
                )
                _write_all(append_descriptor, canonical_json_bytes(record.to_dict()) + b"\n")
                counts = counts.increment(record.status)
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
            raise CensusError("internal accounting invariant failed")
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
        return CensusRunResult(
            run_fingerprint=run.fingerprint,
            record_count=processed,
            counts=counts,
            resumed_records=resumed_records,
            records_path=records_path,
            manifest_path=manifest_path,
            completion_path=completion_path,
        )


__all__ = [
    "AUXILIARY_WITNESS_SCHEMA_VERSION",
    "CENSUS_BACKEND_ID",
    "CENSUS_COMPLETION_SCHEMA_VERSION",
    "CENSUS_MANIFEST_SCHEMA_VERSION",
    "CENSUS_RECORD_SCHEMA_VERSION",
    "CensusAuxiliaryWitness",
    "CensusConfig",
    "CensusCounts",
    "CensusError",
    "CensusFormatError",
    "CensusLockError",
    "CensusRecord",
    "CensusResumeError",
    "CensusRunResult",
    "CensusStatus",
    "ToolkitIdentity",
    "detect_toolkit_identity",
    "run_census",
]
