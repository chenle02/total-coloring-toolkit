"""Fail-closed promotion of a verified, immutable bundle into a public data repo.

The library is deliberately local-only. It never stages Git changes, commits,
pushes, creates releases, or invokes a shell. ``promote`` is a dry run unless
the caller explicitly passes ``apply=True``. Individual replacements and
rollback are durable, but a multi-file filesystem update cannot be crash-atomic;
published artifact paths are therefore immutable and should be versioned.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlsplit

from total_coloring.graph import canonical_json_bytes, strict_json_loads
from total_coloring.universal_release import (
    DEFAULT_LIMITATIONS,
    ArchiveMemberReceipt,
    canonical_finite_scope,
    is_stable_https_url,
    validate_replay_archive,
)

_SUPPORTED_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
_DEFAULT_MANIFEST_PATH = PurePosixPath("manifests/dataset-manifest.json")
_DEFAULT_CHECKSUMS_PATH = PurePosixPath("SHA256SUMS")
_TRUSTED_MANIFEST_V1_SCHEMA_PATH = PurePosixPath("schemas/dataset-manifest-v1.schema.json")
_TRUSTED_MANIFEST_V2_SCHEMA_PATH = PurePosixPath("schemas/dataset-manifest-v2.schema.json")
_TRUSTED_RESULT_V1_SCHEMA_PATH = PurePosixPath("schemas/result-v1.schema.json")
_TRUSTED_UNIVERSAL_SUMMARY_SCHEMA_PATH = PurePosixPath(
    "schemas/universal-census-summary-v1.schema.json"
)
_DEFAULT_DATASET_ID = "total-coloring-data"
_DEFAULT_DATASET_REPOSITORY = "https://github.com/chenle02/total-coloring-data"
_DEFAULT_CODE_REPOSITORY = "https://github.com/chenle02/total-coloring-toolkit"
_DEFAULT_DATASET_LICENSE = "CC-BY-4.0"
_DEFAULT_MANAGED_ROOTS = (PurePosixPath("reports"), PurePosixPath("results"))
_MAX_BUNDLE_JSON_BYTES = 16 * 1024 * 1024
_MAX_CHECKSUM_FILE_BYTES = 4 * 1024 * 1024
_MAX_CHECKSUM_LINE_BYTES = 4096
_MAX_SCHEMA_DIAGNOSTICS = 100
_RELEASE_STATUSES = ("development", "candidate", "published")
_SHA1_PATTERN = re.compile(r"[0-9a-f]{40}")
_CANONICAL_UTC_PATTERN = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z"
)
_SEMVER_PATTERN = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
)
# SHA-256 of the canonical JSON representation (sorted keys, compact separators)
# of schemas/dataset-manifest-v1.schema.json. This trusted value is updated only
# together with the verifier, schema, and adversarial schema-pinning tests.
_TRUSTED_MANIFEST_SCHEMA_DIGESTS = {
    _TRUSTED_MANIFEST_V1_SCHEMA_PATH: (
        "f8adbf0081e768a1e15d2f88f249afd1c0eb422e4ebfd4ec840fb28e50b400e2"
    ),
    # Updated only together with the final data-repository schema and adversarial tests.
    _TRUSTED_MANIFEST_V2_SCHEMA_PATH: (
        "60351bf5daeda4d119678896cbe2a5771d451aaf279c4ae12f9f99dfd4c657fd"
    ),
}
_TRUSTED_RESULT_SCHEMA_DIGESTS = {
    _TRUSTED_RESULT_V1_SCHEMA_PATH: (
        "56acf75e9d41a64d1c2bf8d2e2651cb12a7fdefe7eac0ed55397dc231e36139a"
    ),
    # Updated only together with the final data-repository schema and adversarial tests.
    _TRUSTED_UNIVERSAL_SUMMARY_SCHEMA_PATH: (
        "0a32e047fa967f9d4bc87c2ee433d9e8af9095864920b37791b1aef171d675fd"
    ),
}
_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "additionalProperties",
        "required",
        "properties",
        "items",
        "const",
        "enum",
        "pattern",
        "format",
        "minLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
    }
)


class PublishingError(RuntimeError):
    """Base class for publication failures."""


class BundleVerificationError(PublishingError):
    """The source or staged release bundle violates its data contract."""


class RepositoryStateError(PublishingError):
    """The destination Git repository is not safe to modify."""


class ConcurrentModificationError(PublishingError):
    """Source or destination bytes changed after a plan was constructed."""


@dataclass(frozen=True, slots=True)
class ExternalArtifactFile:
    """Caller-supplied offline bytes for one manifest external artifact."""

    name: PurePosixPath
    path: Path

    def __post_init__(self) -> None:
        if _safe_relative_path(self.name.as_posix()) != self.name:
            raise ValueError("external artifact name must be a safe normalized relative path")
        if any(part.startswith(".") for part in self.name.parts):
            raise ValueError("external artifact name may not contain hidden components")


@dataclass(frozen=True, slots=True)
class PublicationConfig:
    """Paths and exact dirty-file exceptions for one promotion.

    ``allowed_dirty_paths`` tolerates known destination changes. A dirty target
    is accepted only when its bytes already equal the inspected source bundle,
    which supports an explicit idempotent retry after an uncommitted promotion.

    The expected dataset and code repository fields are trusted local policy,
    independent of the candidate-controlled manifest. ``expected_code_commit``
    binds a promotion to the generating toolkit commit when supplied.
    """

    source_root: Path
    destination_root: Path
    manifest_path: PurePosixPath = _DEFAULT_MANIFEST_PATH
    checksums_path: PurePosixPath = _DEFAULT_CHECKSUMS_PATH
    allowed_dirty_paths: tuple[PurePosixPath, ...] = ()
    expected_dataset_id: str = _DEFAULT_DATASET_ID
    expected_dataset_repository: str = _DEFAULT_DATASET_REPOSITORY
    expected_code_repository: str = _DEFAULT_CODE_REPOSITORY
    expected_license: str = _DEFAULT_DATASET_LICENSE
    expected_managed_roots: tuple[PurePosixPath, ...] = _DEFAULT_MANAGED_ROOTS
    expected_code_commit: str | None = None
    geng_executable: str = "geng"
    external_files: tuple[ExternalArtifactFile, ...] = ()

    def __post_init__(self) -> None:
        for label, path in (
            ("manifest_path", self.manifest_path),
            ("checksums_path", self.checksums_path),
        ):
            if _safe_relative_path(str(path)) is None:
                raise ValueError(f"{label} must be a normalized safe relative path")
        normalized_allowed = tuple(sorted(set(self.allowed_dirty_paths), key=str))
        if normalized_allowed != self.allowed_dirty_paths:
            raise ValueError("allowed_dirty_paths must be unique and path-sorted")
        if any(_safe_relative_path(str(path)) is None for path in self.allowed_dirty_paths):
            raise ValueError("allowed_dirty_paths must contain normalized safe relative paths")
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.expected_dataset_id) is None:
            raise ValueError("expected_dataset_id must be a lowercase hyphenated identifier")
        try:
            repository = urlsplit(self.expected_dataset_repository)
        except ValueError as exc:
            raise ValueError("expected_dataset_repository must be an HTTP(S) URI") from exc
        if repository.scheme not in {"http", "https"} or not repository.netloc:
            raise ValueError("expected_dataset_repository must be an HTTP(S) URI")
        try:
            code_repository = urlsplit(self.expected_code_repository)
        except ValueError as exc:
            raise ValueError("expected_code_repository must be an HTTP(S) URI") from exc
        if code_repository.scheme not in {"http", "https"} or not code_repository.netloc:
            raise ValueError("expected_code_repository must be an HTTP(S) URI")
        if not self.expected_license:
            raise ValueError("expected_license must be nonempty")
        normalized_roots = tuple(sorted(set(self.expected_managed_roots), key=str))
        if normalized_roots != self.expected_managed_roots:
            raise ValueError("expected_managed_roots must be unique and path-sorted")
        if any(
            _safe_relative_path(str(path)) is None or len(path.parts) != 1
            for path in self.expected_managed_roots
        ):
            raise ValueError("expected_managed_roots must be simple safe directory names")
        if self.expected_code_commit is not None and not _valid_commit(self.expected_code_commit):
            raise ValueError("expected_code_commit must be a nonzero lowercase 40-hex SHA")
        if (
            not isinstance(self.geng_executable, str)
            or not self.geng_executable
            or "\x00" in self.geng_executable
        ):
            raise ValueError("geng_executable must be a nonempty command or path")
        if not all(isinstance(item, ExternalArtifactFile) for item in self.external_files):
            raise ValueError("external_files must contain ExternalArtifactFile values")
        external_names = tuple(item.name for item in self.external_files)
        if external_names != tuple(sorted(set(external_names), key=str)):
            raise ValueError("external_files must have unique name-sorted entries")


@dataclass(frozen=True, slots=True)
class PublicationFile:
    path: PurePosixPath
    kind: str
    bytes: int
    sha256: str
    destination_sha256: str | None

    @property
    def changed(self) -> bool:
        return self.sha256 != self.destination_sha256


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    config: PublicationConfig
    files: tuple[PublicationFile, ...]
    destination_head: str
    tolerated_dirty_paths: tuple[PurePosixPath, ...]
    plan_digest: str

    @property
    def changed_paths(self) -> tuple[PurePosixPath, ...]:
        return tuple(item.path for item in self.files if item.changed)


@dataclass(frozen=True, slots=True)
class PublicationResult:
    plan: PublicationPlan
    applied: bool

    @property
    def changed_paths(self) -> tuple[PurePosixPath, ...]:
        return self.plan.changed_paths


@dataclass(frozen=True, slots=True)
class _Bundle:
    manifest: dict[str, Any]
    dataset_id: str
    dataset_repository: str
    dataset_license: str
    release_version: str
    release_status: str
    code_repository: str
    code_commit: str
    managed_roots: tuple[PurePosixPath, ...]
    files: tuple[tuple[PurePosixPath, str], ...]


@dataclass(frozen=True, slots=True)
class _EntryIdentity:
    dev: int
    ino: int
    kind: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _EntryIdentity:
        return cls(value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    entry: _EntryIdentity
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileIdentity:
        return cls(
            entry=_EntryIdentity.from_stat(value),
            mode=value.st_mode,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
        )


@dataclass(slots=True)
class _PinnedDirectory:
    descriptor: int
    identity: _EntryIdentity
    parent_descriptor: int
    name: str
    relative: PurePosixPath | None
    created: bool


@dataclass(slots=True)
class _PreparedPublication:
    item: PublicationFile
    parent: _PinnedDirectory
    leaf: str
    stage_name: str
    stage_descriptor: int
    stage_identity: _EntryIdentity
    original_descriptor: int = -1
    original_identity: _FileIdentity | None = None
    displaced_identity: _EntryIdentity | None = None
    installed: bool = False


_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


def _safe_relative_path(value: str) -> PurePosixPath | None:
    if not value or "\\" in value or any(ord(character) < 32 for character in value):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _valid_commit(value: str) -> bool:
    return _SHA1_PATTERN.fullmatch(value) is not None and value != "0" * 40


def _parse_semver(value: str) -> tuple[tuple[str, str, str], tuple[str, ...] | None]:
    match = _SEMVER_PATTERN.fullmatch(value)
    if match is None:
        raise BundleVerificationError(f"release.version is not canonical SemVer: {value!r}")
    major, minor, patch, prerelease = match.groups()
    identifiers = tuple(prerelease.split(".")) if prerelease is not None else None
    return (major, minor, patch), identifiers


def _compare_numeric_identifier(left: str, right: str) -> int:
    if len(left) != len(right):
        return -1 if len(left) < len(right) else 1
    if left == right:
        return 0
    return -1 if left < right else 1


def _compare_semver(left: str, right: str) -> int:
    left_core, left_pre = _parse_semver(left)
    right_core, right_pre = _parse_semver(right)
    for left_item, right_item in zip(left_core, right_core, strict=True):
        comparison = _compare_numeric_identifier(left_item, right_item)
        if comparison:
            return comparison
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1
    for left_item, right_item in zip(left_pre, right_pre, strict=False):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return _compare_numeric_identifier(left_item, right_item)
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


def _canonical_utc(value: str) -> bool:
    if _CANONICAL_UTC_PATTERN.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return parsed.utcoffset() == UTC.utcoffset(parsed)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, label: str) -> Any:
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_BUNDLE_JSON_BYTES + 1)
        if len(data) > _MAX_BUNDLE_JSON_BYTES:
            raise ValueError(f"JSON exceeds {_MAX_BUNDLE_JSON_BYTES} bytes")
        return strict_json_loads(data, max_bytes=_MAX_BUNDLE_JSON_BYTES)
    except (OSError, UnicodeError, ValueError) as error:
        raise BundleVerificationError(f"{label}: cannot read JSON: {error}") from error


def _regular_file(root: Path, relative: PurePosixPath, label: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BundleVerificationError(f"{label}: symlink is forbidden: {relative}")
    if not candidate.is_file():
        raise BundleVerificationError(f"{label}: regular file not found: {relative}")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise BundleVerificationError(f"{label}: path escapes bundle: {relative}") from error
    return candidate


def _external_regular_file(path: Path, label: str) -> Path:
    candidate = path.expanduser().absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise BundleVerificationError(f"{label}: symlink path component is forbidden")
    if not candidate.is_file():
        raise BundleVerificationError(f"{label}: regular file not found: {candidate}")
    return candidate


def _type_matches(instance: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, int | float) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    raise BundleVerificationError(f"unsupported JSON Schema type {expected!r}")


def _format_matches(value: str, format_name: str) -> bool:
    if format_name == "sha256":
        return re.fullmatch(r"[0-9a-f]{64}", value) is not None
    if format_name == "relative-path":
        return _safe_relative_path(value) is not None
    if format_name == "uri":
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    if format_name == "https-uri":
        return is_stable_https_url(value)
    if format_name == "date-time":
        try:
            parsed_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed_time.tzinfo is not None
    if format_name == "utc-date-time":
        return _canonical_utc(value)
    raise BundleVerificationError(f"unsupported JSON Schema format {format_name!r}")


class _SchemaErrors(list[str]):
    """Bound schema diagnostics and signal validators to stop recursive work."""

    __slots__ = ("truncated",)

    def __init__(self) -> None:
        super().__init__()
        self.truncated = False

    def at_limit(self) -> bool:
        return self.truncated

    def append(self, error: str) -> None:
        if self.truncated:
            return
        if len(self) >= _MAX_SCHEMA_DIAGNOSTICS - 1:
            super().append(f"schema diagnostics capped at {_MAX_SCHEMA_DIAGNOSTICS} messages")
            self.truncated = True
            return
        super().append(error)

    def extend(self, errors: Iterable[str]) -> None:
        for error in errors:
            self.append(error)
            if self.at_limit():
                return


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int/float equality coercions."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


def _check_schema_definition(schema: Any, location: str, errors: _SchemaErrors) -> None:
    if errors.at_limit():
        return
    if not isinstance(schema, dict):
        errors.append(f"{location}: schema node must be an object")
        return
    unsupported = sorted(set(schema) - _SUPPORTED_SCHEMA_KEYS)
    if unsupported:
        errors.append(f"{location}: unsupported schema keyword(s): {', '.join(unsupported)}")
    schema_uri = schema.get("$schema")
    if schema_uri is not None and schema_uri != _SUPPORTED_SCHEMA_URI:
        errors.append(f"{location}: unsupported $schema {schema_uri!r}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        errors.append(f"{location}: properties must be an object")
    else:
        for name, child in properties.items():
            _check_schema_definition(child, f"{location}.properties[{name!r}]", errors)
            if errors.at_limit():
                return
    items = schema.get("items")
    if items is not None:
        _check_schema_definition(items, f"{location}.items", errors)
        if errors.at_limit():
            return
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        errors.append(f"{location}: enum must be a nonempty array")
    for keyword in ("minItems", "maxItems"):
        limit = schema.get(keyword)
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            errors.append(f"{location}: {keyword} must be a nonnegative integer")
    for keyword in ("minimum", "maximum"):
        limit = schema.get(keyword)
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int | float)):
            errors.append(f"{location}: {keyword} must be a JSON number")
    if (
        isinstance(schema.get("minItems"), int)
        and not isinstance(schema.get("minItems"), bool)
        and isinstance(schema.get("maxItems"), int)
        and not isinstance(schema.get("maxItems"), bool)
        and schema["minItems"] > schema["maxItems"]
    ):
        errors.append(f"{location}: minItems may not exceed maxItems")
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if (
        isinstance(minimum, int | float)
        and not isinstance(minimum, bool)
        and isinstance(maximum, int | float)
        and not isinstance(maximum, bool)
        and minimum > maximum
    ):
        errors.append(f"{location}: minimum may not exceed maximum")


def _validate_instance(
    instance: Any,
    schema: dict[str, Any],
    location: str,
    errors: _SchemaErrors,
) -> None:
    if errors.at_limit():
        return
    expected_types = schema.get("type")
    allowed_types: tuple[str, ...]
    if isinstance(expected_types, str):
        allowed_types = (expected_types,)
    elif isinstance(expected_types, list) and all(isinstance(item, str) for item in expected_types):
        allowed_types = tuple(expected_types)
    elif expected_types is None:
        allowed_types = ()
    else:
        errors.append(f"{location}: schema type must be a string or string array")
        return
    if allowed_types and not any(_type_matches(instance, item) for item in allowed_types):
        errors.append(
            f"{location}: expected {' or '.join(allowed_types)}, found {type(instance).__name__}"
        )
        return
    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append(f"{location}: expected constant {schema['const']!r}")
    raw_enum = schema.get("enum")
    if isinstance(raw_enum, list) and not any(_json_equal(instance, item) for item in raw_enum):
        errors.append(f"{location}: value {instance!r} is not permitted")
    if isinstance(instance, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(instance) < minimum_length:
            errors.append(f"{location}: minimum length is {minimum_length}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            errors.append(f"{location}: does not match {pattern!r}")
        format_name = schema.get("format")
        if isinstance(format_name, str) and not _format_matches(instance, format_name):
            errors.append(f"{location}: invalid {format_name}")
    minimum = schema.get("minimum")
    if (
        minimum is not None
        and isinstance(instance, int | float)
        and not isinstance(instance, bool)
        and instance < minimum
    ):
        errors.append(f"{location}: minimum is {minimum}")
    maximum = schema.get("maximum")
    if (
        maximum is not None
        and isinstance(instance, int | float)
        and not isinstance(instance, bool)
        and instance > maximum
    ):
        errors.append(f"{location}: maximum is {maximum}")
    if isinstance(instance, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            errors.append(f"{location}: schema required must be a string array")
            required = []
        errors.extend(
            f"{location}: missing required property {key!r}"
            for key in required
            if key not in instance
        )
        if errors.at_limit():
            return
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{location}.{key}: property is not allowed"
                for key in sorted(set(instance) - set(properties))
            )
            if errors.at_limit():
                return
        for key, child_schema in properties.items():
            if key in instance and isinstance(child_schema, dict):
                _validate_instance(instance[key], child_schema, f"{location}.{key}", errors)
                if errors.at_limit():
                    return
    if isinstance(instance, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(instance) < minimum_items:
            errors.append(f"{location}: minimum item count is {minimum_items}")
        if isinstance(maximum_items, int) and len(instance) > maximum_items:
            errors.append(f"{location}: maximum item count is {maximum_items}")
            return
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate_instance(item, item_schema, f"{location}[{index}]", errors)
                if errors.at_limit():
                    return


def _validate_document(instance: Any, schema: Any, label: str) -> None:
    errors = _SchemaErrors()
    _check_schema_definition(schema, f"{label}:schema", errors)
    if isinstance(schema, dict):
        _validate_instance(instance, schema, label, errors)
    if errors:
        raise BundleVerificationError("schema verification failed:\n  " + "\n  ".join(errors))


def _parse_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with path.open("rb") as handle:
            payload = handle.read(_MAX_CHECKSUM_FILE_BYTES + 1)
        if len(payload) > _MAX_CHECKSUM_FILE_BYTES:
            raise BundleVerificationError(
                f"checksum file {path} exceeds {_MAX_CHECKSUM_FILE_BYTES} bytes"
            )
        lines: list[str] = []
        start = 0
        while start < len(payload):
            newline = payload.find(b"\n", start)
            if newline < 0:
                end = len(payload)
                next_start = end
            else:
                end = newline
                next_start = newline + 1
            raw_line = payload[start:end]
            physical_bytes = next_start - start
            start = next_start
            if physical_bytes > _MAX_CHECKSUM_LINE_BYTES:
                raise BundleVerificationError(
                    f"checksum file {path} contains a physical line exceeding "
                    f"{_MAX_CHECKSUM_LINE_BYTES} bytes including LF"
                )
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            try:
                lines.append(raw_line.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise BundleVerificationError(f"checksum file {path} is not valid UTF-8") from error
    except OSError as error:
        raise BundleVerificationError(f"cannot read checksum file {path}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise BundleVerificationError(f"{path}:{line_number}: expected '<sha256>  <path>'")
        digest, raw_path = match.groups()
        if _safe_relative_path(raw_path) is None:
            raise BundleVerificationError(f"{path}:{line_number}: unsafe checksum path")
        if raw_path in result:
            raise BundleVerificationError(f"{path}:{line_number}: duplicate checksum path")
        result[raw_path] = digest
    if list(result) != sorted(result):
        raise BundleVerificationError(f"{path}: checksum entries must be path-sorted")
    return result


def _managed_files(
    root: Path,
    managed_roots: tuple[PurePosixPath, ...],
    release_status: str,
) -> set[str]:
    result: set[str] = set()
    for relative_root in managed_roots:
        managed = root.joinpath(*relative_root.parts)
        if managed.is_symlink() or not managed.is_dir():
            raise BundleVerificationError(f"managed root must be a real directory: {relative_root}")
        for directory, directory_names, file_names in os.walk(managed, followlinks=False):
            directory_path = Path(directory)
            for name in tuple(directory_names):
                child = directory_path / name
                if name.startswith("."):
                    raise BundleVerificationError(
                        f"hidden managed directory forbidden: {child.relative_to(root)}"
                    )
                if child.is_symlink():
                    raise BundleVerificationError(
                        f"directory symlink forbidden: {child.relative_to(root)}"
                    )
            for name in file_names:
                child = directory_path / name
                relative = child.relative_to(root).as_posix()
                if name.startswith("."):
                    is_root_placeholder = name == ".gitkeep" and directory_path == managed
                    if is_root_placeholder and release_status == "development":
                        continue
                    if is_root_placeholder:
                        raise BundleVerificationError(
                            f"release placeholder forbidden for {release_status}: {relative}"
                        )
                    raise BundleVerificationError(f"hidden managed file forbidden: {relative}")
                if child.is_symlink() or not child.is_file():
                    raise BundleVerificationError(f"managed file is not regular: {relative}")
                result.add(relative)
    return result


def _is_json_media_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    base_type = value.partition(";")[0].strip().lower()
    return base_type == "application/json" or base_type.endswith("+json")


def _validate_result_semantics(
    record: Any,
    label: str,
    *,
    expected_repository: str,
    expected_commit: str,
) -> None:
    records = record if isinstance(record, list) else [record]
    for index, item in enumerate(records):
        location = f"{label}[{index}]" if isinstance(record, list) else label
        if not isinstance(item, dict):
            raise BundleVerificationError(f"{location}: result record must be an object")
        status = item.get("status")
        certificate = item.get("certificate")
        producer = item.get("producer")
        if not isinstance(producer, dict):
            raise BundleVerificationError(f"{location}: producer must be an object")
        if producer.get("repository") != expected_repository:
            raise BundleVerificationError(
                f"{location}: producer.repository must equal release.code_repository"
            )
        if producer.get("commit") != expected_commit:
            raise BundleVerificationError(
                f"{location}: producer.commit must equal release.code_commit"
            )
        if status == "witness":
            if not isinstance(certificate, dict) or not certificate:
                raise BundleVerificationError(
                    f"{location}: witness status requires a nonempty object certificate"
                )
        elif status in {"candidate_unsat", "unknown", "error"}:
            if certificate is not None:
                raise BundleVerificationError(
                    f"{location}: {status} status requires a null certificate"
                )
        else:
            raise BundleVerificationError(f"{location}: unsupported result status {status!r}")


_UNIVERSAL_COUNT_KEYS = (
    "verified_all",
    "candidate_unsat",
    "unknown",
    "error",
    "skipped",
)
_REQUIRED_UNIVERSAL_CHECKS = {
    "dsatur-delta-plus-2": (
        "dsatur-iterative-v1",
        1,
        "Replayable DSATUR witness check with Delta(G)+2 colors for every "
        "canonical equitable partition.",
    ),
    "dsatur-delta-plus-3": (
        "dsatur-iterative-v1",
        2,
        "Replayable DSATUR witness check with Delta(G)+3 colors for every "
        "canonical equitable partition.",
    ),
    "static-delta-plus-2": (
        "static-order-iterative-v1",
        1,
        "Replayable static-order witness check with Delta(G)+2 colors for every "
        "canonical equitable partition.",
    ),
}


def _universal_counts(value: object, location: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(_UNIVERSAL_COUNT_KEYS):
        raise BundleVerificationError(f"{location}: invalid universal status-count object")
    result: dict[str, int] = {}
    for key in _UNIVERSAL_COUNT_KEYS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise BundleVerificationError(f"{location}.{key}: expected nonnegative integer")
        result[key] = item
    return result


def _validate_universal_summary_semantics(
    summary: Any,
    location: str,
    *,
    expected_repository: str,
    expected_commit: str,
    release_created_utc: str,
    external_artifacts: Mapping[str, Mapping[str, object]],
) -> tuple[ArchiveMemberReceipt, ...]:
    """Enforce arithmetic, finite-claim, and archive-receipt bindings."""

    if not isinstance(summary, dict):
        raise BundleVerificationError(f"{location}: universal summary must be an object")
    producer = summary.get("producer")
    if not isinstance(producer, dict):
        raise BundleVerificationError(f"{location}.producer: expected object")
    if producer.get("repository") != expected_repository:
        raise BundleVerificationError(f"{location}: producer.repository does not match release")
    if producer.get("commit") != expected_commit:
        raise BundleVerificationError(f"{location}: producer.commit does not match release")
    if summary.get("created_utc") != release_created_utc:
        raise BundleVerificationError(f"{location}: created_utc does not match release")

    replay = summary.get("replay_archive")
    if not isinstance(replay, dict) or not isinstance(replay.get("external_artifact"), str):
        raise BundleVerificationError(f"{location}.replay_archive: invalid external binding")
    external_name = cast(str, replay["external_artifact"])
    declared_external = external_artifacts.get(external_name)
    if declared_external is None:
        raise BundleVerificationError(f"{location}: replay archive is not externally declared")
    for field in ("url", "media_type", "bytes", "sha256"):
        if replay.get(field) != declared_external.get(field):
            raise BundleVerificationError(
                f"{location}: replay archive {field} does not match external declaration"
            )

    raw_checks = summary.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise BundleVerificationError(f"{location}.checks: expected nonempty array")
    check_ids: list[str] = []
    check_specs: dict[str, tuple[object, object, object]] = {}
    for index, check in enumerate(raw_checks):
        if not isinstance(check, dict) or not isinstance(check.get("check_id"), str):
            raise BundleVerificationError(f"{location}.checks[{index}]: invalid check")
        check_id = cast(str, check["check_id"])
        check_ids.append(check_id)
        check_specs[check_id] = (
            check.get("backend_id"),
            check.get("palette_offset"),
            check.get("description"),
        )
    if check_ids != sorted(set(check_ids)):
        raise BundleVerificationError(f"{location}.checks: IDs must be unique and sorted")
    for check_id, spec in _REQUIRED_UNIVERSAL_CHECKS.items():
        if check_specs.get(check_id) != spec:
            raise BundleVerificationError(f"{location}.checks: missing required {check_id}")

    raw_runs = summary.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise BundleVerificationError(f"{location}.runs: expected nonempty array")
    orders: list[int] = []
    run_by_order: dict[int, dict[str, Any]] = {}
    aggregate_counts = {key: 0 for key in _UNIVERSAL_COUNT_KEYS}
    total_records = 0
    total_partitions = 0
    total_evaluations = 0
    receipts: list[ArchiveMemberReceipt] = []
    for index, run in enumerate(raw_runs):
        run_location = f"{location}.runs[{index}]"
        if not isinstance(run, dict):
            raise BundleVerificationError(f"{run_location}: expected object")
        order = run.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 16:
            raise BundleVerificationError(
                f"{run_location}.order: expected integer between 1 and 16"
            )
        orders.append(order)
        run_by_order[order] = run
        if run.get("generator_arguments") != ["-q", str(order)]:
            raise BundleVerificationError(f"{run_location}: generator arguments are restricted")
        if run.get("shard_index") != 0 or run.get("shard_count") != 1:
            raise BundleVerificationError(f"{run_location}: run is not an unsharded stream")
        counts = _universal_counts(run.get("counts"), f"{run_location}.counts")
        record_count = run.get("record_count")
        partition_count = run.get("partition_count")
        evaluations = run.get("check_evaluations")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (record_count, partition_count, evaluations)
        ):
            raise BundleVerificationError(f"{run_location}: invalid run count")
        assert isinstance(record_count, int)
        assert isinstance(partition_count, int)
        assert isinstance(evaluations, int)
        if sum(counts.values()) != record_count:
            raise BundleVerificationError(f"{run_location}: statuses do not sum to records")
        if evaluations != partition_count * len(raw_checks):
            raise BundleVerificationError(f"{run_location}: incorrect check-evaluation count")
        total_records += record_count
        total_partitions += partition_count
        total_evaluations += evaluations
        for key, value in counts.items():
            aggregate_counts[key] += value
        members = run.get("members")
        if not isinstance(members, dict):
            raise BundleVerificationError(f"{run_location}.members: expected object")
        expected_names = {
            "completion": "completion.json",
            "manifest": "manifest.json",
            "records": "records.jsonl",
        }
        for kind, basename in expected_names.items():
            member = members.get(kind)
            if not isinstance(member, dict):
                raise BundleVerificationError(f"{run_location}.members.{kind}: expected object")
            raw_path = member.get("path")
            relative = _safe_relative_path(raw_path) if isinstance(raw_path, str) else None
            if relative != PurePosixPath(f"order-{order:02d}") / basename:
                raise BundleVerificationError(f"{run_location}: noncanonical {kind} member path")
            member_bytes = member.get("bytes")
            member_digest = member.get("sha256")
            if (
                isinstance(member_bytes, bool)
                or not isinstance(member_bytes, int)
                or member_bytes < 0
                or not isinstance(member_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", member_digest) is None
            ):
                raise BundleVerificationError(f"{run_location}: invalid {kind} receipt")
            receipts.append(ArchiveMemberReceipt(relative, member_bytes, member_digest))
    if orders != sorted(set(orders)):
        raise BundleVerificationError(f"{location}.runs: orders must be unique and sorted")
    if len({run.get("run_fingerprint") for run in raw_runs if isinstance(run, dict)}) != len(
        raw_runs
    ):
        raise BundleVerificationError(f"{location}.runs: fingerprints must be unique")

    totals = summary.get("totals")
    expected_totals = {
        "check_evaluations": total_evaluations,
        "counts": aggregate_counts,
        "order_count": len(raw_runs),
        "partition_count": total_partitions,
        "record_count": total_records,
    }
    if canonical_json_bytes(totals) != canonical_json_bytes(expected_totals):
        raise BundleVerificationError(f"{location}.totals: does not equal sum over runs")

    expected_limitations = list(DEFAULT_LIMITATIONS)
    if summary.get("limitations") != expected_limitations:
        raise BundleVerificationError(f"{location}.limitations: noncanonical limitations")
    claims = summary.get("claims")
    if not isinstance(claims, list) or len(claims) != 1:
        raise BundleVerificationError(f"{location}.claims: expected exactly one finite claim")
    claim_ids: list[str] = []
    for index, claim in enumerate(claims):
        claim_location = f"{location}.claims[{index}]"
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            raise BundleVerificationError(f"{claim_location}: invalid claim")
        claim_ids.append(cast(str, claim["claim_id"]))
        claim_orders = claim.get("orders")
        required_checks = claim.get("required_checks")
        if claim.get("claim_type") != "finite_bound":
            raise BundleVerificationError(f"{claim_location}: expected finite_bound claim")
        if claim.get("status") != "verified_in_finite_scope":
            raise BundleVerificationError(f"{claim_location}: expected verified finite status")
        if claim_orders != orders:
            raise BundleVerificationError(f"{claim_location}: invalid supporting orders")
        if required_checks != sorted(_REQUIRED_UNIVERSAL_CHECKS):
            raise BundleVerificationError(f"{claim_location}: invalid required checks")
        if claim.get("finite_scope") != canonical_finite_scope(orders):
            raise BundleVerificationError(f"{claim_location}: noncanonical finite scope")
        if claim.get("limitations") != expected_limitations:
            raise BundleVerificationError(f"{claim_location}: noncanonical limitations")
        supporting = [run_by_order[order] for order in orders]
        supporting_counts = [
            _universal_counts(run["counts"], f"{claim_location}.counts") for run in supporting
        ]
        if any(
            counts[key]
            for counts in supporting_counts
            for key in ("candidate_unsat", "unknown", "error")
        ):
            raise BundleVerificationError(f"{claim_location}: adverse status forbids verified")
        if (
            sum(counts["verified_all"] for counts in supporting_counts) <= 0
            or sum(cast(int, run["partition_count"]) for run in supporting) <= 0
            or sum(cast(int, run["check_evaluations"]) for run in supporting) <= 0
        ):
            raise BundleVerificationError(f"{claim_location}: verified claim is vacuous")
    if claim_ids != sorted(set(claim_ids)):
        raise BundleVerificationError(f"{location}.claims: IDs must be unique and sorted")
    return tuple(sorted(receipts, key=lambda item: item.path.as_posix()))


def _inspect_bundle(
    config: PublicationConfig,
    root: Path,
    *,
    enforce_expected_commit: bool = True,
    validate_external_files: bool = True,
) -> _Bundle:
    manifest_file = _regular_file(root, config.manifest_path, "manifest")
    manifest = _load_json(manifest_file, "manifest")
    if not isinstance(manifest, dict):
        raise BundleVerificationError("manifest must be a JSON object")
    raw_schema_path = manifest.get("$schema")
    schema_path = _safe_relative_path(raw_schema_path) if isinstance(raw_schema_path, str) else None
    if schema_path is None or not schema_path.parts or schema_path.parts[0] != "schemas":
        raise BundleVerificationError("manifest $schema must be a safe path under schemas/")
    trusted_manifest_digest = _TRUSTED_MANIFEST_SCHEMA_DIGESTS.get(schema_path)
    if trusted_manifest_digest is None:
        raise BundleVerificationError("manifest references an unsupported schema version")
    schema_file = _regular_file(root, schema_path, "manifest schema")
    manifest_schema = _load_json(schema_file, "manifest schema")
    if _canonical_digest(manifest_schema) != trusted_manifest_digest:
        raise BundleVerificationError("manifest schema does not match the trusted canonical schema")
    _validate_document(manifest, manifest_schema, "manifest")

    dataset = manifest.get("dataset")
    if not isinstance(dataset, dict):
        raise BundleVerificationError("manifest dataset must be an object")
    dataset_id = dataset.get("id")
    dataset_repository = dataset.get("repository")
    dataset_license = dataset.get("license")
    if not isinstance(dataset_id, str) or dataset_id != config.expected_dataset_id:
        raise BundleVerificationError(
            f"dataset.id {dataset_id!r} does not match {config.expected_dataset_id!r}"
        )
    if (
        not isinstance(dataset_repository, str)
        or dataset_repository != config.expected_dataset_repository
    ):
        raise BundleVerificationError(
            "dataset.repository does not match the configured public repository"
        )
    if not isinstance(dataset_license, str) or dataset_license != config.expected_license:
        raise BundleVerificationError(
            f"dataset.license {dataset_license!r} does not match {config.expected_license!r}"
        )

    release = manifest.get("release")
    if not isinstance(release, dict):
        raise BundleVerificationError("manifest release must be an object")
    release_version = release.get("version")
    release_status = release.get("status")
    created_utc = release.get("created_utc")
    code_repository = release.get("code_repository")
    code_commit = release.get("code_commit")
    if not isinstance(release_version, str):
        raise BundleVerificationError("release.version must be a string")
    _parse_semver(release_version)
    if not isinstance(release_status, str) or release_status not in _RELEASE_STATUSES:
        raise BundleVerificationError(f"unsupported release.status {release_status!r}")
    if not isinstance(created_utc, str) or not _canonical_utc(created_utc):
        raise BundleVerificationError("release.created_utc must be canonical UTC ending in Z")
    if not isinstance(code_repository, str) or not _format_matches(code_repository, "uri"):
        raise BundleVerificationError("release.code_repository must be an HTTP(S) URI")
    if code_repository != config.expected_code_repository:
        raise BundleVerificationError(
            "release.code_repository does not match the configured public code repository"
        )
    if not isinstance(code_commit, str):
        raise BundleVerificationError("release.code_commit must be a string")
    if release_status in {"candidate", "published"} and not _valid_commit(code_commit):
        raise BundleVerificationError(
            "candidate and published releases require a nonzero exact code commit"
        )
    if (
        release_status == "development"
        and code_commit != "UNSET"
        and not _valid_commit(code_commit)
    ):
        raise BundleVerificationError("development code_commit must be UNSET or a nonzero SHA")
    if (
        enforce_expected_commit
        and config.expected_code_commit is not None
        and code_commit != config.expected_code_commit
    ):
        raise BundleVerificationError(
            "release.code_commit does not match the configured generating commit"
        )

    raw_roots = manifest.get("managed_roots")
    if not isinstance(raw_roots, list) or not all(isinstance(item, str) for item in raw_roots):
        raise BundleVerificationError("managed_roots must be an array of strings")
    managed_roots_list: list[PurePosixPath] = []
    for raw_root in raw_roots:
        relative_root = _safe_relative_path(raw_root)
        if relative_root is None or len(relative_root.parts) != 1:
            raise BundleVerificationError("managed roots must be simple relative directory names")
        managed_roots_list.append(relative_root)
    managed_roots = tuple(managed_roots_list)
    if tuple(sorted(managed_roots, key=str)) != managed_roots or len(set(managed_roots)) != len(
        managed_roots
    ):
        raise BundleVerificationError("managed_roots must be unique and path-sorted")
    if managed_roots != config.expected_managed_roots:
        raise BundleVerificationError(
            "managed_roots do not match the configured repository contract"
        )

    raw_external_artifacts = manifest.get("external_artifacts", [])
    if not isinstance(raw_external_artifacts, list):
        raise BundleVerificationError("external_artifacts must be an array")
    if schema_path == _TRUSTED_MANIFEST_V1_SCHEMA_PATH and raw_external_artifacts:
        raise BundleVerificationError("dataset manifest v1 cannot declare external artifacts")
    external_artifacts: dict[str, Mapping[str, object]] = {}
    external_names: list[str] = []
    external_urls: list[str] = []
    for index, external in enumerate(raw_external_artifacts):
        if not isinstance(external, Mapping):
            raise BundleVerificationError(f"external artifact {index} must be an object")
        raw_name = external.get("name")
        name = _safe_relative_path(raw_name) if isinstance(raw_name, str) else None
        if name is None or any(part.startswith(".") for part in name.parts):
            raise BundleVerificationError(f"external artifact {index} has an unsafe name")
        url = external.get("url")
        if not isinstance(url, str) or not _format_matches(url, "https-uri"):
            raise BundleVerificationError(f"external artifact {index} has an invalid URL")
        external_names.append(name.as_posix())
        external_urls.append(url)
        external_artifacts[name.as_posix()] = external
    if external_names != sorted(set(external_names)):
        raise BundleVerificationError("external artifacts must have unique name-sorted entries")
    if external_urls != sorted(set(external_urls)):
        raise BundleVerificationError("external artifact URLs must be unique and sorted")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise BundleVerificationError("artifacts must be an array")
    artifact_paths: list[PurePosixPath] = []
    expected_checksums: dict[str, str] = {}
    schema_paths: set[PurePosixPath] = {schema_path}
    result_record_locations: dict[str, PurePosixPath] = {}
    universal_summaries: list[tuple[dict[str, Any], PurePosixPath]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise BundleVerificationError(f"artifact {index} must be an object")
        role = artifact.get("role")
        media_type = artifact.get("media_type")
        description = artifact.get("description")
        if role not in {"result", "report", "certificate", "fixture"}:
            raise BundleVerificationError(f"artifact {index} has an invalid role")
        if not isinstance(media_type, str) or not media_type:
            raise BundleVerificationError(f"artifact {index} has an invalid media_type")
        if not isinstance(description, str) or not description:
            raise BundleVerificationError(f"artifact {index} has an invalid description")
        raw_path = artifact.get("path")
        relative = _safe_relative_path(raw_path) if isinstance(raw_path, str) else None
        if (
            relative is None
            or not relative.parts
            or PurePosixPath(relative.parts[0]) not in managed_roots
        ):
            raise BundleVerificationError(f"artifact {index} has an unsafe or unmanaged path")
        if any(part.startswith(".") for part in relative.parts):
            raise BundleVerificationError(f"artifact {index} has a hidden path")
        artifact_paths.append(relative)
        artifact_file = _regular_file(root, relative, f"artifact {index}")
        expected_bytes = artifact.get("bytes")
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool):
            raise BundleVerificationError(f"artifact {index} bytes must be an integer")
        if artifact_file.stat().st_size != expected_bytes:
            raise BundleVerificationError(
                f"artifact {relative}: byte count mismatch "
                f"({artifact_file.stat().st_size} != {expected_bytes})"
            )
        expected_hash = artifact.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        ):
            raise BundleVerificationError(f"artifact {index} has an invalid SHA-256")
        actual_hash = _sha256(artifact_file)
        if actual_hash != expected_hash:
            raise BundleVerificationError(
                f"artifact {relative}: SHA-256 mismatch ({actual_hash} != {expected_hash})"
            )
        expected_checksums[str(relative)] = expected_hash

        raw_record_schema = artifact.get("schema")
        if role == "result":
            if not _is_json_media_type(media_type):
                raise BundleVerificationError(
                    f"artifact {index}: result artifacts require a JSON media type"
                )
            if raw_record_schema not in {
                path.as_posix() for path in _TRUSTED_RESULT_SCHEMA_DIGESTS
            }:
                raise BundleVerificationError(
                    f"artifact {index}: result artifacts must use schemas/result-v1 "
                    "or the trusted universal summary schema"
                )
        if isinstance(raw_record_schema, str):
            record_schema_path = _safe_relative_path(raw_record_schema)
            if (
                record_schema_path is None
                or not record_schema_path.parts
                or record_schema_path.parts[0] != "schemas"
            ):
                raise BundleVerificationError(f"artifact {index} has an unsafe schema path")
            trusted_result_digest = _TRUSTED_RESULT_SCHEMA_DIGESTS.get(record_schema_path)
            if trusted_result_digest is None:
                raise BundleVerificationError(
                    f"artifact {index} references an untrusted record schema"
                )
            schema_paths.add(record_schema_path)
            record_schema_file = _regular_file(root, record_schema_path, f"artifact {index} schema")
            record = _load_json(artifact_file, f"artifact {index}")
            record_schema = _load_json(record_schema_file, f"artifact {index} schema")
            if _canonical_digest(record_schema) != trusted_result_digest:
                raise BundleVerificationError(
                    f"artifact {index} schema does not match the trusted result schema"
                )
            _validate_document(record, record_schema, str(relative))
            if record_schema_path == _TRUSTED_RESULT_V1_SCHEMA_PATH:
                _validate_result_semantics(
                    record,
                    str(relative),
                    expected_repository=config.expected_code_repository,
                    expected_commit=code_commit,
                )
                if isinstance(record, dict) and isinstance(
                    record_id := record.get("record_id"), str
                ):
                    previous = result_record_locations.get(record_id)
                    if previous is not None:
                        raise BundleVerificationError(
                            f"artifact {relative}: duplicate result record_id {record_id!r}; "
                            f"first declared at {previous}"
                        )
                    result_record_locations[record_id] = relative
            elif isinstance(record, dict):
                universal_summaries.append((record, relative))
            declared_records = artifact.get("records")
            if isinstance(declared_records, int) and not isinstance(declared_records, bool):
                actual_records = len(record) if isinstance(record, list) else 1
                if actual_records != declared_records:
                    raise BundleVerificationError(
                        f"artifact {relative}: record count mismatch "
                        f"({actual_records} != {declared_records})"
                    )

    if len(universal_summaries) > 1:
        raise BundleVerificationError("a release may declare at most one universal summary")
    if universal_summaries:
        summary, relative = universal_summaries[0]
        _validate_universal_summary_semantics(
            summary,
            str(relative),
            expected_repository=config.expected_code_repository,
            expected_commit=code_commit,
            release_created_utc=created_utc,
            external_artifacts=external_artifacts,
        )

    supplied_external = {item.name.as_posix(): item.path for item in config.external_files}
    if validate_external_files:
        if set(supplied_external) != set(external_artifacts):
            raise BundleVerificationError(
                "supplied external files must exactly match manifest external artifacts"
            )
        for external_key, metadata in external_artifacts.items():
            external_file = _external_regular_file(
                supplied_external[external_key], f"external {external_key}"
            )
            if external_file.stat().st_size != metadata.get("bytes"):
                raise BundleVerificationError(f"external {external_key}: byte count mismatch")
            if _sha256(external_file) != metadata.get("sha256"):
                raise BundleVerificationError(f"external {external_key}: SHA-256 mismatch")
            if universal_summaries:
                replay = universal_summaries[0][0]["replay_archive"]
                if isinstance(replay, dict) and replay.get("external_artifact") == external_key:
                    try:
                        validate_replay_archive(
                            external_file,
                            universal_summaries[0][0],
                            executable=config.geng_executable,
                        )
                    except RuntimeError as exc:
                        raise BundleVerificationError(
                            f"external {external_key}: invalid replay archive: {exc}"
                        ) from exc

    if artifact_paths != sorted(artifact_paths, key=str) or len(set(artifact_paths)) != len(
        artifact_paths
    ):
        raise BundleVerificationError("artifacts must have unique, path-sorted paths")
    actual_managed = _managed_files(root, managed_roots, release_status)
    if actual_managed != {str(path) for path in artifact_paths}:
        unlisted = sorted(actual_managed - {str(path) for path in artifact_paths})
        missing = sorted({str(path) for path in artifact_paths} - actual_managed)
        raise BundleVerificationError(
            f"managed inventory mismatch: unlisted={unlisted}, missing={missing}"
        )
    checksums_file = _regular_file(root, config.checksums_path, "checksums")
    actual_checksums = _parse_checksums(checksums_file)
    if actual_checksums != expected_checksums:
        raise BundleVerificationError("SHA256SUMS does not match the manifest exactly")

    file_kinds: dict[PurePosixPath, str] = {
        config.manifest_path: "manifest",
        config.checksums_path: "checksums",
    }
    file_kinds.update({path: "schema" for path in schema_paths})
    file_kinds.update({path: "artifact" for path in artifact_paths})
    files = tuple(sorted(file_kinds.items(), key=lambda item: str(item[0])))
    return _Bundle(
        manifest=manifest,
        dataset_id=dataset_id,
        dataset_repository=dataset_repository,
        dataset_license=dataset_license,
        release_version=release_version,
        release_status=release_status,
        code_repository=code_repository,
        code_commit=code_commit,
        managed_roots=managed_roots,
        files=files,
    )


def _run_git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RepositoryStateError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _git_root(root: Path) -> Path:
    raw = _run_git(root, "rev-parse", "--show-toplevel")
    try:
        return Path(raw.decode("utf-8").strip()).resolve(strict=True)
    except (UnicodeError, OSError) as error:
        raise RepositoryStateError("cannot decode or resolve Git repository root") from error


def _git_head(root: Path) -> str:
    head = _run_git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise RepositoryStateError(f"unexpected Git HEAD {head!r}")
    return head


def _git_path(root: Path, name: str) -> Path:
    raw = _run_git(root, "rev-parse", "--git-path", name)
    try:
        decoded = raw.decode("utf-8").strip()
    except UnicodeError as error:
        raise RepositoryStateError("cannot decode Git administrative path") from error
    if not decoded or "\x00" in decoded:
        raise RepositoryStateError("Git returned an invalid administrative path")
    path = Path(decoded)
    if not path.is_absolute():
        path = root / path
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise RepositoryStateError("cannot resolve Git administrative directory") from error
    return parent / path.name


@contextmanager
def _promotion_lock(root: Path) -> Iterator[None]:
    lock_path = _git_path(root, "total-coloring-publish.lock")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as error:
        raise RepositoryStateError(
            f"another promotion is active or left a stale lock: {lock_path}"
        ) from error
    except OSError as error:
        raise RepositoryStateError(f"cannot create promotion lock {lock_path}: {error}") from error
    identity = os.fstat(descriptor)
    try:
        payload = f"pid={os.getpid()}\n".encode("ascii")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        with suppress(OSError):
            current = lock_path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                lock_path.unlink()


def _git_dirty_paths(root: Path) -> tuple[PurePosixPath, ...]:
    raw = _run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    records = raw.split(b"\0")
    result: set[PurePosixPath] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise RepositoryStateError("cannot parse Git porcelain status")
        try:
            status = record[:2].decode("ascii")
            raw_path = record[3:].decode("utf-8")
        except UnicodeError as error:
            raise RepositoryStateError("Git status contains a non-UTF-8 path") from error
        relative = _safe_relative_path(raw_path)
        if relative is None:
            raise RepositoryStateError(f"Git status contains an unsafe path {raw_path!r}")
        result.add(relative)
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise RepositoryStateError("incomplete Git rename/copy status")
            try:
                second_raw_path = records[index].decode("utf-8")
            except UnicodeError as error:
                raise RepositoryStateError("Git status contains a non-UTF-8 path") from error
            index += 1
            second = _safe_relative_path(second_raw_path)
            if second is None:
                raise RepositoryStateError(
                    f"Git status contains an unsafe path {second_raw_path!r}"
                )
            result.add(second)
    return tuple(sorted(result, key=str))


def _destination_digest(path: Path, relative: PurePosixPath) -> str | None:
    candidate = path.joinpath(*relative.parts)
    current = path
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RepositoryStateError(f"destination target contains a symlink: {relative}")
    if not candidate.exists():
        return None
    if not candidate.is_file():
        raise RepositoryStateError(f"destination target is not a regular file: {relative}")
    return _sha256(candidate)


def _normalized_config(config: PublicationConfig) -> PublicationConfig:
    try:
        source_root = config.source_root.resolve(strict=True)
        destination_root = config.destination_root.resolve(strict=True)
    except OSError as error:
        raise PublishingError(f"cannot resolve publication root: {error}") from error
    if not source_root.is_dir() or not destination_root.is_dir():
        raise PublishingError("source_root and destination_root must be directories")
    if source_root == destination_root:
        raise PublishingError("source and destination roots must differ")
    return PublicationConfig(
        source_root=source_root,
        destination_root=destination_root,
        manifest_path=config.manifest_path,
        checksums_path=config.checksums_path,
        allowed_dirty_paths=config.allowed_dirty_paths,
        expected_dataset_id=config.expected_dataset_id,
        expected_dataset_repository=config.expected_dataset_repository,
        expected_code_repository=config.expected_code_repository,
        expected_license=config.expected_license,
        expected_managed_roots=config.expected_managed_roots,
        expected_code_commit=config.expected_code_commit,
        geng_executable=config.geng_executable,
        external_files=config.external_files,
    )


def _existing_destination_bundle(config: PublicationConfig) -> _Bundle | None:
    if _destination_digest(config.destination_root, config.manifest_path) is None:
        return None
    return _inspect_bundle(
        config,
        config.destination_root,
        enforce_expected_commit=False,
        validate_external_files=False,
    )


def _validate_release_transition(source: _Bundle, destination: _Bundle | None) -> None:
    if destination is None:
        return
    if source.code_repository != destination.code_repository:
        raise RepositoryStateError("release.code_repository cannot change between releases")
    comparison = _compare_semver(source.release_version, destination.release_version)
    if comparison < 0:
        raise RepositoryStateError(
            f"release downgrade forbidden: {destination.release_version} -> "
            f"{source.release_version}"
        )
    if comparison == 0:
        source_rank = _RELEASE_STATUSES.index(source.release_status)
        destination_rank = _RELEASE_STATUSES.index(destination.release_status)
        if source_rank < destination_rank:
            raise RepositoryStateError(
                f"release status regression forbidden: {destination.release_status} -> "
                f"{source.release_status}"
            )
        if destination.release_status == "published" and source.manifest != destination.manifest:
            raise RepositoryStateError("a published release version is immutable")


def plan_promotion(config: PublicationConfig) -> PublicationPlan:
    """Validate both sides and construct an immutable, non-writing plan."""

    normalized = _normalized_config(config)
    if _git_root(normalized.destination_root) != normalized.destination_root:
        raise RepositoryStateError("destination_root must be the Git worktree root")
    bundle = _inspect_bundle(normalized, normalized.source_root)
    destination_bundle = _existing_destination_bundle(normalized)
    _validate_release_transition(bundle, destination_bundle)
    dirty_paths = _git_dirty_paths(normalized.destination_root)
    allowed = set(normalized.allowed_dirty_paths)
    unexpected_dirty = sorted(set(dirty_paths) - allowed, key=str)
    if unexpected_dirty:
        raise RepositoryStateError(
            "destination has non-allowlisted changes: "
            + ", ".join(str(path) for path in unexpected_dirty)
        )

    target_paths = {path for path, _kind in bundle.files}
    overlap = sorted(set(dirty_paths) & target_paths, key=str)
    unsafe_overlap = [
        path
        for path in overlap
        if _sha256(_regular_file(normalized.source_root, path, "source target"))
        != _destination_digest(normalized.destination_root, path)
    ]
    if unsafe_overlap:
        raise RepositoryStateError(
            "dirty paths overlap publication targets with different bytes: "
            + ", ".join(str(path) for path in unsafe_overlap)
        )

    destination_managed = _managed_files(
        normalized.destination_root,
        bundle.managed_roots,
        bundle.release_status,
    )
    declared_artifacts = {str(path) for path, kind in bundle.files if kind == "artifact"}
    stale = sorted(destination_managed - declared_artifacts)
    if stale:
        raise RepositoryStateError(
            "destination contains managed artifacts absent from the candidate manifest: "
            + ", ".join(stale)
        )

    for artifact_path in sorted(
        (path for path, kind in bundle.files if kind == "artifact"), key=str
    ):
        source_digest = _sha256(
            _regular_file(normalized.source_root, artifact_path, "source artifact")
        )
        destination_digest = _destination_digest(normalized.destination_root, artifact_path)
        if destination_digest is not None and destination_digest != source_digest:
            raise RepositoryStateError(
                f"immutable artifact path already contains different bytes: {artifact_path}"
            )

    files: list[PublicationFile] = []
    for relative, kind in bundle.files:
        source_file = _regular_file(normalized.source_root, relative, f"source {kind}")
        files.append(
            PublicationFile(
                path=relative,
                kind=kind,
                bytes=source_file.stat().st_size,
                sha256=_sha256(source_file),
                destination_sha256=_destination_digest(normalized.destination_root, relative),
            )
        )
    files_tuple = tuple(files)
    head = _git_head(normalized.destination_root)
    plan_digest = _canonical_digest(
        {
            "destination_head": head,
            "dirty_paths": [str(path) for path in dirty_paths],
            "files": [
                {
                    "path": str(item.path),
                    "kind": item.kind,
                    "bytes": item.bytes,
                    "sha256": item.sha256,
                    "destination_sha256": item.destination_sha256,
                }
                for item in files_tuple
            ],
        }
    )
    return PublicationPlan(
        config=normalized,
        files=files_tuple,
        destination_head=head,
        tolerated_dirty_paths=dirty_paths,
        plan_digest=plan_digest,
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path_or_descriptor: Path | int) -> None:
    if isinstance(path_or_descriptor, int):
        os.fsync(path_or_descriptor)
        return
    descriptor = os.open(path_or_descriptor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _entry_at(descriptor: int, name: str) -> _EntryIdentity | None:
    try:
        return _EntryIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _file_identity_at(descriptor: int, name: str) -> _FileIdentity | None:
    try:
        return _FileIdentity.from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _renameat2(
    *,
    source_directory_descriptor: int,
    source_name: str,
    destination_directory_descriptor: int,
    destination_name: str,
    flags: int,
) -> None:
    """Invoke Linux renameat2, retrying EINTR and failing closed elsewhere."""

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
                flags,
            )
            == 0
        ):
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EINTR:
            continue
        raise OSError(error_number, os.strerror(error_number))


def _rename_noreplace(
    source_directory_descriptor: int,
    source_name: str,
    destination_directory_descriptor: int,
    destination_name: str,
) -> None:
    _renameat2(
        source_directory_descriptor=source_directory_descriptor,
        source_name=source_name,
        destination_directory_descriptor=destination_directory_descriptor,
        destination_name=destination_name,
        flags=_RENAME_NOREPLACE,
    )


def _rename_exchange(
    source_directory_descriptor: int,
    source_name: str,
    destination_directory_descriptor: int,
    destination_name: str,
) -> None:
    _renameat2(
        source_directory_descriptor=source_directory_descriptor,
        source_name=source_name,
        destination_directory_descriptor=destination_directory_descriptor,
        destination_name=destination_name,
        flags=_RENAME_EXCHANGE,
    )


def _translate_rename_error(error: OSError, *, action: str) -> PublishingError:
    if error.errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        return PublishingError(
            f"{action}: Linux renameat2 transaction support is unavailable; failing closed"
        )
    if error.errno == errno.EXDEV:
        return PublishingError(f"{action}: target-local rename crossed filesystems")
    if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
        return ConcurrentModificationError(
            f"{action}: destination appeared concurrently and was preserved"
        )
    return PublishingError(f"{action}: atomic rename failed: {error}")


def _open_absolute_parent_nofollow(path: Path) -> tuple[int, str]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute.name in {"", ".", ".."}:
        raise RepositoryStateError(f"path must name a filesystem entry: {path}")
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
def _pinned_destination_hierarchy(
    root: Path,
) -> Iterator[dict[tuple[str, ...], _PinnedDirectory]]:
    root_parent = -1
    directories: dict[tuple[str, ...], _PinnedDirectory] = {}
    try:
        try:
            root_parent, root_name = _open_absolute_parent_nofollow(root)
            root_descriptor = os.open(
                root_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_parent,
            )
            root_identity = _EntryIdentity.from_stat(os.fstat(root_descriptor))
            if root_identity.kind != stat.S_IFDIR:
                raise RepositoryStateError("destination root is not a real directory")
            directories[()] = _PinnedDirectory(
                descriptor=root_descriptor,
                identity=root_identity,
                parent_descriptor=root_parent,
                name=root_name,
                relative=None,
                created=False,
            )
        except OSError as error:
            raise RepositoryStateError(
                f"cannot pin destination hierarchy without symlinks: {error}"
            ) from error
        yield directories
    finally:
        for directory in reversed(tuple(directories.values())):
            os.close(directory.descriptor)
        if root_parent >= 0:
            os.close(root_parent)


def _assert_directory_binding(directory: _PinnedDirectory) -> None:
    current = _entry_at(directory.parent_descriptor, directory.name)
    if current != directory.identity:
        location = directory.relative or PurePosixPath(".")
        raise ConcurrentModificationError(
            f"destination directory binding changed during promotion: {location}"
        )
    if _EntryIdentity.from_stat(os.fstat(directory.descriptor)) != directory.identity:
        raise ConcurrentModificationError("pinned destination directory identity changed")


def _assert_hierarchy_bindings(
    directories: Mapping[tuple[str, ...], _PinnedDirectory],
) -> None:
    for directory in directories.values():
        _assert_directory_binding(directory)


def _target_parent(
    directories: dict[tuple[str, ...], _PinnedDirectory],
    relative: PurePosixPath,
) -> tuple[_PinnedDirectory, str]:
    parent = directories[()]
    prefix: tuple[str, ...] = ()
    for component in relative.parts[:-1]:
        prefix += (component,)
        existing = directories.get(prefix)
        if existing is not None:
            parent = existing
            continue
        _assert_directory_binding(parent)
        created = False
        try:
            descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent.descriptor,
            )
        except FileNotFoundError:
            try:
                os.mkdir(component, 0o755, dir_fd=parent.descriptor)
            except FileExistsError as error:
                raise ConcurrentModificationError(
                    f"destination directory appeared concurrently: {'/'.join(prefix)}"
                ) from error
            created = True
            descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent.descriptor,
            )
            _fsync_directory(parent.descriptor)
        identity = _EntryIdentity.from_stat(os.fstat(descriptor))
        named = _entry_at(parent.descriptor, component)
        if identity.kind != stat.S_IFDIR or named != identity:
            os.close(descriptor)
            raise ConcurrentModificationError(
                f"destination directory changed while pinning: {'/'.join(prefix)}"
            )
        pinned = _PinnedDirectory(
            descriptor=descriptor,
            identity=identity,
            parent_descriptor=parent.descriptor,
            name=component,
            relative=PurePosixPath(*prefix),
            created=created,
        )
        directories[prefix] = pinned
        parent = pinned
    return parent, relative.name


def _unique_stage_name(parent: _PinnedDirectory, leaf: str) -> str:
    for _attempt in range(100):
        candidate = f".{leaf}.total-coloring-stage-{secrets.token_hex(12)}"
        if _entry_at(parent.descriptor, candidate) is None:
            return candidate
    raise PublishingError(f"cannot allocate a private stage name for {leaf}")


def _copy_local_stage(
    snapshot: Path,
    item: PublicationFile,
    parent: _PinnedDirectory,
    leaf: str,
) -> _PreparedPublication:
    stage_name = _unique_stage_name(parent, leaf)
    source_descriptor = -1
    stage_descriptor = -1
    stage_identity: _EntryIdentity | None = None
    try:
        source_descriptor = os.open(
            snapshot,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        source_before = _FileIdentity.from_stat(os.fstat(source_descriptor))
        if source_before.entry.kind != stat.S_IFREG:
            raise ConcurrentModificationError(f"staged source is not regular: {item.path}")
        stage_descriptor = os.open(
            stage_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent.descriptor,
        )
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(source_descriptor, 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(stage_descriptor, view)
                if written <= 0:  # pragma: no cover - POSIX write invariant
                    raise OSError("short write while staging publication file")
                view = view[written:]
        os.fchmod(stage_descriptor, 0o644)
        os.fsync(stage_descriptor)
        source_after = _FileIdentity.from_stat(os.fstat(source_descriptor))
        if (
            source_after != source_before
            or total != item.bytes
            or digest.hexdigest() != item.sha256
        ):
            raise ConcurrentModificationError(f"source changed while staging {item.path}")
        stage_stat = os.fstat(stage_descriptor)
        stage_identity = _EntryIdentity.from_stat(stage_stat)
        if stage_identity.kind != stat.S_IFREG or stage_stat.st_size != item.bytes:
            raise ConcurrentModificationError(f"local stage is invalid for {item.path}")
        if _entry_at(parent.descriptor, stage_name) != stage_identity:
            raise ConcurrentModificationError(f"local stage was replaced for {item.path}")
        return _PreparedPublication(
            item=item,
            parent=parent,
            leaf=leaf,
            stage_name=stage_name,
            stage_descriptor=stage_descriptor,
            stage_identity=stage_identity,
        )
    except BaseException:
        if stage_descriptor >= 0:
            os.close(stage_descriptor)
            stage_descriptor = -1
        if (
            stage_identity is not None
            and _entry_at(parent.descriptor, stage_name) == stage_identity
        ):
            os.unlink(stage_name, dir_fd=parent.descriptor)
            _fsync_directory(parent.descriptor)
        raise
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)


def _capture_original(prepared: _PreparedPublication) -> None:
    expected = prepared.item.destination_sha256
    current = _entry_at(prepared.parent.descriptor, prepared.leaf)
    if expected is None:
        if current is not None:
            raise ConcurrentModificationError(
                f"destination appeared before installing {prepared.item.path}; preserved"
            )
        return
    if current is None:
        raise ConcurrentModificationError(
            f"destination disappeared before replacing {prepared.item.path}"
        )
    descriptor = os.open(
        prepared.leaf,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=prepared.parent.descriptor,
    )
    try:
        before = _FileIdentity.from_stat(os.fstat(descriptor))
        if before.entry.kind != stat.S_IFREG:
            raise ConcurrentModificationError(
                f"destination is no longer regular: {prepared.item.path}"
            )
        digest = _sha256_descriptor(descriptor)
        after = _FileIdentity.from_stat(os.fstat(descriptor))
        named = _file_identity_at(prepared.parent.descriptor, prepared.leaf)
        if before != after or named != before or digest != expected:
            raise ConcurrentModificationError(
                f"destination changed before replacing {prepared.item.path}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    prepared.original_descriptor = descriptor
    prepared.original_identity = before


def _assert_displaced_original(prepared: _PreparedPublication) -> None:
    original = prepared.original_identity
    if original is None or prepared.original_descriptor < 0:
        raise PublishingError("existing-target transaction is missing its original identity")
    before = _FileIdentity.from_stat(os.fstat(prepared.original_descriptor))
    digest = _sha256_descriptor(prepared.original_descriptor)
    after = _FileIdentity.from_stat(os.fstat(prepared.original_descriptor))
    named = _file_identity_at(prepared.parent.descriptor, prepared.stage_name)
    if (
        before.entry != original.entry
        or before.mode != original.mode
        or before.size != original.size
        or before.mtime_ns != original.mtime_ns
        or after != before
        or named != before
        or digest != prepared.item.destination_sha256
    ):
        raise ConcurrentModificationError(
            f"destination changed during replacement of {prepared.item.path}; restoring it"
        )


def _install_prepared(
    prepared: _PreparedPublication,
    directories: Mapping[tuple[str, ...], _PinnedDirectory],
) -> None:
    _assert_hierarchy_bindings(directories)
    if _entry_at(prepared.parent.descriptor, prepared.stage_name) != prepared.stage_identity:
        raise ConcurrentModificationError(f"local stage changed for {prepared.item.path}")
    _capture_original(prepared)
    try:
        if prepared.original_identity is None:
            _rename_noreplace(
                prepared.parent.descriptor,
                prepared.stage_name,
                prepared.parent.descriptor,
                prepared.leaf,
            )
        else:
            _rename_exchange(
                prepared.parent.descriptor,
                prepared.stage_name,
                prepared.parent.descriptor,
                prepared.leaf,
            )
        prepared.installed = True
    except OSError as error:
        raise _translate_rename_error(error, action=f"install {prepared.item.path}") from error

    final_identity = _entry_at(prepared.parent.descriptor, prepared.leaf)
    if final_identity != prepared.stage_identity:
        raise ConcurrentModificationError(
            f"installed target identity mismatch for {prepared.item.path}"
        )
    if prepared.original_identity is not None:
        prepared.displaced_identity = _entry_at(prepared.parent.descriptor, prepared.stage_name)
        if prepared.displaced_identity is None:
            raise ConcurrentModificationError(
                f"displaced destination vanished for {prepared.item.path}"
            )
        _assert_displaced_original(prepared)
    _fsync_directory(prepared.parent.descriptor)
    _assert_hierarchy_bindings(directories)
    if _entry_at(prepared.parent.descriptor, prepared.leaf) != prepared.stage_identity:
        raise ConcurrentModificationError(
            f"installed target was replaced concurrently: {prepared.item.path}"
        )


def _cleanup_owned_stage(
    prepared: _PreparedPublication,
    expected: _EntryIdentity,
) -> str | None:
    current = _entry_at(prepared.parent.descriptor, prepared.stage_name)
    if current is None:
        return None
    if current != expected:
        return f"{prepared.item.path}: foreign stage replacement preserved"
    try:
        os.unlink(prepared.stage_name, dir_fd=prepared.parent.descriptor)
        _fsync_directory(prepared.parent.descriptor)
    except OSError as error:
        return f"{prepared.item.path}: stage cleanup failed: {error}"
    return None


def _rollback_prepared(prepared: _PreparedPublication) -> str | None:
    if not prepared.installed:
        return _cleanup_owned_stage(prepared, prepared.stage_identity)
    current_final = _entry_at(prepared.parent.descriptor, prepared.leaf)
    if current_final != prepared.stage_identity:
        return f"{prepared.item.path}: foreign final replacement preserved"
    try:
        if prepared.original_identity is None:
            if _entry_at(prepared.parent.descriptor, prepared.stage_name) is not None:
                return f"{prepared.item.path}: foreign rollback destination preserved"
            _rename_noreplace(
                prepared.parent.descriptor,
                prepared.leaf,
                prepared.parent.descriptor,
                prepared.stage_name,
            )
        else:
            displaced = prepared.displaced_identity
            if displaced is None:
                return f"{prepared.item.path}: displaced destination identity is unavailable"
            if _entry_at(prepared.parent.descriptor, prepared.stage_name) != displaced:
                return f"{prepared.item.path}: foreign displaced-file replacement preserved"
            _rename_exchange(
                prepared.parent.descriptor,
                prepared.stage_name,
                prepared.parent.descriptor,
                prepared.leaf,
            )
    except OSError as error:
        return str(_translate_rename_error(error, action=f"rollback {prepared.item.path}"))

    if _entry_at(prepared.parent.descriptor, prepared.stage_name) != prepared.stage_identity:
        return f"{prepared.item.path}: rollback moved an unexpected inode"
    if prepared.original_identity is None:
        if _entry_at(prepared.parent.descriptor, prepared.leaf) is not None:
            return f"{prepared.item.path}: rollback did not restore absence"
    elif _entry_at(prepared.parent.descriptor, prepared.leaf) != prepared.displaced_identity:
        return f"{prepared.item.path}: rollback did not restore the displaced destination"
    try:
        _fsync_directory(prepared.parent.descriptor)
    except OSError as error:
        return f"{prepared.item.path}: rollback directory fsync failed: {error}"
    prepared.installed = False
    return _cleanup_owned_stage(prepared, prepared.stage_identity)


def _discard_displaced_original(prepared: _PreparedPublication) -> str | None:
    if prepared.original_identity is None:
        return None
    try:
        _assert_displaced_original(prepared)
    except PublishingError as error:
        return str(error)
    displaced = prepared.displaced_identity
    if displaced is None:
        return f"{prepared.item.path}: displaced identity is unavailable"
    return _cleanup_owned_stage(prepared, displaced)


def _cleanup_created_directories(
    directories: Mapping[tuple[str, ...], _PinnedDirectory],
) -> list[str]:
    errors: list[str] = []
    for directory in reversed(tuple(directories.values())):
        if not directory.created:
            continue
        current = _entry_at(directory.parent_descriptor, directory.name)
        if current != directory.identity:
            errors.append(f"{directory.relative}: foreign directory replacement preserved")
            continue
        try:
            if os.listdir(directory.descriptor):
                errors.append(f"{directory.relative}: nonempty created directory preserved")
                continue
            os.rmdir(directory.name, dir_fd=directory.parent_descriptor)
            _fsync_directory(directory.parent_descriptor)
        except OSError as error:
            errors.append(f"{directory.relative}: directory cleanup failed: {error}")
    return errors


def _close_prepared(prepared: Iterable[_PreparedPublication]) -> None:
    for target in prepared:
        if target.original_descriptor >= 0:
            os.close(target.original_descriptor)
            target.original_descriptor = -1
        if target.stage_descriptor >= 0:
            os.close(target.stage_descriptor)
            target.stage_descriptor = -1


def _assert_plan_fresh(plan: PublicationPlan) -> PublicationPlan:
    try:
        fresh = plan_promotion(plan.config)
    except PublishingError as error:
        raise ConcurrentModificationError(
            "source bundle or destination repository changed after planning"
        ) from error
    if fresh != plan:
        raise ConcurrentModificationError("publication plan differs from a freshly inspected plan")
    return fresh


def _install_order(item: PublicationFile) -> tuple[int, str]:
    rank = {"schema": 0, "artifact": 1, "checksums": 2, "manifest": 3}
    return (rank[item.kind], str(item.path))


def apply_promotion(plan: PublicationPlan) -> PublicationResult:
    """Apply a fresh plan with atomic file replacement and rollback on failure."""

    with _promotion_lock(plan.config.destination_root):
        fresh = _assert_plan_fresh(plan)
        return _apply_fresh_plan(fresh)


def _apply_fresh_plan(plan: PublicationPlan) -> PublicationResult:
    changed = tuple(item for item in plan.files if item.changed)
    if not changed:
        return PublicationResult(plan=plan, applied=True)

    destination = plan.config.destination_root
    source = plan.config.source_root
    with tempfile.TemporaryDirectory(
        prefix=".total-coloring-publish-", dir=destination.parent
    ) as temporary:
        transaction_root = Path(temporary)
        stage_root = transaction_root / "stage"
        for item in changed:
            source_file = _regular_file(source, item.path, f"source {item.kind}")
            staged_file = stage_root.joinpath(*item.path.parts)
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, staged_file)
            os.chmod(staged_file, 0o644)
            _fsync_file(staged_file)
            if staged_file.stat().st_size != item.bytes or _sha256(staged_file) != item.sha256:
                raise ConcurrentModificationError(f"source changed while staging {item.path}")

        plan = _assert_plan_fresh(plan)
        prepared: list[_PreparedPublication] = []
        with _pinned_destination_hierarchy(destination) as directories:
            try:
                for item in sorted(changed, key=_install_order):
                    parent, leaf = _target_parent(directories, item.path)
                    snapshot = stage_root.joinpath(*item.path.parts)
                    prepared.append(_copy_local_stage(snapshot, item, parent, leaf))

                _assert_hierarchy_bindings(directories)
                for target in prepared:
                    _install_prepared(target, directories)

                _assert_hierarchy_bindings(directories)
                _inspect_bundle(plan.config, destination)
                _assert_hierarchy_bindings(directories)
                for target in prepared:
                    if _entry_at(target.parent.descriptor, target.leaf) != target.stage_identity:
                        raise ConcurrentModificationError(
                            f"installed target changed during verification: {target.item.path}"
                        )
                    if target.original_identity is not None:
                        _assert_displaced_original(target)
                    _fsync_directory(target.parent.descriptor)
            except BaseException as original_error:
                rollback_errors: list[str] = []
                for target in reversed(prepared):
                    error = _rollback_prepared(target)
                    if error is not None:
                        rollback_errors.append(error)
                rollback_errors.extend(_cleanup_created_directories(directories))
                _close_prepared(prepared)
                if rollback_errors:
                    raise PublishingError(
                        f"promotion failed ({original_error}) and rollback was incomplete: "
                        + "; ".join(rollback_errors)
                    ) from original_error
                raise

            # The verified, fsynced outputs are now committed. Removing displaced
            # originals is cleanup, not part of rollback: a multi-file worktree
            # update cannot be power-loss atomic.
            cleanup_errors = [
                error
                for target in prepared
                if (error := _discard_displaced_original(target)) is not None
            ]
            _close_prepared(prepared)
            if cleanup_errors:
                raise PublishingError(
                    "promotion committed but displaced-file cleanup was incomplete: "
                    + "; ".join(cleanup_errors)
                )

    return PublicationResult(plan=plan, applied=True)


def promote(config: PublicationConfig, *, apply: bool = False) -> PublicationResult:
    """Plan a promotion, applying it only when ``apply`` is explicitly true."""

    plan = plan_promotion(config)
    if not apply:
        return PublicationResult(plan=plan, applied=False)
    return apply_promotion(plan)


__all__ = [
    "BundleVerificationError",
    "ConcurrentModificationError",
    "ExternalArtifactFile",
    "PublicationConfig",
    "PublicationFile",
    "PublicationPlan",
    "PublicationResult",
    "PublishingError",
    "RepositoryStateError",
    "apply_promotion",
    "plan_promotion",
    "promote",
]
