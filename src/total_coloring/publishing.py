"""Fail-closed promotion of a verified, immutable bundle into a public data repo.

The library is deliberately local-only. It never stages Git changes, commits,
pushes, creates releases, or invokes a shell. ``promote`` is a dry run unless
the caller explicitly passes ``apply=True``. Individual replacements and
rollback are durable, but a multi-file filesystem update cannot be crash-atomic;
published artifact paths are therefore immutable and should be versioned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

_SUPPORTED_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
_DEFAULT_MANIFEST_PATH = PurePosixPath("manifests/dataset-manifest.json")
_DEFAULT_CHECKSUMS_PATH = PurePosixPath("SHA256SUMS")
_TRUSTED_RESULT_SCHEMA_PATH = PurePosixPath("schemas/result-v1.schema.json")
_DEFAULT_DATASET_ID = "total-coloring-data"
_DEFAULT_DATASET_REPOSITORY = "https://github.com/chenle02/total-coloring-data"
_DEFAULT_DATASET_LICENSE = "CC-BY-4.0"
_DEFAULT_MANAGED_ROOTS = (PurePosixPath("reports"), PurePosixPath("results"))
_RELEASE_STATUSES = ("development", "candidate", "published")
_SHA1_PATTERN = re.compile(r"[0-9a-f]{40}")
_CANONICAL_UTC_PATTERN = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z"
)
_SEMVER_PATTERN = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
# SHA-256 of the canonical JSON representation (sorted keys, compact separators)
# of schemas/dataset-manifest-v1.schema.json. This trusted value is updated only
# together with the verifier, schema, and adversarial schema-pinning tests.
_TRUSTED_MANIFEST_SCHEMA_DIGEST = "d820fadf9dfb1de44c81c1cf9baea43b95de8fc360448e02d2c16461cb747133"
_TRUSTED_RESULT_SCHEMA_DIGEST = "56acf75e9d41a64d1c2bf8d2e2651cb12a7fdefe7eac0ed55397dc231e36139a"
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
        "minimum",
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
class PublicationConfig:
    """Paths and exact dirty-file exceptions for one promotion.

    ``allowed_dirty_paths`` tolerates known destination changes. A dirty target
    is accepted only when its bytes already equal the inspected source bundle,
    which supports an explicit idempotent retry after an uncommitted promotion.

    The expected dataset fields are a trusted local policy, independent of the
    candidate-controlled manifest. ``expected_code_commit`` binds a promotion
    to the generating toolkit commit when supplied.
    """

    source_root: Path
    destination_root: Path
    manifest_path: PurePosixPath = _DEFAULT_MANIFEST_PATH
    checksums_path: PurePosixPath = _DEFAULT_CHECKSUMS_PATH
    allowed_dirty_paths: tuple[PurePosixPath, ...] = ()
    expected_dataset_id: str = _DEFAULT_DATASET_ID
    expected_dataset_repository: str = _DEFAULT_DATASET_REPOSITORY
    expected_license: str = _DEFAULT_DATASET_LICENSE
    expected_managed_roots: tuple[PurePosixPath, ...] = _DEFAULT_MANAGED_ROOTS
    expected_code_commit: str | None = None

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
        repository = urlsplit(self.expected_dataset_repository)
        if repository.scheme not in {"http", "https"} or not repository.netloc:
            raise ValueError("expected_dataset_repository must be an HTTP(S) URI")
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


def _parse_semver(value: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    match = _SEMVER_PATTERN.fullmatch(value)
    if match is None:
        raise BundleVerificationError(f"release.version is not canonical SemVer: {value!r}")
    major, minor, patch, prerelease = match.groups()
    identifiers = tuple(prerelease.split(".")) if prerelease is not None else None
    return (int(major), int(minor), int(patch)), identifiers


def _compare_semver(left: str, right: str) -> int:
    left_core, left_pre = _parse_semver(left)
    right_core, right_pre = _parse_semver(right)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
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
            return -1 if int(left_item) < int(right_item) else 1
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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
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
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
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
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    if format_name == "date-time":
        try:
            parsed_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed_time.tzinfo is not None
    if format_name == "utc-date-time":
        return _canonical_utc(value)
    raise BundleVerificationError(f"unsupported JSON Schema format {format_name!r}")


def _check_schema_definition(schema: Any, location: str, errors: list[str]) -> None:
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
    items = schema.get("items")
    if items is not None:
        _check_schema_definition(items, f"{location}.items", errors)


def _validate_instance(
    instance: Any,
    schema: dict[str, Any],
    location: str,
    errors: list[str],
) -> None:
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
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{location}: expected constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
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
        and isinstance(instance, (int, float))
        and not isinstance(instance, bool)
        and instance < minimum
    ):
        errors.append(f"{location}: minimum is {minimum}")
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
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{location}.{key}: property is not allowed"
                for key in sorted(set(instance) - set(properties))
            )
        for key, child_schema in properties.items():
            if key in instance and isinstance(child_schema, dict):
                _validate_instance(instance[key], child_schema, f"{location}.{key}", errors)
    if isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate_instance(item, item_schema, f"{location}[{index}]", errors)


def _validate_document(instance: Any, schema: Any, label: str) -> None:
    errors: list[str] = []
    _check_schema_definition(schema, f"{label}:schema", errors)
    if isinstance(schema, dict):
        _validate_instance(instance, schema, label, errors)
    if errors:
        raise BundleVerificationError("schema verification failed:\n  " + "\n  ".join(errors))


def _parse_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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


def _inspect_bundle(
    config: PublicationConfig,
    root: Path,
    *,
    enforce_expected_commit: bool = True,
) -> _Bundle:
    manifest_file = _regular_file(root, config.manifest_path, "manifest")
    manifest = _load_json(manifest_file, "manifest")
    if not isinstance(manifest, dict):
        raise BundleVerificationError("manifest must be a JSON object")
    raw_schema_path = manifest.get("$schema")
    schema_path = _safe_relative_path(raw_schema_path) if isinstance(raw_schema_path, str) else None
    if schema_path is None or not schema_path.parts or schema_path.parts[0] != "schemas":
        raise BundleVerificationError("manifest $schema must be a safe path under schemas/")
    schema_file = _regular_file(root, schema_path, "manifest schema")
    manifest_schema = _load_json(schema_file, "manifest schema")
    if _canonical_digest(manifest_schema) != _TRUSTED_MANIFEST_SCHEMA_DIGEST:
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

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise BundleVerificationError("artifacts must be an array")
    artifact_paths: list[PurePosixPath] = []
    expected_checksums: dict[str, str] = {}
    schema_paths: set[PurePosixPath] = {schema_path}
    result_record_locations: dict[str, PurePosixPath] = {}
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
            if raw_record_schema != _TRUSTED_RESULT_SCHEMA_PATH.as_posix():
                raise BundleVerificationError(
                    f"artifact {index}: result artifacts must use {_TRUSTED_RESULT_SCHEMA_PATH}"
                )
        if isinstance(raw_record_schema, str):
            record_schema_path = _safe_relative_path(raw_record_schema)
            if (
                record_schema_path is None
                or not record_schema_path.parts
                or record_schema_path.parts[0] != "schemas"
            ):
                raise BundleVerificationError(f"artifact {index} has an unsafe schema path")
            if record_schema_path != _TRUSTED_RESULT_SCHEMA_PATH:
                raise BundleVerificationError(
                    f"artifact {index} references an untrusted record schema"
                )
            schema_paths.add(record_schema_path)
            record_schema_file = _regular_file(root, record_schema_path, f"artifact {index} schema")
            record = _load_json(artifact_file, f"artifact {index}")
            record_schema = _load_json(record_schema_file, f"artifact {index} schema")
            if _canonical_digest(record_schema) != _TRUSTED_RESULT_SCHEMA_DIGEST:
                raise BundleVerificationError(
                    f"artifact {index} schema does not match the trusted result schema"
                )
            _validate_document(record, record_schema, str(relative))
            _validate_result_semantics(
                record,
                str(relative),
                expected_repository=code_repository,
                expected_commit=code_commit,
            )
            if isinstance(record, dict) and isinstance(record_id := record.get("record_id"), str):
                previous = result_record_locations.get(record_id)
                if previous is not None:
                    raise BundleVerificationError(
                        f"artifact {relative}: duplicate result record_id {record_id!r}; "
                        f"first declared at {previous}"
                    )
                result_record_locations[record_id] = relative
            declared_records = artifact.get("records")
            if isinstance(declared_records, int) and not isinstance(declared_records, bool):
                actual_records = len(record) if isinstance(record, list) else 1
                if actual_records != declared_records:
                    raise BundleVerificationError(
                        f"artifact {relative}: record count mismatch "
                        f"({actual_records} != {declared_records})"
                    )

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
        expected_license=config.expected_license,
        expected_managed_roots=config.expected_managed_roots,
        expected_code_commit=config.expected_code_commit,
    )


def _existing_destination_bundle(config: PublicationConfig) -> _Bundle | None:
    if _destination_digest(config.destination_root, config.manifest_path) is None:
        return None
    return _inspect_bundle(config, config.destination_root, enforce_expected_commit=False)


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        backup_root = transaction_root / "backup"
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
        installed: list[PublicationFile] = []
        backups: set[PurePosixPath] = set()
        created_directories: list[Path] = []
        try:
            for item in sorted(changed, key=_install_order):
                final = destination.joinpath(*item.path.parts)
                missing_parents: list[Path] = []
                parent = final.parent
                while parent != destination and not parent.exists():
                    missing_parents.append(parent)
                    parent = parent.parent
                for directory in reversed(missing_parents):
                    directory.mkdir()
                    created_directories.append(directory)

                current_digest = _destination_digest(destination, item.path)
                if current_digest != item.destination_sha256:
                    raise ConcurrentModificationError(
                        f"destination changed before replacing {item.path}"
                    )
                if final.exists():
                    backup = backup_root.joinpath(*item.path.parts)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(final, backup)
                    _fsync_file(backup)
                    backups.add(item.path)
                staged = stage_root.joinpath(*item.path.parts)
                os.replace(staged, final)
                installed.append(item)
                _fsync_directory(final.parent)

            _inspect_bundle(plan.config, destination)
        except BaseException as original_error:
            rollback_errors: list[str] = []
            for item in reversed(installed):
                final = destination.joinpath(*item.path.parts)
                try:
                    if item.path in backups:
                        backup = backup_root.joinpath(*item.path.parts)
                        os.replace(backup, final)
                    elif final.exists():
                        final.unlink()
                    _fsync_directory(final.parent)
                except OSError as error:
                    rollback_errors.append(f"{item.path}: {error}")
            for directory in reversed(created_directories):
                with suppress(OSError):
                    directory.rmdir()
            if rollback_errors:
                raise PublishingError(
                    "promotion failed and rollback was incomplete: " + "; ".join(rollback_errors)
                ) from original_error
            raise

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
