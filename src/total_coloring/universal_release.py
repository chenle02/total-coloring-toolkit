"""Deterministic, review-gated export of completed universal census runs.

The exporter never downloads data and never writes into a Git repository.  It
creates a standalone candidate bundle plus a separate replay archive.  The
existing :mod:`total_coloring.publishing` transaction can subsequently promote
the small bundle into ``total-coloring-data`` after independent review.
"""

from __future__ import annotations

import ctypes
import errno
import gzip
import hashlib
import os
import re
import secrets
import stat
import tarfile
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, cast
from urllib.parse import urlsplit

from total_coloring.backends import SolverBackend
from total_coloring.census import (
    MAX_CENSUS_METADATA_BYTES,
    CensusFormatError,
    _fsync_directory,
)
from total_coloring.geng import GengError
from total_coloring.graph import canonical_json_bytes, strict_json_loads
from total_coloring.schema_resources import read_schema_bytes
from total_coloring.universal_census import (
    MAX_OFFLINE_UNIVERSAL_ORDER,
    MAX_UNIVERSAL_RECORD_BYTES,
    PARTITION_ENUMERATOR_ID,
    UNIVERSAL_OBJECTIVE,
    UniversalCensusCounts,
    UniversalCensusTranscriptValidation,
    UniversalCensusValidation,
    UniversalCheckSpec,
    validate_completed_universal_census,
    validate_completed_universal_transcript,
)

DATASET_MANIFEST_SCHEMA_NAME: Final = "dataset-manifest-v2.schema.json"
UNIVERSAL_SUMMARY_SCHEMA_NAME: Final = "universal-census-summary-v1.schema.json"
DATASET_MANIFEST_SCHEMA_PATH: Final = PurePosixPath(f"schemas/{DATASET_MANIFEST_SCHEMA_NAME}")
UNIVERSAL_SUMMARY_SCHEMA_PATH: Final = PurePosixPath(f"schemas/{UNIVERSAL_SUMMARY_SCHEMA_NAME}")
SUMMARY_SCHEMA_VERSION: Final = "total-coloring.universal-census-summary.v1"
MANIFEST_SCHEMA_VERSION: Final = "2.0.0"
DEFAULT_CODE_REPOSITORY: Final = "https://github.com/chenle02/total-coloring-toolkit"
DEFAULT_DATASET_REPOSITORY: Final = "https://github.com/chenle02/total-coloring-data"
DEFAULT_DATASET_ID: Final = "total-coloring-data"
DEFAULT_DATASET_TITLE: Final = "Total Coloring Data"
DEFAULT_DATASET_LICENSE: Final = "CC-BY-4.0"
ARCHIVE_MEDIA_TYPE: Final = "application/gzip"
DEFAULT_LIMITATIONS: Final = (
    "The finite census is computational evidence and does not establish an unbounded theorem.",
    "Generator completeness is assumed for the hash-pinned nauty-geng executable.",
)
_REQUIRED_CHECKS: Final = {
    "dsatur-delta-plus-2": UniversalCheckSpec(SolverBackend.DSATUR, 1),
    "dsatur-delta-plus-3": UniversalCheckSpec(SolverBackend.DSATUR, 2),
    "static-delta-plus-2": UniversalCheckSpec(SolverBackend.STATIC, 1),
}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_COMMIT_PATTERN = re.compile(r"(?!0{40}$)[0-9a-f]{40}")
_SUMMARY_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CLAIM_ID_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*")
_SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
)
_CANONICAL_GZIP_HEADER: Final = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
_TAR_BLOCK_BYTES: Final = 512
_TAR_RECORD_BYTES: Final = 10_240
_STREAM_CHUNK_BYTES: Final = 1024 * 1024
_MAX_REPLAY_UNCOMPRESSED_BYTES: Final = 16 * 1024 * 1024 * 1024
_RUN_MANIFEST_NAME: Final = "manifest.json"
_RUN_COMPLETION_NAME: Final = "completion.json"
_RUN_RECORDS_NAME: Final = "records.jsonl"
_MAX_RELEASE_RUNS: Final = 256


class UniversalReleaseError(RuntimeError):
    """A completed run or requested export violates the public contract."""


@dataclass(frozen=True, slots=True)
class UniversalReleaseConfig:
    """Immutable metadata and output locations for one candidate release."""

    bundle_root: Path
    archive_path: Path
    summary_id: str
    created_utc: str
    release_version: str
    code_commit: str
    external_artifact: PurePosixPath
    external_url: str
    claim_id: str = "UAUX-BOUND"
    release_status: str = "candidate"
    code_repository: str = DEFAULT_CODE_REPOSITORY
    dataset_repository: str = DEFAULT_DATASET_REPOSITORY
    expected_toolkit_source_sha256: str | None = None
    expected_generator_sha256: str | None = None

    def __post_init__(self) -> None:
        if _SUMMARY_ID_PATTERN.fullmatch(self.summary_id) is None:
            raise ValueError("summary_id must be a lowercase hyphenated identifier")
        if _CLAIM_ID_PATTERN.fullmatch(self.claim_id) is None:
            raise ValueError("claim_id must be an uppercase hyphenated identifier")
        if _SEMVER_PATTERN.fullmatch(self.release_version) is None:
            raise ValueError("release_version must be canonical SemVer")
        if self.release_status not in {"candidate", "published"}:
            raise ValueError("release_status must be candidate or published")
        if _COMMIT_PATTERN.fullmatch(self.code_commit) is None:
            raise ValueError("code_commit must be a nonzero lowercase 40-hex Git commit")
        _require_canonical_utc(self.created_utc)
        for label, url in (
            ("code_repository", self.code_repository),
            ("dataset_repository", self.dataset_repository),
        ):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError(f"{label} must be a stable HTTPS URL")
        if not _safe_relative_path(self.external_artifact):
            raise ValueError("external_artifact must be a safe normalized relative path")
        if any(part.startswith(".") for part in self.external_artifact.parts):
            raise ValueError("external_artifact may not contain hidden path components")
        if not self.external_artifact.name.endswith(".tar.gz"):
            raise ValueError("external_artifact must name a .tar.gz archive")
        if not is_stable_https_url(self.external_url):
            raise ValueError("external_url must be stable HTTPS and end in the archive basename")
        parsed_external = urlsplit(self.external_url)
        if not parsed_external.path.endswith("/" + self.external_artifact.name):
            raise ValueError("external_url must be stable HTTPS and end in the archive basename")
        for label, digest in (
            ("expected_toolkit_source_sha256", self.expected_toolkit_source_sha256),
            ("expected_generator_sha256", self.expected_generator_sha256),
        ):
            if digest is not None and _SHA256_PATTERN.fullmatch(digest) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ArchiveMemberReceipt:
    path: PurePosixPath
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"bytes": self.bytes, "path": self.path.as_posix(), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class UniversalReleaseResult:
    bundle_root: Path
    archive_path: Path
    summary_path: Path
    manifest_path: Path
    archive_bytes: int
    archive_sha256: str
    orders: tuple[int, ...]
    totals: dict[str, object]


