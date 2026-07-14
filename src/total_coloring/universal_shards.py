"""Exact, read-only validation for a complete universal-census shard set."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, cast

from total_coloring.census import (
    MAX_CENSUS_METADATA_BYTES,
    CensusError,
    ToolkitIdentity,
)
from total_coloring.geng import GengIdentity, geng_identity, resolve_geng, stream_geng
from total_coloring.graph import GraphFormatError, canonical_json_bytes, strict_json_loads
from total_coloring.graph6 import encode_graph6
from total_coloring.universal_census import (
    UniversalCensusConfig,
    UniversalCensusCounts,
    UniversalCensusValidation,
    UniversalCheckSpec,
    validate_completed_universal_census,
)

DEFAULT_MAX_UNION_GRAPHS: Final = 10_000_000
_SHARD_ARTIFACT_NAMES: Final = (
    "records.jsonl",
    "manifest.json",
    "completion.json",
)


class UniversalShardSetError(CensusError):
    """A proposed shard set is incomplete, inconsistent, or overlapping."""


@dataclass(frozen=True, slots=True)
class UniversalShardReceipt:
    """Immutable receipt for one already replayed shard."""

    shard_index: int
    run_fingerprint: str
    record_count: int
    partition_count: int
    check_evaluations: int
    counts: UniversalCensusCounts
    records_bytes: int
    records_sha256: str
    manifest_sha256: str
    completion_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "check_evaluations": self.check_evaluations,
            "completion_sha256": self.completion_sha256,
            "counts": self.counts.to_dict(),
            "manifest_sha256": self.manifest_sha256,
            "partition_count": self.partition_count,
            "record_count": self.record_count,
            "records_bytes": self.records_bytes,
            "records_sha256": self.records_sha256,
            "run_fingerprint": self.run_fingerprint,
            "shard_index": self.shard_index,
        }


@dataclass(frozen=True, slots=True)
class UniversalShardSetValidation:
    """Deterministic result of transcript replay and exact union validation."""

    order: int
    shard_count: int
    split_depth: int | None
    checks: tuple[UniversalCheckSpec, ...]
    generator_executable: str
    generator_sha256: str
    toolkit: ToolkitIdentity
    record_count: int
    partition_count: int
    check_evaluations: int
    records_bytes: int
    counts: UniversalCensusCounts
    receipts: tuple[UniversalShardReceipt, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "generator": {
                "executable": self.generator_executable,
                "sha256": self.generator_sha256,
            },
            "order": self.order,
            "shard_count": self.shard_count,
            "shards": [receipt.to_dict() for receipt in self.receipts],
            "split_depth": self.split_depth,
            "toolkit": self.toolkit.to_dict(),
            "totals": {
                "check_evaluations": self.check_evaluations,
                "counts": self.counts.to_dict(),
                "partition_count": self.partition_count,
                "record_count": self.record_count,
                "records_bytes": self.records_bytes,
            },
        }


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _ValidatedShard:
    directory: Path
    validation: UniversalCensusValidation
    receipt: UniversalShardReceipt
    identities: tuple[_PathIdentity, _PathIdentity, _PathIdentity]


@dataclass(frozen=True, slots=True)
class _OpenShardDirectory:
    shard_index: int
    path: Path
    descriptor: int
    identity: _PathIdentity


@dataclass(frozen=True, slots=True)
class _ArtifactSnapshot:
    identity: _PathIdentity
    size: int
    sha256: str


def _stat_identity(status: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _path_identity(path: Path) -> _PathIdentity:
    try:
        status = path.lstat()
    except OSError as exc:
        raise UniversalShardSetError(f"cannot inspect shard artifact {path}: {exc}") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise UniversalShardSetError(f"shard artifact must be a regular non-symlink file: {path}")
    return _stat_identity(status)


def _directory_identity(path: Path) -> _PathIdentity:
    try:
        status = path.lstat()
    except OSError as exc:
        raise UniversalShardSetError(f"cannot inspect shard directory {path}: {exc}") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise UniversalShardSetError(
            f"shard directory must be a directory, not a symbolic link: {path}"
        )
    return _stat_identity(status)


def _directory_entries(directory: _OpenShardDirectory) -> tuple[str, ...]:
    try:
        entries = tuple(sorted(os.listdir(directory.descriptor)))
    except OSError as exc:
        raise UniversalShardSetError(
            f"cannot inventory shard {directory.shard_index} directory {directory.path}: {exc}"
        ) from exc
    expected = tuple(sorted(_SHARD_ARTIFACT_NAMES))
    if entries != expected:
        raise UniversalShardSetError(
            f"shard {directory.shard_index} artifact inventory must be exactly "
            f"{list(expected)}; found {list(entries)}"
        )
    return entries


def _open_shard_directory(path: Path, *, shard_index: int) -> _OpenShardDirectory:
    identity = _directory_identity(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UniversalShardSetError(f"cannot open shard directory {path}: {exc}") from exc
    try:
        opened = _stat_identity(os.fstat(descriptor))
        if opened != identity or not stat.S_ISDIR(opened.mode):
            raise UniversalShardSetError(
                f"shard {shard_index} directory changed while it was opened: {path}"
            )
        directory = _OpenShardDirectory(shard_index, path, descriptor, identity)
        _directory_entries(directory)
    except BaseException:
        os.close(descriptor)
        raise
    return directory


def _artifact_identity_at(directory: _OpenShardDirectory, name: str) -> _PathIdentity:
    try:
        status = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    except OSError as exc:
        raise UniversalShardSetError(
            f"cannot inspect shard {directory.shard_index} artifact {name}: {exc}"
        ) from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise UniversalShardSetError(
            f"shard {directory.shard_index} artifact must be a regular non-symlink file: {name}"
        )
    return _stat_identity(status)


def _hash_bound_artifact(directory: _OpenShardDirectory, name: str) -> _ArtifactSnapshot:
    """Hash one artifact through a descriptor bracketed by entry identities."""

    before = _artifact_identity_at(directory, name)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory.descriptor)
    except OSError as exc:
        raise UniversalShardSetError(
            f"cannot open shard {directory.shard_index} artifact {name}: {exc}"
        ) from exc
    try:
        opened = _stat_identity(os.fstat(descriptor))
        if opened != before or not stat.S_ISREG(opened.mode):
            raise UniversalShardSetError(
                f"shard {directory.shard_index} artifact {name} changed while it was opened"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        after_descriptor = _stat_identity(os.fstat(descriptor))
    except OSError as exc:
        raise UniversalShardSetError(
            f"cannot read shard {directory.shard_index} artifact {name}: {exc}"
        ) from exc
    finally:
        os.close(descriptor)
    after_entry = _artifact_identity_at(directory, name)
    if after_descriptor != opened or after_entry != opened or size != opened.size:
        raise UniversalShardSetError(
            f"shard {directory.shard_index} artifact {name} changed while it was hashed"
        )
    return _ArtifactSnapshot(opened, size, digest.hexdigest())


def _artifact_identities(
    validation: UniversalCensusValidation,
) -> tuple[_PathIdentity, _PathIdentity, _PathIdentity]:
    result = validation.result
    return tuple(
        _path_identity(path)
        for path in (result.records_path, result.manifest_path, result.completion_path)
    )  # type: ignore[return-value]


def _metadata_mapping(path: Path, *, name: str) -> tuple[bytes, Mapping[str, object]]:
    with path.open("rb") as stream:
        data = stream.read(MAX_CENSUS_METADATA_BYTES + 1)
    if len(data) > MAX_CENSUS_METADATA_BYTES:
        raise UniversalShardSetError(f"{name} exceeds the metadata size limit")
    try:
        value = strict_json_loads(data)
    except GraphFormatError as exc:
        raise UniversalShardSetError(f"invalid {name}: {exc}") from exc
    if not isinstance(value, Mapping) or canonical_json_bytes(value) + b"\n" != data:
        raise UniversalShardSetError(f"{name} is not canonical JSON with one trailing LF")
    return data, cast(Mapping[str, object], value)


def _receipt(validation: UniversalCensusValidation) -> UniversalShardReceipt:
    config = validation.config
    index = config.geng.shard_index
    if index is None:
        raise UniversalShardSetError("every shard-set run must declare a shard index")
    result = validation.result
    manifest_bytes, manifest = _metadata_mapping(result.manifest_path, name="manifest.json")
    completion_bytes, _ = _metadata_mapping(result.completion_path, name="completion.json")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise UniversalShardSetError("validated manifest artifacts must be an object")
    records_bytes = artifacts.get("records_bytes")
    records_sha256 = artifacts.get("records_sha256")
    if (
        isinstance(records_bytes, bool)
        or not isinstance(records_bytes, int)
        or records_bytes < 0
        or not isinstance(records_sha256, str)
        or len(records_sha256) != 64
        or any(character not in "0123456789abcdef" for character in records_sha256)
    ):
        raise UniversalShardSetError("validated manifest has malformed record receipt")
    return UniversalShardReceipt(
        shard_index=index,
        run_fingerprint=result.run_fingerprint,
        record_count=result.record_count,
        partition_count=result.partition_count,
        check_evaluations=result.partition_count * len(config.checks),
        counts=result.counts,
        records_bytes=records_bytes,
        records_sha256=records_sha256,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        completion_sha256=hashlib.sha256(completion_bytes).hexdigest(),
    )


def _uniform_config(config: UniversalCensusConfig) -> bytes:
    value = config.to_dict()
    generator_spec = dict(cast(Mapping[str, object], value["generator_spec"]))
    generator_spec.pop("shard_index")
    value["generator_spec"] = generator_spec
    return canonical_json_bytes(value)


def _sum_counts(validations: Sequence[_ValidatedShard]) -> UniversalCensusCounts:
    names = UniversalCensusCounts().to_dict()
    return UniversalCensusCounts(
        **{name: sum(item.receipt.counts.to_dict()[name] for item in validations) for name in names}
    )


def _require_unchanged(shards: Sequence[_ValidatedShard]) -> None:
    for shard in shards:
        before = _artifact_identities(shard.validation)
        if before != shard.identities:
            raise UniversalShardSetError(
                f"shard {shard.receipt.shard_index} artifacts changed during validation"
            )
        result = shard.validation.result
        digests = (
            _sha256_path(result.records_path),
            _sha256_path(result.manifest_path),
            _sha256_path(result.completion_path),
        )
        after = _artifact_identities(shard.validation)
        if after != before:
            raise UniversalShardSetError(
                f"shard {shard.receipt.shard_index} artifacts changed during final hashing"
            )
        expected = (
            shard.receipt.records_sha256,
            shard.receipt.manifest_sha256,
            shard.receipt.completion_sha256,
        )
        if digests != expected:
            raise UniversalShardSetError(
                f"shard {shard.receipt.shard_index} artifact content changed during validation"
            )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def recheck_universal_shard_artifact_inventory(
    validation: UniversalShardSetValidation,
    run_directories: Sequence[str | Path],
) -> UniversalShardSetValidation:
    """Recheck one validated shard set's files without scientific replay.

    ``run_directories[index]`` must be the completed directory for shard
    ``index``.  Each directory must contain exactly ``records.jsonl``,
    ``manifest.json``, and ``completion.json``.  The function opens directories
    and artifacts without following symbolic links, brackets every hash with
    descriptor and directory-entry identities, and performs a final whole-set
    identity sweep.  It never writes and never invokes a solver or ``geng``.

    The original validation is returned unchanged on success so callers can
    conveniently replace a stale trust decision with this fresh artifact gate.
    """

    if not isinstance(validation, UniversalShardSetValidation):
        raise ValueError("validation must be a UniversalShardSetValidation")
    if isinstance(run_directories, str | bytes) or not isinstance(run_directories, Sequence):
        raise ValueError("run_directories must be a sequence of paths")
    if len(run_directories) != validation.shard_count:
        raise UniversalShardSetError(
            f"expected {validation.shard_count} shard directories, received {len(run_directories)}"
        )
    receipts = {receipt.shard_index: receipt for receipt in validation.receipts}
    expected_indices = set(range(validation.shard_count))
    if len(receipts) != len(validation.receipts) or set(receipts) != expected_indices:
        raise UniversalShardSetError(
            "validation receipts must contain exactly one receipt for every shard index"
        )

    opened: list[_OpenShardDirectory] = []
    snapshots: dict[tuple[int, str], _ArtifactSnapshot] = {}
    try:
        seen_directories: set[tuple[int, int]] = set()
        for shard_index, raw_directory in enumerate(run_directories):
            directory = _open_shard_directory(
                Path(raw_directory).absolute(),
                shard_index=shard_index,
            )
            opened.append(directory)
            identity_key = (directory.identity.device, directory.identity.inode)
            if identity_key in seen_directories:
                raise UniversalShardSetError("shard directories must be distinct")
            seen_directories.add(identity_key)

        for directory in opened:
            receipt = receipts[directory.shard_index]
            expected = {
                "records.jsonl": (receipt.records_bytes, receipt.records_sha256),
                "manifest.json": (None, receipt.manifest_sha256),
                "completion.json": (None, receipt.completion_sha256),
            }
            for name in _SHARD_ARTIFACT_NAMES:
                expected_size, expected_sha256 = expected[name]
                if (
                    not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in expected_sha256)
                ):
                    raise UniversalShardSetError(
                        f"shard {directory.shard_index} has a malformed {name} SHA-256 receipt"
                    )
                snapshot = _hash_bound_artifact(directory, name)
                if expected_size is not None and snapshot.size != expected_size:
                    raise UniversalShardSetError(
                        f"shard {directory.shard_index} {name} size changed: "
                        f"expected {expected_size}, got {snapshot.size}"
                    )
                if snapshot.sha256 != expected_sha256:
                    raise UniversalShardSetError(
                        f"shard {directory.shard_index} {name} SHA-256 changed: "
                        f"expected {expected_sha256}, got {snapshot.sha256}"
                    )
                snapshots[(directory.shard_index, name)] = snapshot

        for directory in opened:
            _directory_entries(directory)
            for name in _SHARD_ARTIFACT_NAMES:
                current = _artifact_identity_at(directory, name)
                if current != snapshots[(directory.shard_index, name)].identity:
                    raise UniversalShardSetError(
                        f"shard {directory.shard_index} artifact {name} changed after hashing"
                    )
            try:
                descriptor_identity = _stat_identity(os.fstat(directory.descriptor))
            except OSError as exc:
                raise UniversalShardSetError(
                    f"cannot recheck shard {directory.shard_index} directory: {exc}"
                ) from exc
            if (
                descriptor_identity != directory.identity
                or _directory_identity(directory.path) != directory.identity
            ):
                raise UniversalShardSetError(
                    f"shard {directory.shard_index} directory changed during inventory recheck"
                )
    finally:
        for directory in reversed(opened):
            os.close(directory.descriptor)

    return validation


def _validate_exact_union(
    shards: Sequence[_ValidatedShard],
    *,
    executable: str,
    max_union_graphs: int,
) -> int:
    union: set[str] = set()
    for shard in shards:
        generated = 0
        for graph in stream_geng(shard.validation.config.geng, executable=executable):
            graph6 = encode_graph6(graph)
            if graph6 in union:
                raise UniversalShardSetError(
                    f"generated graph overlap detected at shard {shard.receipt.shard_index}"
                )
            if len(union) >= max_union_graphs:
                raise UniversalShardSetError(
                    f"shard union exceeds the configured {max_union_graphs}-graph memory cap"
                )
            union.add(graph6)
            generated += 1
        if generated != shard.receipt.record_count:
            raise UniversalShardSetError(
                f"shard {shard.receipt.shard_index} generator count changed after replay"
            )

    union_count = len(union)
    base_spec = shards[0].validation.config.geng
    unsharded_spec = replace(
        base_spec,
        shard_index=None,
        shard_count=None,
        split_depth=None,
    )
    direct_count = 0
    for graph in stream_geng(unsharded_spec, executable=executable):
        graph6 = encode_graph6(graph)
        if graph6 not in union:
            raise UniversalShardSetError(
                "unsharded generator emitted a missing or duplicate shard-union graph"
            )
        union.remove(graph6)
        direct_count += 1
    if union:
        raise UniversalShardSetError(
            f"shard union contains {len(union)} graph(s) absent from the unsharded stream"
        )
    if direct_count != union_count:
        raise UniversalShardSetError("unsharded and shard-union generator counts disagree")
    return union_count


def validate_completed_universal_shard_set(
    run_directories: Sequence[str | Path],
    *,
    executable: str = "geng",
    max_union_graphs: int = DEFAULT_MAX_UNION_GRAPHS,
) -> UniversalShardSetValidation:
    """Replay every shard and prove its disjoint union equals the direct stream.

    The function never writes to a run directory. It intentionally leaves the
    version-1 public exporter unchanged: this validates a computational array,
    but it does not turn sharded transcripts into an unsharded release artifact.
    """

    if isinstance(run_directories, str | bytes) or not isinstance(run_directories, Sequence):
        raise ValueError("run_directories must be a sequence of paths")
    if not run_directories:
        raise ValueError("run_directories must not be empty")
    if (
        isinstance(max_union_graphs, bool)
        or not isinstance(max_union_graphs, int)
        or max_union_graphs <= 0
    ):
        raise ValueError("max_union_graphs must be a positive integer")

    resolved = str(resolve_geng(executable))
    validated: list[_ValidatedShard] = []
    for raw_directory in run_directories:
        directory = Path(raw_directory)
        before = tuple(
            _path_identity(directory / name)
            for name in ("records.jsonl", "manifest.json", "completion.json")
        )
        validation = validate_completed_universal_census(directory, executable=resolved)
        identities = _artifact_identities(validation)
        if identities != before:
            raise UniversalShardSetError("shard artifacts changed during completed-run replay")
        receipt = _receipt(validation)
        if _artifact_identities(validation) != identities:
            raise UniversalShardSetError(
                f"shard {receipt.shard_index} artifacts changed while its receipt was read"
            )
        validated.append(_ValidatedShard(directory.resolve(), validation, receipt, identities))

    first = validated[0].validation
    first_spec = first.config.geng
    shard_count = first_spec.shard_count
    if shard_count is None or first_spec.shard_index is None:
        raise UniversalShardSetError("every run must use explicit geng sharding")
    if len(validated) != shard_count:
        raise UniversalShardSetError(
            f"expected {shard_count} shard directories, received {len(validated)}"
        )
    indices = [item.receipt.shard_index for item in validated]
    expected_indices = list(range(shard_count))
    if sorted(indices) != expected_indices:
        raise UniversalShardSetError(
            f"shard indices must be exactly {expected_indices}; received {sorted(indices)}"
        )

    reference_config = _uniform_config(first.config)
    reference_generator = (first.generator.executable, first.generator.sha256)
    for item in validated:
        current = item.validation
        if _uniform_config(current.config) != reference_config:
            raise UniversalShardSetError("shards do not share one non-index census config")
        if current.config.geng.split_depth != first_spec.split_depth:
            raise UniversalShardSetError("shards do not share one split depth")
        if (current.generator.executable, current.generator.sha256) != reference_generator:
            raise UniversalShardSetError("shards do not share one geng executable identity")
        if current.toolkit != first.toolkit:
            raise UniversalShardSetError("shards do not share one toolkit identity")

    ordered = sorted(validated, key=lambda item: item.receipt.shard_index)
    generator_before: GengIdentity = geng_identity(first_spec, executable=resolved)
    if (generator_before.executable, generator_before.sha256) != reference_generator:
        raise UniversalShardSetError("geng identity changed after per-shard validation")
    union_count = _validate_exact_union(
        ordered,
        executable=resolved,
        max_union_graphs=max_union_graphs,
    )
    generator_after = geng_identity(first_spec, executable=resolved)
    if generator_after != generator_before:
        raise UniversalShardSetError("geng identity changed during shard-union validation")
    _require_unchanged(ordered)

    record_count = sum(item.receipt.record_count for item in ordered)
    if record_count != union_count:
        raise UniversalShardSetError("validated record total does not equal the exact graph union")
    partition_count = sum(item.receipt.partition_count for item in ordered)
    check_evaluations = sum(item.receipt.check_evaluations for item in ordered)
    records_bytes = sum(item.receipt.records_bytes for item in ordered)
    counts = _sum_counts(ordered)
    if counts.total != record_count:
        raise UniversalShardSetError("aggregate status counts do not equal the record total")
    return UniversalShardSetValidation(
        order=first_spec.order,
        shard_count=shard_count,
        split_depth=first_spec.split_depth,
        checks=first.config.checks,
        generator_executable=first.generator.executable,
        generator_sha256=first.generator.sha256,
        toolkit=first.toolkit,
        record_count=record_count,
        partition_count=partition_count,
        check_evaluations=check_evaluations,
        records_bytes=records_bytes,
        counts=counts,
        receipts=tuple(item.receipt for item in ordered),
    )


__all__ = [
    "DEFAULT_MAX_UNION_GRAPHS",
    "UniversalShardReceipt",
    "UniversalShardSetError",
    "UniversalShardSetValidation",
    "recheck_universal_shard_artifact_inventory",
    "validate_completed_universal_shard_set",
]