@dataclass(frozen=True, slots=True)
class _RunExport:
    validation: UniversalCensusValidation
    order: int
    members: dict[str, ArchiveMemberReceipt]


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    dev: int
    ino: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileIdentity:
        return cls(
            dev=value.st_dev,
            ino=value.st_ino,
            mode=stat.S_IFMT(value.st_mode),
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _EntryIdentity:
    dev: int
    ino: int
    kind: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _EntryIdentity:
        return cls(value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


@dataclass(frozen=True, slots=True)
class _RunSnapshots:
    paths: tuple[Path, ...]
    member_identities: tuple[tuple[Path, _FileIdentity], ...]

    def assert_unchanged(self) -> None:
        for path, expected in self.member_identities:
            with _open_regular_nofollow(path) as (_descriptor, actual):
                if actual != expected:
                    raise UniversalReleaseError(
                        f"private run snapshot changed after capture: {path.name}"
                    )


@dataclass(frozen=True, slots=True)
class _OutputTarget:
    path: Path
    parent_path: Path
    parent_descriptor: int
    parent_identity: _EntryIdentity
    leaf: str


@dataclass(frozen=True, slots=True)
class _HeldLock:
    target: _OutputTarget
    name: str
    descriptor: int
    identity: _EntryIdentity


_RENAME_NOREPLACE = 1


def _rename_noreplace(
    *,
    source_directory_descriptor: int,
    source_name: str,
    destination_directory_descriptor: int,
    destination_name: str,
) -> None:
    """Call Linux renameat2(RENAME_NOREPLACE), retrying only EINTR."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "libc does not expose renameat2")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    while True:
        if (
            renameat2(
                source_directory_descriptor,
                source,
                destination_directory_descriptor,
                destination,
                _RENAME_NOREPLACE,
            )
            == 0
        ):
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EINTR:
            continue
        raise OSError(error_number, os.strerror(error_number))


def _translate_noreplace_error(exc: OSError, *, action: str) -> UniversalReleaseError:
    if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
        return UniversalReleaseError(f"{action}: destination already exists; no-clobber enforced")
    if exc.errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        return UniversalReleaseError(
            f"{action}: Linux renameat2(RENAME_NOREPLACE) is unsupported "
            "by this platform/filesystem"
        )
    if exc.errno == errno.EXDEV:
        return UniversalReleaseError(f"{action}: internal same-parent rename crossed filesystems")
    return UniversalReleaseError(f"{action}: renameat2 failed: {exc}")


@contextmanager
def _open_output_targets(
    bundle_path: Path, archive_path: Path
) -> Iterator[tuple[_OutputTarget, _OutputTarget]]:
    descriptors: list[int] = []
    try:
        targets: list[_OutputTarget] = []
        for path in (bundle_path, archive_path):
            descriptor, leaf = _open_parent_directory_nofollow(path)
            descriptors.append(descriptor)
            parent_stat = os.fstat(descriptor)
            target = _OutputTarget(
                path=path,
                parent_path=Path(os.path.abspath(os.fspath(path))).parent,
                parent_descriptor=descriptor,
                parent_identity=_EntryIdentity.from_stat(parent_stat),
                leaf=leaf,
            )
            if _entry_at(descriptor, leaf) is not None:
                raise UniversalReleaseError(f"refusing to overwrite output path: {path}")
            targets.append(target)
        bundle, archive = targets
        if (
            bundle.parent_identity.dev,
            bundle.parent_identity.ino,
            bundle.leaf,
        ) == (
            archive.parent_identity.dev,
            archive.parent_identity.ino,
            archive.leaf,
        ):
            raise UniversalReleaseError("bundle and archive output paths alias one destination")
        yield bundle, archive
    except OSError as exc:
        raise UniversalReleaseError(f"cannot open strict output parent: {exc}") from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _lock_name(target: _OutputTarget) -> str:
    key = f"{target.parent_identity.dev}:{target.parent_identity.ino}:{target.leaf}".encode()
    return f".universal-release-{hashlib.sha256(key).hexdigest()[:24]}.lock"


@contextmanager
def _cooperative_output_locks(targets: Sequence[_OutputTarget]) -> Iterator[None]:
    held: list[_HeldLock] = []
    cleanup_errors: list[str] = []
    ordered = sorted(
        targets,
        key=lambda target: (
            target.parent_identity.dev,
            target.parent_identity.ino,
            target.leaf,
        ),
    )
    try:
        for target in ordered:
            name = _lock_name(target)
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=target.parent_descriptor,
                )
            except OSError as exc:
                raise UniversalReleaseError(
                    f"cannot acquire cooperative output lock for {target.path}: {exc}"
                ) from exc
            os.fchmod(descriptor, 0o600)
            _write_all_fd(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
            identity = _EntryIdentity.from_stat(os.fstat(descriptor))
            held.append(_HeldLock(target, name, descriptor, identity))
            os.fsync(target.parent_descriptor)
        yield
    finally:
        for lock in reversed(held):
            os.close(lock.descriptor)
            current = _entry_at(lock.target.parent_descriptor, lock.name)
            if current == lock.identity:
                try:
                    os.unlink(lock.name, dir_fd=lock.target.parent_descriptor)
                    os.fsync(lock.target.parent_descriptor)
                except OSError as exc:
                    cleanup_errors.append(f"{lock.name}: {exc}")
            elif current is not None:
                cleanup_errors.append(f"{lock.name}: foreign replacement preserved")
        if cleanup_errors:
            raise UniversalReleaseError(
                "cooperative output lock cleanup failed: " + "; ".join(cleanup_errors)
            )


def _open_parent_directory_nofollow(path: Path) -> tuple[int, str]:
    """Open an absolute parent component-by-component without following links."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute.name in {"", ".", ".."}:
        raise UniversalReleaseError(f"path must name a filesystem entry: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parent.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, absolute.name


@contextmanager
def _open_regular_nofollow(path: Path) -> Iterator[tuple[int, _FileIdentity]]:
    parent_descriptor = -1
    descriptor = -1
    try:
        parent_descriptor, leaf = _open_parent_directory_nofollow(path)
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        before = _FileIdentity.from_stat(os.fstat(descriptor))
        if before.mode != stat.S_IFREG:
            raise UniversalReleaseError("replay archive must be a regular non-symlink file")
        yield descriptor, before
        after = _FileIdentity.from_stat(os.fstat(descriptor))
        named = _FileIdentity.from_stat(
            os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
        )
        if after != before or named != before:
            raise UniversalReleaseError("replay archive changed during validation")
    except OSError as exc:
        raise UniversalReleaseError(f"cannot safely open replay archive: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _entry_at(descriptor: int, name: str) -> _EntryIdentity | None:
    try:
        return _EntryIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _unique_staging_name(target: _OutputTarget, *, kind: str) -> str:
    for _attempt in range(100):
        name = f".{target.leaf}.{kind}-{secrets.token_hex(12)}"
        if _entry_at(target.parent_descriptor, name) is None:
            return name
    raise UniversalReleaseError(f"cannot allocate private {kind} staging name")


def _remove_directory_contents(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        entry = _entry_at(descriptor, name)
        if entry is None:
            continue
        if entry.kind == stat.S_IFDIR:
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                _remove_directory_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _cleanup_owned_staging(
    target: _OutputTarget,
    name: str,
    expected: _EntryIdentity,
) -> str | None:
    current = _entry_at(target.parent_descriptor, name)
    if current is None:
        return None
    if current != expected:
        return f"{name}: foreign staging replacement preserved"
    try:
        if expected.kind == stat.S_IFDIR:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=target.parent_descriptor,
            )
            try:
                if _EntryIdentity.from_stat(os.fstat(descriptor)) != expected:
                    return f"{name}: staging directory identity changed; preserved"
                _remove_directory_contents(descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if _entry_at(target.parent_descriptor, name) != expected:
                return f"{name}: staging directory was replaced during cleanup; preserved"
            os.rmdir(name, dir_fd=target.parent_descriptor)
        else:
            os.unlink(name, dir_fd=target.parent_descriptor)
        os.fsync(target.parent_descriptor)
    except OSError as exc:
        return f"{name}: cleanup failed: {exc}"
    return None


def _rollback_installed_output(
    target: _OutputTarget,
    *,
    staging_name: str,
    expected: _EntryIdentity,
) -> str | None:
    current = _entry_at(target.parent_descriptor, target.leaf)
    if current is None:
        return None
    if current != expected:
        return f"{target.path}: foreign final replacement preserved"
    try:
        _rename_noreplace(
            source_directory_descriptor=target.parent_descriptor,
            source_name=target.leaf,
            destination_directory_descriptor=target.parent_descriptor,
            destination_name=staging_name,
        )
    except OSError as exc:
        return str(_translate_noreplace_error(exc, action=f"rollback {target.path}"))
    if _entry_at(target.parent_descriptor, staging_name) != expected:
        return f"{target.path}: rollback moved an unexpected inode"
    try:
        os.fsync(target.parent_descriptor)
    except OSError as exc:
        return f"{target.path}: rollback parent fsync failed: {exc}"
    return None


def _install_output_noreplace(
    target: _OutputTarget,
    *,
    staging_name: str,
    expected: _EntryIdentity,
) -> None:
    if _entry_at(target.parent_descriptor, staging_name) != expected:
        raise UniversalReleaseError(f"staged output identity changed before install: {target.path}")
    try:
        _rename_noreplace(
            source_directory_descriptor=target.parent_descriptor,
            source_name=staging_name,
            destination_directory_descriptor=target.parent_descriptor,
            destination_name=target.leaf,
        )
    except OSError as exc:
        raise _translate_noreplace_error(exc, action=f"install {target.path}") from exc
    if _entry_at(target.parent_descriptor, target.leaf) != expected:
        raise UniversalReleaseError(f"installed output identity mismatch: {target.path}")


def _write_all_fd(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - POSIX write invariant
            raise OSError("short write while creating private snapshot")
        view = view[written:]


def _copy_snapshot_member(
    source_descriptor: int,
    destination_directory_descriptor: int,
    name: str,
) -> _FileIdentity:
    destination_descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=destination_directory_descriptor,
    )
    try:
        os.fchmod(destination_descriptor, 0o600)
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while chunk := os.read(source_descriptor, _STREAM_CHUNK_BYTES):
            _write_all_fd(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
        identity = _FileIdentity.from_stat(os.fstat(destination_descriptor))
        if identity.mode != stat.S_IFREG:
            raise UniversalReleaseError("snapshot destination is not a regular file")
        return identity
    finally:
        os.close(destination_descriptor)


def _snapshot_one_run(
    source: Path,
    destination_directory_descriptor: int,
    *,
    run_name: str,
    owned_members: dict[tuple[str, str], _EntryIdentity],
) -> tuple[_FileIdentity, ...]:
    parent_descriptor = -1
    source_directory_descriptor = -1
    source_descriptors: list[int] = []
    names = ("manifest.json", "completion.json", _RUN_RECORDS_NAME)
    try:
        parent_descriptor, leaf = _open_parent_directory_nofollow(source)
        source_directory_descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        directory_before = _FileIdentity.from_stat(os.fstat(source_directory_descriptor))
        if directory_before.mode != stat.S_IFDIR:
            raise UniversalReleaseError("completed run source must be a real directory")
        if _entry_at(source_directory_descriptor, ".records.jsonl.partial") is not None:
            raise UniversalReleaseError("completed run source contains a partial record stream")
        source_identities: list[_FileIdentity] = []
        for name in names:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=source_directory_descriptor,
            )
            identity = _FileIdentity.from_stat(os.fstat(descriptor))
            if identity.mode != stat.S_IFREG:
                os.close(descriptor)
                raise UniversalReleaseError(f"completed run {name} must be a regular file")
            source_descriptors.append(descriptor)
            source_identities.append(identity)

        captured_identities: list[_FileIdentity] = []
        for descriptor, name in zip(source_descriptors, names, strict=True):
            captured = _copy_snapshot_member(descriptor, destination_directory_descriptor, name)
            captured_identities.append(captured)
            owned_members[(run_name, name)] = _EntryIdentity(
                captured.dev, captured.ino, captured.mode
            )
        destination_identities = tuple(captured_identities)
        for descriptor, name, expected in zip(
            source_descriptors, names, source_identities, strict=True
        ):
            if _FileIdentity.from_stat(os.fstat(descriptor)) != expected:
                raise UniversalReleaseError(f"completed run {name} changed while copying")
            named = _FileIdentity.from_stat(
                os.stat(name, dir_fd=source_directory_descriptor, follow_symlinks=False)
            )
            if named != expected:
                raise UniversalReleaseError(f"completed run {name} was replaced while copying")
        if _entry_at(source_directory_descriptor, ".records.jsonl.partial") is not None:
            raise UniversalReleaseError("partial record stream appeared while copying run")
        if _FileIdentity.from_stat(os.fstat(source_directory_descriptor)) != directory_before:
            raise UniversalReleaseError("completed run directory changed while copying")
        os.fsync(destination_directory_descriptor)
        return destination_identities
    except OSError as exc:
        raise UniversalReleaseError(f"cannot capture completed run {source}: {exc}") from exc
    finally:
        for descriptor in source_descriptors:
            os.close(descriptor)
        if source_directory_descriptor >= 0:
            os.close(source_directory_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _cleanup_snapshot_tree(
    parent_descriptor: int,
    root_descriptor: int,
    root_name: str,
    root_identity: _EntryIdentity,
    run_names: Sequence[str],
    owned_members: Mapping[tuple[str, str], _EntryIdentity],
) -> None:
    errors: list[str] = []
    for run_name in run_names:
        run_descriptor = -1
        try:
            run_descriptor = os.open(
                run_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_descriptor,
            )
            for member_name in ("manifest.json", "completion.json", "records.jsonl"):
                entry = _entry_at(run_descriptor, member_name)
                expected = owned_members.get((run_name, member_name))
                if entry is not None and entry == expected:
                    os.unlink(member_name, dir_fd=run_descriptor)
                elif entry is not None:
                    errors.append(f"{run_name}/{member_name}: foreign replacement preserved")
            os.fsync(run_descriptor)
            os.rmdir(run_name, dir_fd=root_descriptor)
        except OSError as exc:
            errors.append(f"{run_name}: {exc}")
        finally:
            if run_descriptor >= 0:
                os.close(run_descriptor)
    try:
        os.fsync(root_descriptor)
        if _entry_at(parent_descriptor, root_name) != root_identity:
            errors.append("snapshot root pathname was replaced; foreign entry preserved")
        else:
            os.rmdir(root_name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
    except OSError as exc:
        errors.append(f"snapshot root: {exc}")
    if errors:
        raise UniversalReleaseError("private snapshot cleanup failed: " + "; ".join(errors))


@contextmanager
def _private_run_snapshots(
    run_directories: Sequence[str | Path], *, parent: Path
) -> Iterator[_RunSnapshots]:
    parent_descriptor = -1
    root_descriptor = -1
    root_name = ""
    run_names: list[str] = []
    owned_members: dict[tuple[str, str], _EntryIdentity] = {}
    root_identity: _EntryIdentity | None = None
    try:
        parent_descriptor, _unused = _open_parent_directory_nofollow(parent / ".snapshot")
        for _attempt in range(100):
            candidate = f".universal-census-snapshots-{secrets.token_hex(12)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            root_name = candidate
            break
        if not root_name:  # pragma: no cover - cryptographic-name exhaustion
            raise UniversalReleaseError("cannot allocate a private snapshot directory")
        root_descriptor = os.open(
            root_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        os.fchmod(root_descriptor, 0o700)
        root_identity = _EntryIdentity.from_stat(os.fstat(root_descriptor))
        snapshot_paths: list[Path] = []
        member_identities: list[tuple[Path, _FileIdentity]] = []
        absolute_parent = Path(os.path.abspath(os.fspath(parent)))
        for index, source in enumerate(run_directories):
            run_name = f"run-{index:03d}"
            os.mkdir(run_name, 0o700, dir_fd=root_descriptor)
            run_names.append(run_name)
            run_descriptor = os.open(
                run_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_descriptor,
            )
            try:
                os.fchmod(run_descriptor, 0o700)
                identities = _snapshot_one_run(
                    Path(source),
                    run_descriptor,
                    run_name=run_name,
                    owned_members=owned_members,
                )
            finally:
                os.close(run_descriptor)
            snapshot_path = absolute_parent / root_name / run_name
            snapshot_paths.append(snapshot_path)
            member_identities.extend(
                (snapshot_path / member_name, identity)
                for member_name, identity in zip(
                    ("manifest.json", "completion.json", "records.jsonl"),
                    identities,
                    strict=True,
                )
            )
        os.fsync(root_descriptor)
        snapshots = _RunSnapshots(tuple(snapshot_paths), tuple(member_identities))
        snapshots.assert_unchanged()
        yield snapshots
    finally:
        cleanup_error: BaseException | None = None
        if root_descriptor >= 0 and root_identity is not None:
            try:
                _cleanup_snapshot_tree(
                    parent_descriptor,
                    root_descriptor,
                    root_name,
                    root_identity,
                    run_names,
                    owned_members,
                )
            except BaseException as exc:
                cleanup_error = exc
        if root_descriptor >= 0:
            os.close(root_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if cleanup_error is not None:
            raise cleanup_error


def _require_canonical_utc(value: str) -> None:
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value) is None:
        raise ValueError("created_utc must be canonical UTC with second precision")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("created_utc is not a real UTC date-time") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):  # pragma: no cover - defensive
        raise ValueError("created_utc must be UTC")


def is_stable_https_url(value: str) -> bool:
    """Return whether *value* is a normalized immutable-artifact HTTPS URL.

    This deliberately accepts a narrower language than a general URI parser:
    ASCII only, a literal lowercase ``https://`` prefix, canonical lowercase
    DNS host, no authority decorations or percent escapes, and a nonempty path
    whose literal components cannot change traversal semantics.  The
    public-data verifier implements the same predicate.
    """

    if (
        not isinstance(value, str)
        or not value.startswith("https://")
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or "\\" in value
        or "%" in value
        or "?" in value
        or "#" in value
        or not value.isascii()
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or len(hostname) > 253
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != hostname
        or hostname != hostname.lower()
    ):
        return False
    labels = hostname.split(".")
    if len(labels) < 2 or any(
        len(label) > 63 or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in labels
    ):
        return False
    if not parsed.path.startswith("/") or parsed.path in {"", "/"}:
        return False
    parts = parsed.path.split("/")[1:]
    return bool(parts) and all(
        part not in {"", ".", ".."}
        and re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@-]+", part) is not None
        for part in parts
    )


def _safe_relative_path(path: PurePosixPath) -> bool:
    value = path.as_posix()
    return bool(
        value
        and not path.is_absolute()
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in path.parts)
        and all(ord(character) >= 32 for character in value)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _receipt(path: Path, archive_path: PurePosixPath) -> ArchiveMemberReceipt:
    with _open_regular_nofollow(path) as (descriptor, identity):
        return ArchiveMemberReceipt(archive_path, identity.size, _sha256_fd(descriptor))


def _check_id(spec: UniversalCheckSpec) -> str:
    backend = "dsatur" if spec.backend is SolverBackend.DSATUR else "static"
    return f"{backend}-delta-plus-{spec.palette_offset + 1}"


def _check_description(spec: UniversalCheckSpec) -> str:
    backend = "DSATUR" if spec.backend is SolverBackend.DSATUR else "static-order"
    delta_offset = spec.palette_offset + 1
    return (
        f"Replayable {backend} witness check with Delta(G)+{delta_offset} colors "
        "for every canonical equitable partition."
    )


def release_summary_path(orders: Sequence[int]) -> PurePosixPath:
    """Return the release-scoped compact-summary path for a canonical order set."""

    values = tuple(orders)
    if (
        not values
        or any(
            isinstance(order, bool) or not isinstance(order, int) or order < 1 for order in values
        )
        or values != tuple(sorted(set(values)))
    ):
        raise ValueError("orders must be a nonempty sorted unique sequence of positive integers")
    scope = str(values[0]) if len(values) == 1 else f"{values[0]}-{values[-1]}"
    return PurePosixPath(f"results/order-{scope}-universal-census-summary-v1.json")


def _uniform_run_contract(validations: Sequence[UniversalCensusValidation]) -> None:
    if not validations:
        raise UniversalReleaseError("at least one completed universal census run is required")
    if len(validations) > _MAX_RELEASE_RUNS:
        raise UniversalReleaseError("public v1 summaries support at most 256 order runs")
    first = validations[0]
    if first.config.checks != tuple(_REQUIRED_CHECKS.values()):
        raise UniversalReleaseError("run check matrix must equal the v1 DSATUR/static audit matrix")
    if not first.config.require_high_degree:
        raise UniversalReleaseError("public v1 summaries require the high-degree filter")
    reference_config = first.config.to_dict()
    reference_generator_spec = dict(cast(Mapping[str, object], reference_config["generator_spec"]))
    reference_generator_spec.pop("order")
    reference_config["generator_spec"] = reference_generator_spec
    for validation in validations:
        config = validation.config
        spec = config.geng
        if isinstance(spec.order, bool) or not isinstance(spec.order, int) or spec.order < 1:
            raise UniversalReleaseError("public universal-census orders must be positive")
        if (
            spec.connected
            or spec.min_degree is not None
            or spec.max_degree is not None
            or spec.shard_index is not None
            or spec.shard_count is not None
            or validation.generator.arguments != ("-q", str(spec.order))
        ):
            raise UniversalReleaseError(
                "public v1 summaries require one unrestricted, unsharded geng run per order"
            )
        compared = config.to_dict()
        compared_generator_spec = dict(cast(Mapping[str, object], compared["generator_spec"]))
        compared_generator_spec.pop("order")
        compared["generator_spec"] = compared_generator_spec
        if canonical_json_bytes(compared) != canonical_json_bytes(reference_config):
            raise UniversalReleaseError("all order runs must use one identical non-order config")
        if config.checks != first.config.checks:
            raise UniversalReleaseError("all order runs must use the same ordered check matrix")
        if validation.generator.executable != first.generator.executable or (
            validation.generator.sha256 != first.generator.sha256
        ):
            raise UniversalReleaseError("all order runs must use the same geng executable bytes")
        if validation.toolkit != first.toolkit:
            raise UniversalReleaseError("all order runs must use the same toolkit identity")


def _make_exports(
    validations: Sequence[UniversalCensusValidation],
) -> tuple[_RunExport, ...]:
    exports: list[_RunExport] = []
    seen_orders: set[int] = set()
    for validation in validations:
        order = validation.config.geng.order
        if order in seen_orders:
            raise UniversalReleaseError(f"duplicate completed run for order {order}")
        seen_orders.add(order)
        prefix = PurePosixPath(f"order-{order:02d}")
        result = validation.result
        exports.append(
            _RunExport(
                validation=validation,
                order=order,
                members={
                    "completion": _receipt(result.completion_path, prefix / "completion.json"),
                    "manifest": _receipt(result.manifest_path, prefix / "manifest.json"),
                    "records": _receipt(result.records_path, prefix / "records.jsonl"),
                },
            )
        )
    exports.sort(key=lambda item: item.order)
    return tuple(exports)


def _archive_sources(
    exports: Sequence[_RunExport],
) -> tuple[tuple[ArchiveMemberReceipt, Path], ...]:
    sources: list[tuple[ArchiveMemberReceipt, Path]] = []
    for export in exports:
        result = export.validation.result
        source_by_kind = {
            "completion": result.completion_path,
            "manifest": result.manifest_path,
            "records": result.records_path,
        }
        sources.extend((receipt, source_by_kind[kind]) for kind, receipt in export.members.items())
    return tuple(sorted(sources, key=lambda item: item[0].path.as_posix()))


def _write_deterministic_archive(
    path: Path, sources: Sequence[tuple[ArchiveMemberReceipt, Path]]
) -> None:
    with path.open("xb") as raw:
        os.fchmod(raw.fileno(), 0o600)
        with (
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped,
            tarfile.open(fileobj=zipped, mode="w|", format=tarfile.USTAR_FORMAT) as archive,
        ):
            for receipt, source in sources:
                info = tarfile.TarInfo(receipt.path.as_posix())
                info.size = receipt.bytes
                info.mode = 0o644
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with _open_regular_nofollow(source) as (descriptor, identity):
                    if identity.size != receipt.bytes:
                        raise UniversalReleaseError(
                            "run artifact size changed after its archive receipt"
                        )
                    with os.fdopen(os.dup(descriptor), "rb") as handle:
                        handle.seek(0)
                        archive.addfile(info, handle)
        raw.flush()
        os.fsync(raw.fileno())


def _canonical_tar_layout(
    receipts: Sequence[ArchiveMemberReceipt],
) -> tuple[tuple[tuple[int, bytes], ...], tuple[tuple[int, int], ...], int, int]:
    headers: list[tuple[int, bytes]] = []
    zero_ranges: list[tuple[int, int]] = []
    offset = 0
    for receipt in receipts:
        if (
            not _safe_relative_path(receipt.path)
            or any(part.startswith(".") for part in receipt.path.parts)
            or isinstance(receipt.bytes, bool)
            or not isinstance(receipt.bytes, int)
            or receipt.bytes < 0
            or _SHA256_PATTERN.fullmatch(receipt.sha256) is None
        ):
            raise UniversalReleaseError("expected archive receipt is malformed")
        if (
            receipt.path.name in {_RUN_MANIFEST_NAME, _RUN_COMPLETION_NAME}
            and receipt.bytes > MAX_CENSUS_METADATA_BYTES
        ):
            raise UniversalReleaseError("census metadata member exceeds the 4 MiB limit")
        info = tarfile.TarInfo(receipt.path.as_posix())
        info.size = receipt.bytes
        info.mode = 0o644
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        try:
            header = info.tobuf(
                format=tarfile.USTAR_FORMAT,
                encoding="utf-8",
                errors="surrogateescape",
            )
        except (UnicodeError, ValueError) as exc:
            raise UniversalReleaseError("archive receipt cannot be encoded as USTAR") from exc
        if len(header) != _TAR_BLOCK_BYTES:  # pragma: no cover - tarfile invariant
            raise UniversalReleaseError("canonical USTAR header has an invalid length")
        headers.append((offset, header))
        data_end = offset + _TAR_BLOCK_BYTES + receipt.bytes
        offset = (
            offset
            + _TAR_BLOCK_BYTES
            + ((receipt.bytes + _TAR_BLOCK_BYTES - 1) // _TAR_BLOCK_BYTES) * _TAR_BLOCK_BYTES
        )
        if data_end < offset:
            zero_ranges.append((data_end, offset))
    terminal_end = offset + 2 * _TAR_BLOCK_BYTES
    archive_bytes = (
        (terminal_end + _TAR_RECORD_BYTES - 1) // _TAR_RECORD_BYTES
    ) * _TAR_RECORD_BYTES
    if archive_bytes > _MAX_REPLAY_UNCOMPRESSED_BYTES:
        raise UniversalReleaseError("declared USTAR expands beyond the 16 GiB limit")
    zero_ranges.append((offset, archive_bytes))
    return tuple(headers), tuple(zero_ranges), offset, archive_bytes


def _check_decompressed_chunk(
    chunk: bytes,
    *,
    start: int,
    headers: Sequence[tuple[int, bytes]],
    zero_ranges: Sequence[tuple[int, int]],
) -> None:
    end = start + len(chunk)
    for header_offset, expected in headers:
        header_end = header_offset + len(expected)
        overlap_start = max(start, header_offset)
        overlap_end = min(end, header_end)
        if overlap_start < overlap_end:
            actual_start = overlap_start - start
            expected_start = overlap_start - header_offset
            length = overlap_end - overlap_start
            if (
                chunk[actual_start : actual_start + length]
                != expected[expected_start : expected_start + length]
            ):
                raise UniversalReleaseError("replay archive is not canonical USTAR")
    for zero_start, zero_end in zero_ranges:
        overlap_start = max(start, zero_start)
        overlap_end = min(end, zero_end)
        if overlap_start < overlap_end and any(chunk[overlap_start - start : overlap_end - start]):
            raise UniversalReleaseError("replay archive USTAR padding must be all zero")


def _validate_canonical_gzip_ustar(
    handle: BinaryIO, receipts: Sequence[ArchiveMemberReceipt]
) -> int:
    headers, zero_ranges, logical_end, expected_uncompressed_bytes = _canonical_tar_layout(receipts)
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    decompressed_bytes = 0
    try:
        handle.seek(0)
        header = handle.read(len(_CANONICAL_GZIP_HEADER))
        if header != _CANONICAL_GZIP_HEADER:
            raise UniversalReleaseError("replay archive must use the canonical gzip level-9 header")
        handle.seek(0)
        gzip_eof = False
        while raw_chunk := handle.read(_STREAM_CHUNK_BYTES):
            pending = raw_chunk
            drain_buffered_output = False
            while pending or drain_buffered_output:
                drain_buffered_output = False
                maximum = min(
                    _STREAM_CHUNK_BYTES,
                    expected_uncompressed_bytes - decompressed_bytes + 1,
                )
                try:
                    output = decompressor.decompress(pending, maximum)
                except zlib.error as exc:
                    raise UniversalReleaseError(
                        f"replay archive gzip trailer or stream is invalid: {exc}"
                    ) from exc
                _check_decompressed_chunk(
                    output,
                    start=decompressed_bytes,
                    headers=headers,
                    zero_ranges=zero_ranges,
                )
                decompressed_bytes += len(output)
                if decompressed_bytes > expected_uncompressed_bytes:
                    raise UniversalReleaseError(
                        "replay archive USTAR terminal length is noncanonical"
                    )
                pending = decompressor.unconsumed_tail
                if decompressor.eof:
                    if decompressor.unused_data or pending or handle.read(1):
                        raise UniversalReleaseError(
                            "replay archive must contain exactly one gzip member to raw EOF"
                        )
                    gzip_eof = True
                    break
                if not pending and len(output) == maximum:
                    drain_buffered_output = True
            if gzip_eof:
                break
        if not gzip_eof:
            raise UniversalReleaseError("replay archive gzip stream or trailer is truncated")
    except OSError as exc:
        raise UniversalReleaseError(f"cannot read replay archive: {exc}") from exc
    if decompressed_bytes != expected_uncompressed_bytes:
        raise UniversalReleaseError("replay archive USTAR terminal length is noncanonical")
    return logical_end


def _update_jsonl_line_length(current: int, chunk: bytes) -> int:
    start = 0
    while True:
        newline = chunk.find(b"\n", start)
        if newline < 0:
            current += len(chunk) - start
            if current > MAX_UNIVERSAL_RECORD_BYTES:
                raise UniversalReleaseError("replay archive JSONL record exceeds the 16 MiB limit")
            return current
        current += newline - start + 1
        if current > MAX_UNIVERSAL_RECORD_BYTES:
            raise UniversalReleaseError("replay archive JSONL record exceeds the 16 MiB limit")
        current = 0
        start = newline + 1


def _validate_replay_archive_structure_fd(
    descriptor: int,
    expected: Iterable[ArchiveMemberReceipt],
) -> None:
    receipts = tuple(sorted(expected, key=lambda item: item.path.as_posix()))
    expected_paths = tuple(item.path.as_posix() for item in receipts)
    if len(expected_paths) != len(set(expected_paths)):
        raise UniversalReleaseError("expected archive member paths must be unique")
    with os.fdopen(os.dup(descriptor), "rb") as compressed:
        logical_end = _validate_canonical_gzip_ustar(compressed, receipts)
    member_count = 0
    actual_logical_end = 0
    try:
        with os.fdopen(os.dup(descriptor), "rb") as raw:
            raw.seek(0)
            with tarfile.open(fileobj=raw, mode="r:gz") as archive:
                for index, member in enumerate(archive):
                    if index >= len(receipts):
                        raise UniversalReleaseError("replay archive contains an undeclared member")
                    receipt = receipts[index]
                    if member.name != receipt.path.as_posix():
                        raise UniversalReleaseError(
                            "replay archive members are not in canonical order"
                        )
                    if (
                        member.type != tarfile.REGTYPE
                        or member.linkname
                        or member.pax_headers
                        or member.offset != actual_logical_end
                        or member.offset_data != actual_logical_end + _TAR_BLOCK_BYTES
                    ):
                        raise UniversalReleaseError("replay archive members must be regular files")
                    if (
                        member.mode != 0o644
                        or member.mtime != 0
                        or member.uid != 0
                        or member.gid != 0
                        or member.uname != ""
                        or member.gname != ""
                    ):
                        raise UniversalReleaseError("replay archive metadata is not normalized")
                    if member.size != receipt.bytes:
                        raise UniversalReleaseError("replay archive member byte count mismatch")
                    extracted = archive.extractfile(member)
                    if extracted is None:  # pragma: no cover - guarded by isreg
                        raise UniversalReleaseError("cannot read replay archive member")
                    digest = hashlib.sha256()
                    jsonl_line_bytes = 0
                    with extracted:
                        while chunk := extracted.read(1024 * 1024):
                            digest.update(chunk)
                            if receipt.path.name == _RUN_RECORDS_NAME:
                                jsonl_line_bytes = _update_jsonl_line_length(
                                    jsonl_line_bytes, chunk
                                )
                    if receipt.path.name == _RUN_RECORDS_NAME and jsonl_line_bytes:
                        raise UniversalReleaseError("replay archive records.jsonl must end with LF")
                    if digest.hexdigest() != receipt.sha256:
                        raise UniversalReleaseError("replay archive member SHA-256 mismatch")
                    member_count = index + 1
                    actual_logical_end = (
                        member.offset_data
                        + ((member.size + _TAR_BLOCK_BYTES - 1) // _TAR_BLOCK_BYTES)
                        * _TAR_BLOCK_BYTES
                    )
    except (EOFError, OSError, tarfile.TarError, zlib.error) as exc:
        raise UniversalReleaseError(f"cannot parse replay archive: {exc}") from exc
    if member_count != len(expected_paths):
        raise UniversalReleaseError("replay archive member inventory is incomplete")
    if actual_logical_end != logical_end:
        raise UniversalReleaseError("replay archive USTAR layout is inconsistent")


def _validate_replay_archive_structure(
    archive_path: str | Path,
    expected: Iterable[ArchiveMemberReceipt],
) -> None:
    """Internal receipt-level USTAR validator used before a summary exists."""

    with _open_regular_nofollow(Path(archive_path)) as (descriptor, _identity):
        _validate_replay_archive_structure_fd(descriptor, expected)


def _summary_archive_contract(
    summary: Mapping[str, object],
) -> tuple[tuple[ArchiveMemberReceipt, ...], tuple[Mapping[str, object], ...], int, str]:
    replay = summary.get("replay_archive")
    runs = summary.get("runs")
    if not isinstance(replay, Mapping) or not isinstance(runs, list):
        raise UniversalReleaseError("replay archive validation requires a complete summary")
    archive_bytes = replay.get("bytes")
    archive_sha256 = replay.get("sha256")
    if (
        isinstance(archive_bytes, bool)
        or not isinstance(archive_bytes, int)
        or archive_bytes < 0
        or not isinstance(archive_sha256, str)
        or _SHA256_PATTERN.fullmatch(archive_sha256) is None
    ):
        raise UniversalReleaseError("summary replay archive receipt is malformed")
    if not runs or len(runs) > _MAX_RELEASE_RUNS:
        raise UniversalReleaseError("summary must contain between 1 and 256 runs")
    parsed_runs: list[Mapping[str, object]] = []
    receipts: list[ArchiveMemberReceipt] = []
    for run_index, raw_run in enumerate(runs):
        if not isinstance(raw_run, Mapping):
            raise UniversalReleaseError(f"summary run {run_index} must be an object")
        run = cast(Mapping[str, object], raw_run)
        order = run.get("order")
        members = run.get("members")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or not 1 <= order <= MAX_OFFLINE_UNIVERSAL_ORDER
            or not isinstance(members, Mapping)
        ):
            raise UniversalReleaseError(f"summary run {run_index} contract is malformed")
        expected_names = {
            "completion": "completion.json",
            "manifest": "manifest.json",
            "records": "records.jsonl",
        }
        if set(members) != set(expected_names):
            raise UniversalReleaseError(f"summary run {run_index} member map is incomplete")
        for kind, basename in expected_names.items():
            raw_receipt = members[kind]
            if not isinstance(raw_receipt, Mapping):
                raise UniversalReleaseError(f"summary run {run_index} receipt is malformed")
            path_value = raw_receipt.get("path")
            byte_count = raw_receipt.get("bytes")
            digest = raw_receipt.get("sha256")
            expected_path = PurePosixPath(f"order-{order:02d}") / basename
            if (
                path_value != expected_path.as_posix()
                or isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or byte_count < 0
                or not isinstance(digest, str)
                or _SHA256_PATTERN.fullmatch(digest) is None
            ):
                raise UniversalReleaseError(
                    f"summary run {run_index} {kind} receipt is noncanonical"
                )
            receipts.append(ArchiveMemberReceipt(expected_path, byte_count, digest))
        parsed_runs.append(run)
    sorted_receipts = tuple(sorted(receipts, key=lambda item: item.path.as_posix()))
    if len({receipt.path for receipt in sorted_receipts}) != len(sorted_receipts):
        raise UniversalReleaseError("summary archive member receipts must be unique")
    return sorted_receipts, tuple(parsed_runs), archive_bytes, archive_sha256


def _actual_transcript_descriptor(
    transcript: UniversalCensusTranscriptValidation,
) -> dict[str, object]:
    shard_index = (
        transcript.config.geng.shard_index if transcript.config.geng.shard_index is not None else 0
    )
    shard_count = (
        transcript.config.geng.shard_count if transcript.config.geng.shard_count is not None else 1
    )
    return {
        "config": transcript.config.to_dict(),
        "generator": {
            "arguments": list(transcript.generator.arguments),
            "executable": transcript.generator.executable,
            "name": "nauty-geng",
            "sha256": transcript.generator.sha256,
        },
        "objective": UNIVERSAL_OBJECTIVE,
        "shard": {"count": shard_count, "index": shard_index},
        "toolkit": transcript.toolkit.to_dict(),
    }


def _expected_summary_descriptor(
    summary: Mapping[str, object], run: Mapping[str, object]
) -> dict[str, object]:
    scope = summary.get("scope")
    configuration = summary.get("configuration")
    generator = summary.get("generator")
    producer = summary.get("producer")
    checks = summary.get("checks")
    if not all(isinstance(item, Mapping) for item in (scope, configuration, generator, producer)):
        raise UniversalReleaseError("summary provenance objects are malformed")
    if not isinstance(checks, list) or not all(isinstance(item, Mapping) for item in checks):
        raise UniversalReleaseError("summary check matrix is malformed")
    scope = cast(Mapping[str, object], scope)
    configuration = cast(Mapping[str, object], configuration)
    generator = cast(Mapping[str, object], generator)
    producer = cast(Mapping[str, object], producer)
    return {
        "config": {
            "checkpoint_interval": configuration.get("checkpoint_interval"),
            "checks": [
                {
                    "backend_id": check.get("backend_id"),
                    "palette_offset": check.get("palette_offset"),
                }
                for check in checks
            ],
            "filters": {"require_high_degree": scope.get("require_high_degree")},
            "fix_distinguished_colors": scope.get("fix_distinguished_colors"),
            "generator_spec": {
                "connected": scope.get("connected"),
                "max_degree": scope.get("max_degree"),
                "min_degree": scope.get("min_degree"),
                "order": run.get("order"),
                "shard_count": None,
                "shard_index": None,
            },
            "partition_enumerator": scope.get("partition_enumerator"),
            "search_limits": configuration.get("search_limits"),
        },
        "generator": {
            "arguments": run.get("generator_arguments"),
            "executable": generator.get("executable"),
            "name": generator.get("name"),
            "sha256": generator.get("sha256"),
        },
        "objective": scope.get("objective"),
        "shard": {"count": run.get("shard_count"), "index": run.get("shard_index")},
        "toolkit": {
            "distribution_version": producer.get("distribution_version"),
            "python_implementation": producer.get("python_implementation"),
            "python_version": producer.get("python_version"),
            "source_sha256": producer.get("source_sha256"),
        },
    }


def _validate_semantic_archive_fd(
    descriptor: int,
    summary: Mapping[str, object],
    runs: Sequence[Mapping[str, object]],
    *,
    executable: str,
) -> None:
    try:
        with os.fdopen(os.dup(descriptor), "rb") as raw:
            raw.seek(0)
            with tarfile.open(fileobj=raw, mode="r:gz") as archive:
                member_by_name = {member.name: member for member in archive.getmembers()}
                for run_index, run in enumerate(runs):
                    members = cast(Mapping[str, Mapping[str, object]], run["members"])
                    payloads: dict[str, bytes] = {}
                    for kind in ("manifest", "completion"):
                        member = member_by_name[cast(str, members[kind]["path"])]
                        extracted = archive.extractfile(member)
                        if extracted is None:  # pragma: no cover - structural pass proves regular
                            raise UniversalReleaseError("cannot read replay metadata member")
                        with extracted:
                            payload = extracted.read(MAX_CENSUS_METADATA_BYTES + 1)
                        if len(payload) != member.size:
                            raise UniversalReleaseError("replay metadata member size changed")
                        payloads[kind] = payload
                    records_member = member_by_name[cast(str, members["records"]["path"])]
                    records_stream = archive.extractfile(records_member)
                    if records_stream is None:  # pragma: no cover - structural pass proves regular
                        raise UniversalReleaseError("cannot read replay records member")
                    with records_stream:
                        transcript = validate_completed_universal_transcript(
                            payloads["manifest"],
                            payloads["completion"],
                            records_stream,
                            executable=executable,
                        )
                    expected_descriptor = _expected_summary_descriptor(summary, run)
                    if canonical_json_bytes(_actual_transcript_descriptor(transcript)) != (
                        canonical_json_bytes(expected_descriptor)
                    ):
                        raise UniversalReleaseError(
                            f"replay run {run_index} provenance does not match summary"
                        )
                    expected_values = {
                        "check_evaluations": transcript.check_evaluations,
                        "counts": transcript.counts.to_dict(),
                        "partition_count": transcript.partition_count,
                        "record_count": transcript.record_count,
                        "run_fingerprint": transcript.run_fingerprint,
                    }
                    for field, expected_value in expected_values.items():
                        if canonical_json_bytes(run.get(field)) != canonical_json_bytes(
                            expected_value
                        ):
                            raise UniversalReleaseError(
                                f"replay run {run_index} {field} does not match transcript"
                            )
    except UniversalReleaseError:
        raise
    except (
        CensusFormatError,
        EOFError,
        GengError,
        KeyError,
        OSError,
        ValueError,
        tarfile.TarError,
    ) as exc:
        raise UniversalReleaseError(f"replay archive semantic validation failed: {exc}") from exc


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, _STREAM_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _validate_replay_archive_fd(
    descriptor: int,
    identity: _FileIdentity,
    summary: Mapping[str, object],
    *,
    executable: str,
) -> None:
    receipts, runs, expected_bytes, expected_sha256 = _summary_archive_contract(summary)
    if identity.size != expected_bytes or _sha256_fd(descriptor) != expected_sha256:
        raise UniversalReleaseError("replay archive does not match the summary receipt")
    _validate_replay_archive_structure_fd(descriptor, receipts)
    _validate_semantic_archive_fd(descriptor, summary, runs, executable=executable)
    if _FileIdentity.from_stat(os.fstat(descriptor)) != identity:
        raise UniversalReleaseError("replay archive inode changed during validation")


def validate_replay_archive(
    archive_path: str | Path,
    summary: Mapping[str, object],
    *,
    executable: str = "geng",
) -> None:
    """Validate canonical bytes and every embedded run against its summary."""

    if not isinstance(summary, Mapping):
        raise ValueError("summary must be a mapping")
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise ValueError("executable must be a nonempty command or path")
    # Reject malformed and out-of-policy contracts before touching archive I/O.
    _summary_archive_contract(summary)
    with _open_regular_nofollow(Path(archive_path)) as (descriptor, identity):
        _validate_replay_archive_fd(
            descriptor,
            identity,
            summary,
            executable=executable,
        )


def _sum_counts(exports: Sequence[_RunExport]) -> UniversalCensusCounts:
    values = {key: 0 for key in UniversalCensusCounts().to_dict()}
    for export in exports:
        for key, value in export.validation.result.counts.to_dict().items():
            values[key] += value
    return UniversalCensusCounts(**values)


def canonical_finite_scope(orders: Sequence[int]) -> str:
    """Derive the one version-1 finite-claim scope from its complete run orders."""

    values = tuple(orders)
    if (
        not values
        or any(
            isinstance(order, bool) or not isinstance(order, int) or order < 1 for order in values
        )
        or values != tuple(sorted(set(values)))
    ):
        raise ValueError("finite scope orders must be positive, unique, and sorted")
    rendered = ", ".join(str(order) for order in values)
    return (
        "Only the complete unrestricted nauty-geng streams for the declared orders "
        f"{rendered}, filtered by 2*Delta(G) >= n, with every canonical equitable "
        "(Delta(G)+1)-class partition subjected to the three declared positive-witness checks."
    )


def _summary(
    config: UniversalReleaseConfig,
    exports: Sequence[_RunExport],
    archive_bytes: int,
    archive_sha256: str,
) -> dict[str, object]:
    first = exports[0].validation
    check_items = sorted(
        (
            {
                "backend_id": spec.backend.value,
                "check_id": _check_id(spec),
                "description": _check_description(spec),
                "palette_offset": spec.palette_offset,
            }
            for spec in first.config.checks
        ),
        key=lambda item: str(item["check_id"]),
    )
    required_check_ids = sorted(_REQUIRED_CHECKS)
    run_items: list[dict[str, object]] = []
    total_partitions = 0
    total_records = 0
    total_evaluations = 0
    for export in exports:
        result = export.validation.result
        evaluations = result.partition_count * len(check_items)
        total_records += result.record_count
        total_partitions += result.partition_count
        total_evaluations += evaluations
        run_items.append(
            {
                "check_evaluations": evaluations,
                "counts": result.counts.to_dict(),
                "generator_arguments": list(export.validation.generator.arguments),
                "members": {
                    kind: export.members[kind].to_dict()
                    for kind in ("manifest", "completion", "records")
                },
                "order": export.order,
                "partition_count": result.partition_count,
                "record_count": result.record_count,
                "run_fingerprint": result.run_fingerprint,
                "shard_count": 1,
                "shard_index": 0,
            }
        )
    counts = _sum_counts(exports)
    totals = {
        "check_evaluations": total_evaluations,
        "counts": counts.to_dict(),
        "order_count": len(exports),
        "partition_count": total_partitions,
        "record_count": total_records,
    }
    orders = [export.order for export in exports]
    finite_scope = canonical_finite_scope(orders)
    claim_limitations = list(DEFAULT_LIMITATIONS)
    return {
        "$schema": UNIVERSAL_SUMMARY_SCHEMA_PATH.as_posix(),
        "checks": check_items,
        "claims": [
            {
                "claim_id": config.claim_id,
                "claim_type": "finite_bound",
                "finite_scope": finite_scope,
                "limitations": claim_limitations,
                "orders": orders,
                "required_checks": required_check_ids,
                "status": "verified_in_finite_scope",
            }
        ],
        "configuration": {
            "checkpoint_interval": first.config.checkpoint_interval,
            "search_limits": {
                "max_nodes_per_check": first.config.limits_per_check.max_nodes,
                "timeout_seconds_per_check": first.config.limits_per_check.timeout_seconds,
            },
        },
        "created_utc": config.created_utc,
        "generator": {
            "executable": first.generator.executable,
            "name": "nauty-geng",
            "sha256": first.generator.sha256,
        },
        "limitations": claim_limitations,
        "producer": {
            "commit": config.code_commit,
            "distribution_version": first.toolkit.distribution_version,
            "python_implementation": first.toolkit.python_implementation,
            "python_version": first.toolkit.python_version,
            "repository": config.code_repository,
            "source_sha256": first.toolkit.source_sha256,
        },
        "replay_archive": {
            "bytes": archive_bytes,
            "external_artifact": config.external_artifact.as_posix(),
            "media_type": ARCHIVE_MEDIA_TYPE,
            "sha256": archive_sha256,
            "url": config.external_url,
        },
        "runs": run_items,
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "scope": {
            "connected": False,
            "fix_distinguished_colors": True,
            "graph_family": "finite_simple_unlabeled_graphs",
            "max_degree": None,
            "min_degree": None,
            "objective": UNIVERSAL_OBJECTIVE,
            "partition_enumerator": PARTITION_ENUMERATOR_ID,
            "require_high_degree": first.config.require_high_degree,
        },
        "summary_id": config.summary_id,
        "totals": totals,
    }


def _canonical_schema_digest(data: bytes) -> str:
    try:
        value = strict_json_loads(data)
    except ValueError as exc:
        raise UniversalReleaseError(f"packaged release schema is invalid JSON: {exc}") from exc
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def release_schema_digests() -> dict[str, str]:
    """Return canonical JSON trust pins for the two data-release schemas."""

    return {
        DATASET_MANIFEST_SCHEMA_PATH.as_posix(): _canonical_schema_digest(
            read_schema_bytes(DATASET_MANIFEST_SCHEMA_NAME)
        ),
        UNIVERSAL_SUMMARY_SCHEMA_PATH.as_posix(): _canonical_schema_digest(
            read_schema_bytes(UNIVERSAL_SUMMARY_SCHEMA_NAME)
        ),
    }


def _manifest(
    config: UniversalReleaseConfig,
    summary_path: Path,
    summary_relative: PurePosixPath,
    archive_bytes: int,
    archive_sha256: str,
) -> dict[str, object]:
    summary_digest = _sha256(summary_path)
    return {
        "$schema": DATASET_MANIFEST_SCHEMA_PATH.as_posix(),
        "artifacts": [
            {
                "bytes": summary_path.stat().st_size,
                "description": (
                    "Reviewed finite-scope universal auxiliary-extension census summary."
                ),
                "media_type": "application/json",
                "path": summary_relative.as_posix(),
                "records": 1,
                "role": "result",
                "schema": UNIVERSAL_SUMMARY_SCHEMA_PATH.as_posix(),
                "sha256": summary_digest,
            }
        ],
        "dataset": {
            "id": DEFAULT_DATASET_ID,
            "license": DEFAULT_DATASET_LICENSE,
            "repository": config.dataset_repository,
            "title": DEFAULT_DATASET_TITLE,
        },
        "external_artifacts": [
            {
                "bytes": archive_bytes,
                "description": (
                    "Deterministic replay archive containing complete universal run transcripts."
                ),
                "media_type": ARCHIVE_MEDIA_TYPE,
                "name": config.external_artifact.as_posix(),
                "sha256": archive_sha256,
                "url": config.external_url,
            }
        ],
        "managed_roots": ["reports", "results"],
        "release": {
            "code_commit": config.code_commit,
            "code_repository": config.code_repository,
            "created_utc": config.created_utc,
            "status": config.release_status,
            "version": config.release_version,
        },
        "schema_version": MANIFEST_SCHEMA_VERSION,
    }


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_bundle(
    root: Path,
    config: UniversalReleaseConfig,
    summary: Mapping[str, object],
    summary_relative: PurePosixPath,
    archive_bytes: int,
    archive_sha256: str,
) -> None:
    (root / "reports").mkdir(parents=True)
    (root / "results").mkdir()
    (root / "schemas").mkdir()
    (root / "manifests").mkdir()
    for schema_name in (DATASET_MANIFEST_SCHEMA_NAME, UNIVERSAL_SUMMARY_SCHEMA_NAME):
        _write_file(root / "schemas" / schema_name, read_schema_bytes(schema_name))
    summary_path = root.joinpath(*summary_relative.parts)
    _write_file(summary_path, canonical_json_bytes(summary) + b"\n")
    manifest = _manifest(
        config,
        summary_path,
        summary_relative,
        archive_bytes,
        archive_sha256,
    )
    _write_file(root / "manifests/dataset-manifest.json", canonical_json_bytes(manifest) + b"\n")
    _write_file(
        root / "SHA256SUMS",
        f"{_sha256(summary_path)}  {summary_relative.as_posix()}\n".encode("ascii"),
    )
    for directory in (root / "reports", root / "results", root / "schemas", root / "manifests"):
        _fsync_directory(directory)
    _fsync_directory(root)


def _export_universal_release_from_snapshots(
    snapshots: _RunSnapshots,
    config: UniversalReleaseConfig,
    *,
    executable: str = "geng",
) -> UniversalReleaseResult:
    """Build one release using only already isolated run snapshots."""

    run_directories = snapshots.paths
    if not isinstance(config, UniversalReleaseConfig):
        raise ValueError("config must be UniversalReleaseConfig")
    if not run_directories:
        raise UniversalReleaseError("at least one run directory is required")
    if config.bundle_root.exists() or config.bundle_root.is_symlink():
        raise UniversalReleaseError(f"refusing to overwrite bundle path: {config.bundle_root}")
    if config.archive_path.exists() or config.archive_path.is_symlink():
        raise UniversalReleaseError(f"refusing to overwrite archive path: {config.archive_path}")
    for parent, label in (
        (config.bundle_root.parent, "bundle"),
        (config.archive_path.parent, "archive"),
    ):
        if parent.is_symlink() or not parent.is_dir():
            raise UniversalReleaseError(f"{label} output parent must be a real directory: {parent}")

    snapshots.assert_unchanged()
    validations: list[UniversalCensusValidation] = []
    for directory in run_directories:
        try:
            validations.append(
                validate_completed_universal_census(directory, executable=executable)
            )
        except (CensusFormatError, OSError, ValueError) as exc:
            raise UniversalReleaseError(
                f"completed run validation failed for {directory}: {exc}"
            ) from exc
    validations.sort(key=lambda item: item.config.geng.order)
    snapshots.assert_unchanged()
    _uniform_run_contract(validations)
    first = validations[0]
    if (
        config.expected_toolkit_source_sha256 is not None
        and first.toolkit.source_sha256 != config.expected_toolkit_source_sha256
    ):
        raise UniversalReleaseError("completed runs do not match expected toolkit source SHA-256")
    if (
        config.expected_generator_sha256 is not None
        and first.generator.sha256 != config.expected_generator_sha256
    ):
        raise UniversalReleaseError("completed runs do not match expected geng SHA-256")
    exports = _make_exports(validations)
    snapshots.assert_unchanged()
    release_counts = _sum_counts(exports)
    release_partitions = sum(export.validation.result.partition_count for export in exports)
    release_evaluations = release_partitions * len(first.config.checks)
    if (
        release_counts.candidate_unsat
        or release_counts.unknown
        or release_counts.error
        or release_counts.verified_all <= 0
        or release_partitions <= 0
        or release_evaluations <= 0
    ):
        raise UniversalReleaseError(
            "v1 public release requires positive verified evidence and zero adverse statuses"
        )
    sources = _archive_sources(exports)
    summary_relative = release_summary_path(tuple(export.order for export in exports))

    with (
        _open_output_targets(config.bundle_root, config.archive_path) as (
            bundle_target,
            archive_target,
        ),
        _cooperative_output_locks((bundle_target, archive_target)),
    ):
        archive_staging_name = _unique_staging_name(archive_target, kind="archive")
        bundle_staging_name = _unique_staging_name(bundle_target, kind="bundle")
        archive_descriptor = -1
        bundle_descriptor = -1
        archive_identity: _EntryIdentity | None = None
        bundle_identity: _EntryIdentity | None = None
        archive_installed = False
        bundle_installed = False
        cleanup_errors: list[str] = []
        try:
            archive_staging_path = (
                Path(f"/proc/self/fd/{archive_target.parent_descriptor}") / archive_staging_name
            )
            snapshots.assert_unchanged()
            _write_deterministic_archive(archive_staging_path, sources)
            snapshots.assert_unchanged()
            archive_descriptor = os.open(
                archive_staging_name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=archive_target.parent_descriptor,
            )
            archive_file_identity = _FileIdentity.from_stat(os.fstat(archive_descriptor))
            if archive_file_identity.mode != stat.S_IFREG:
                raise UniversalReleaseError("staged replay archive is not a regular file")
            archive_identity = _EntryIdentity.from_stat(os.fstat(archive_descriptor))
            _validate_replay_archive_structure_fd(
                archive_descriptor, (receipt for receipt, _source in sources)
            )
            archive_bytes = archive_file_identity.size
            archive_sha256 = _sha256_fd(archive_descriptor)
            summary = _summary(config, exports, archive_bytes, archive_sha256)
            _validate_replay_archive_fd(
                archive_descriptor,
                archive_file_identity,
                summary,
                executable=executable,
            )
            snapshots.assert_unchanged()

            os.mkdir(
                bundle_staging_name,
                0o700,
                dir_fd=bundle_target.parent_descriptor,
            )
            bundle_descriptor = os.open(
                bundle_staging_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=bundle_target.parent_descriptor,
            )
            os.fchmod(bundle_descriptor, 0o700)
            bundle_identity = _EntryIdentity.from_stat(os.fstat(bundle_descriptor))
            bundle_staging_path = Path(f"/proc/self/fd/{bundle_descriptor}")
            _write_bundle(
                bundle_staging_path,
                config,
                summary,
                summary_relative,
                archive_bytes,
                archive_sha256,
            )
            os.fsync(bundle_descriptor)
            snapshots.assert_unchanged()

            totals = summary["totals"]
            if not isinstance(totals, dict):  # pragma: no cover - constructed invariant
                raise UniversalReleaseError("constructed summary totals are not an object")
            result = UniversalReleaseResult(
                bundle_root=config.bundle_root,
                archive_path=config.archive_path,
                summary_path=config.bundle_root.joinpath(*summary_relative.parts),
                manifest_path=config.bundle_root / "manifests/dataset-manifest.json",
                archive_bytes=archive_bytes,
                archive_sha256=archive_sha256,
                orders=tuple(export.order for export in exports),
                totals=totals,
            )

            _install_output_noreplace(
                archive_target,
                staging_name=archive_staging_name,
                expected=archive_identity,
            )
            archive_installed = True
            os.fsync(archive_target.parent_descriptor)
            _install_output_noreplace(
                bundle_target,
                staging_name=bundle_staging_name,
                expected=bundle_identity,
            )
            bundle_installed = True
            os.fsync(bundle_target.parent_descriptor)
            return result
        except BaseException as original:
            rollback_errors: list[str] = []
            if bundle_installed and bundle_identity is not None:
                error = _rollback_installed_output(
                    bundle_target,
                    staging_name=bundle_staging_name,
                    expected=bundle_identity,
                )
                if error is not None:
                    rollback_errors.append(error)
            if archive_installed and archive_identity is not None:
                error = _rollback_installed_output(
                    archive_target,
                    staging_name=archive_staging_name,
                    expected=archive_identity,
                )
                if error is not None:
                    rollback_errors.append(error)
            if rollback_errors:
                raise UniversalReleaseError(
                    f"release transaction failed ({original}); rollback incomplete: "
                    + "; ".join(rollback_errors)
                ) from original
            raise
        finally:
            if archive_descriptor >= 0:
                os.close(archive_descriptor)
            if bundle_descriptor >= 0:
                os.close(bundle_descriptor)
            if archive_identity is not None:
                error = _cleanup_owned_staging(
                    archive_target,
                    archive_staging_name,
                    archive_identity,
                )
                if error is not None:
                    cleanup_errors.append(error)
            if bundle_identity is not None:
                error = _cleanup_owned_staging(
                    bundle_target,
                    bundle_staging_name,
                    bundle_identity,
                )
                if error is not None:
                    cleanup_errors.append(error)
            if cleanup_errors:
                raise UniversalReleaseError(
                    "release staging cleanup failed: " + "; ".join(cleanup_errors)
                )


def export_universal_release(
    run_directories: Sequence[str | Path],
    config: UniversalReleaseConfig,
    *,
    executable: str = "geng",
) -> UniversalReleaseResult:
    """Snapshot, replay, and publish a compact bundle plus replay archive."""

    if not isinstance(config, UniversalReleaseConfig):
        raise ValueError("config must be UniversalReleaseConfig")
    if not run_directories:
        raise UniversalReleaseError("at least one run directory is required")
    if len(run_directories) > _MAX_RELEASE_RUNS:
        raise UniversalReleaseError("public v1 summaries support at most 256 order runs")
    if config.bundle_root.exists() or config.bundle_root.is_symlink():
        raise UniversalReleaseError(f"refusing to overwrite bundle path: {config.bundle_root}")
    if config.archive_path.exists() or config.archive_path.is_symlink():
        raise UniversalReleaseError(f"refusing to overwrite archive path: {config.archive_path}")
    for parent, label in (
        (config.bundle_root.parent, "bundle"),
        (config.archive_path.parent, "archive"),
    ):
        if parent.is_symlink() or not parent.is_dir():
            raise UniversalReleaseError(f"{label} output parent must be a real directory: {parent}")
    with _open_output_targets(config.bundle_root, config.archive_path):
        pass
    with _private_run_snapshots(run_directories, parent=config.archive_path.parent) as snapshots:
        return _export_universal_release_from_snapshots(
            snapshots,
            config,
            executable=executable,
        )


__all__ = [
    "ARCHIVE_MEDIA_TYPE",
    "DATASET_MANIFEST_SCHEMA_NAME",
    "DATASET_MANIFEST_SCHEMA_PATH",
    "DEFAULT_LIMITATIONS",
    "SUMMARY_SCHEMA_VERSION",
    "UNIVERSAL_SUMMARY_SCHEMA_NAME",
    "UNIVERSAL_SUMMARY_SCHEMA_PATH",
    "ArchiveMemberReceipt",
    "UniversalReleaseConfig",
    "UniversalReleaseError",
    "UniversalReleaseResult",
    "canonical_finite_scope",
    "export_universal_release",
    "is_stable_https_url",
    "release_schema_digests",
    "release_summary_path",
    "validate_replay_archive",
]
