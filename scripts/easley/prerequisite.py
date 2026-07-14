"""Strict validation and artifact gating for the order-eight prerequisite."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from scripts.easley.common import (
    CAMPAIGN_CONTRACT_SCHEMA,
    MAX_METADATA_BYTES,
    CampaignError,
    atomic_json,
    canonical_json_bytes,
    launcher_files,
    require_no_python_bytecode,
    require_readonly_tree,
)

RUNTIME_SCHEMA = "total-coloring.easley-runtime.v1"
VALIDATION_SCHEMA = "total-coloring.easley-validation.v1"
REDUCE_SCHEMA = "total-coloring.easley-reduce.v1"
EXACT_UNION_SCHEMA = "total-coloring.easley-exact-union.v1"
ORDER8_GATE_SCHEMA = "total-coloring.easley-order8-gate.v1"
ORDER8_ARTIFACT_INVENTORY_SCHEMA = "total-coloring.easley-order8-artifacts.v1"
ORDER8_GATE_FILENAME = "order8-prerequisite-complete.json"

COUNT_NAMES: Final = ("candidate_unsat", "error", "skipped", "unknown", "verified_all")
EXPECTED_CHECKS: Final = (
    {"backend_id": "dsatur-iterative-v1", "palette_offset": 1},
    {"backend_id": "dsatur-iterative-v1", "palette_offset": 2},
    {"backend_id": "static-order-iterative-v1", "palette_offset": 1},
)
NAUTY_TAR_SHA256 = "9fc4edae04f88a0f5883985be3b39cf7f898fd6cc96e96b9ee25452743cc1b5b"
UNIVERSAL_MANIFEST_SCHEMA = "total-coloring.universal-census-manifest.v1"
UNIVERSAL_COMPLETION_SCHEMA = "total-coloring.universal-census-completion.v1"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """One full POSIX identity snapshot used to close path-based TOCTOU gaps."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, status: os.stat_result) -> FileIdentity:
        return cls(
            device=status.st_dev,
            inode=status.st_ino,
            mode=status.st_mode,
            size=status.st_size,
            mtime_ns=status.st_mtime_ns,
            ctime_ns=status.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class ArtifactInventoryEntry:
    """Portable content identity for one prerequisite artifact."""

    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class Order8ArtifactInventory:
    """Canonical inventory of every order-eight validation and run artifact."""

    entries: tuple[ArtifactInventoryEntry, ...]
    root_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "artifacts": [entry.to_dict() for entry in self.entries],
            "schema_version": ORDER8_ARTIFACT_INVENTORY_SCHEMA,
        }


@dataclass(frozen=True, slots=True)
class Order8Prerequisite:
    """Validated identities carried into an order-nine campaign."""

    geng_sha256: str
    receipt_sha256: str
    runtime_receipt_sha256: str
    code_commit: str
    wheel_sha256: str
    launcher_sha256: str
    launcher_archive_sha256: str
    order8_campaign_contract_sha256: str
    artifact_inventory: Order8ArtifactInventory | None = None

    @property
    def order8_artifact_root_sha256(self) -> str | None:
        """Return the portable artifact root when full verification was requested."""

        if self.artifact_inventory is None:
            return None
        return self.artifact_inventory.root_sha256


@dataclass(frozen=True, slots=True)
class Order8Gate:
    """Strict identity parsed from an order-eight prerequisite gate receipt."""

    job_id: str
    order8_receipt_sha256: str
    order8_artifact_root_sha256: str
    order8_replay_sha256: str
    runtime_receipt_sha256: str
    code_commit: str
    geng_sha256: str
    wheel_sha256: str
    launcher_sha256: str
    launcher_archive_sha256: str
    campaign_contract_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_contract_sha256": self.campaign_contract_sha256,
            "code_commit": self.code_commit,
            "geng_sha256": self.geng_sha256,
            "job_id": self.job_id,
            "launcher_archive_sha256": self.launcher_archive_sha256,
            "launcher_sha256": self.launcher_sha256,
            "order8_artifact_root_sha256": self.order8_artifact_root_sha256,
            "order8_receipt_sha256": self.order8_receipt_sha256,
            "order8_replay_sha256": self.order8_replay_sha256,
            "runtime_receipt_sha256": self.runtime_receipt_sha256,
            "schema_version": ORDER8_GATE_SCHEMA,
            "status": "order8_prerequisite_complete",
            "wheel_sha256": self.wheel_sha256,
        }


@dataclass(frozen=True, slots=True)
class _BoundFile:
    path: Path
    identity: FileIdentity
    sha256: str
    data: bytes | None = None


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise CampaignError(f"{name} must be a JSON object with string keys")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, *, name: str) -> Sequence[Any]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise CampaignError(f"{name} must be a JSON array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, name: str) -> None:
    if set(value) != expected:
        raise CampaignError(f"{name} has an unexpected field set")


def _integer(value: object, *, name: str, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if positive else "nonnegative"
        raise CampaignError(f"{name} must be a {qualifier} integer")
    return value


def _string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CampaignError(f"{name} must be a nonempty string")
    return value


def _digest(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CampaignError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _commit(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CampaignError(f"{name} must be a 40-character lowercase Git object id")
    return value


def _counts(value: object, *, name: str) -> dict[str, int]:
    raw = _mapping(value, name=name)
    _exact_keys(raw, set(COUNT_NAMES), name=name)
    return {key: _integer(raw[key], name=f"{name}.{key}") for key in COUNT_NAMES}


def _totals(value: object, *, name: str) -> tuple[dict[str, int], dict[str, int]]:
    raw = _mapping(value, name=name)
    _exact_keys(
        raw,
        {"check_evaluations", "counts", "partition_count", "record_count", "records_bytes"},
        name=name,
    )
    totals = {
        key: _integer(raw[key], name=f"{name}.{key}")
        for key in ("check_evaluations", "partition_count", "record_count", "records_bytes")
    }
    return totals, _counts(raw["counts"], name=f"{name}.counts")


def _toolkit_identity(value: object, *, name: str) -> Mapping[str, Any]:
    toolkit = _mapping(value, name=name)
    _exact_keys(
        toolkit,
        {"distribution_version", "python_implementation", "python_version", "source_sha256"},
        name=name,
    )
    _string(toolkit["distribution_version"], name=f"{name}.distribution_version")
    _string(toolkit["python_implementation"], name=f"{name}.python_implementation")
    _string(toolkit["python_version"], name=f"{name}.python_version")
    _digest(toolkit["source_sha256"], name=f"{name}.source_sha256")
    return toolkit


def _file_identity(path: Path, *, name: str) -> FileIdentity:
    try:
        status = path.lstat()
    except OSError as exc:
        raise CampaignError(f"cannot inspect {name} {path}: {exc}") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise CampaignError(f"{name} must be a regular non-symlink file: {path}")
    return FileIdentity.from_stat(status)


def _bind_file(path: Path, *, name: str, capture: bool = False) -> _BoundFile:
    """Hash a file through one descriptor bracketed by complete path identities."""

    before = _file_identity(path, name=name)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignError(f"cannot open {name} {path}: {exc}") from exc
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if capture else None
    consumed = 0
    try:
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        if opened != before:
            raise CampaignError(f"{name} changed between lstat and open: {path}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            consumed += len(block)
            if capture and consumed > MAX_METADATA_BYTES:
                raise CampaignError(f"JSON artifact exceeds {MAX_METADATA_BYTES} bytes: {path}")
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
        opened_after = FileIdentity.from_stat(os.fstat(descriptor))
        if opened_after != opened or consumed != opened.size:
            raise CampaignError(f"{name} changed while it was read: {path}")
    finally:
        os.close(descriptor)
    after = _file_identity(path, name=name)
    if after != before:
        raise CampaignError(f"{name} changed while it was hashed: {path}")
    return _BoundFile(
        path=path,
        identity=before,
        sha256=digest.hexdigest(),
        data=b"".join(chunks) if chunks is not None else None,
    )


def _load_bound_json(path: Path, *, name: str) -> tuple[Mapping[str, Any], _BoundFile]:
    bound = _bind_file(path, name=name, capture=True)
    assert bound.data is not None
    try:
        value = json.loads(bound.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignError(f"invalid JSON artifact {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or not all(isinstance(key, str) for key in value)
        or canonical_json_bytes(value) != bound.data
    ):
        raise CampaignError(f"artifact is not canonical JSON with one trailing LF: {path}")
    return cast(Mapping[str, Any], value), bound


def _require_real_directory(path: Path, *, name: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise CampaignError(f"cannot inspect {name} {path}: {exc}") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise CampaignError(f"{name} must be a real directory: {path}")


def _require_bound_files_unchanged(bound_files: Sequence[_BoundFile]) -> None:
    """Re-hash every bound file after the whole chain has been checked."""

    unique: dict[Path, _BoundFile] = {}
    for bound in bound_files:
        previous = unique.setdefault(bound.path, bound)
        if previous.identity != bound.identity or previous.sha256 != bound.sha256:
            raise CampaignError(f"file acquired conflicting identities: {bound.path}")
    for path, expected in unique.items():
        actual = _bind_file(path, name="bound prerequisite file")
        if actual.identity != expected.identity or actual.sha256 != expected.sha256:
            raise CampaignError(f"prerequisite file changed during final verification: {path}")


def _launcher_digest(
    root: Path,
    *,
    bound_files: list[_BoundFile] | None = None,
) -> str:
    inventory: list[dict[str, str]] = []
    for path in launcher_files(root):
        bound = _bind_file(path, name="sealed launcher source")
        if bound_files is not None:
            bound_files.append(bound)
        inventory.append({"path": path.relative_to(root).as_posix(), "sha256": bound.sha256})
    return hashlib.sha256(canonical_json_bytes(inventory)).hexdigest()


def _validate_runtime(
    runtime: Path,
    *,
    code_commit: str,
    launcher_archive_sha256: str,
    launcher_digest: str,
    toolkit_version: str,
    wheel_sha256: str,
    bound_files: list[_BoundFile],
) -> tuple[Mapping[str, Any], str, Path]:
    receipt_path = runtime / "runtime-receipt.json"
    receipt, receipt_bound = _load_bound_json(receipt_path, name="runtime receipt")
    bound_files.append(receipt_bound)
    _exact_keys(
        receipt,
        {
            "bootstrap_job_id",
            "code_commit",
            "geng_sha256",
            "launcher_archive_sha256",
            "launcher_sha256",
            "nauty_tar_sha256",
            "nauty_version",
            "platform",
            "python",
            "runtime_python",
            "runtime_python_sha256",
            "schema_version",
            "smoke_order",
            "smoke_record_count",
            "toolkit_identity",
            "toolkit_version",
            "wheel_name",
            "wheel_sha256",
        },
        name="runtime receipt",
    )
    if receipt["schema_version"] != RUNTIME_SCHEMA:
        raise CampaignError("runtime receipt has an unsupported schema")
    if _commit(receipt["code_commit"], name="runtime code commit") != code_commit:
        raise CampaignError("runtime receipt binds a different code commit")
    if (
        _digest(receipt["launcher_archive_sha256"], name="runtime launcher archive SHA-256")
        != launcher_archive_sha256
        or _digest(receipt["launcher_sha256"], name="runtime launcher SHA-256") != launcher_digest
        or _string(receipt["toolkit_version"], name="runtime toolkit version") != toolkit_version
        or _digest(receipt["wheel_sha256"], name="runtime wheel SHA-256") != wheel_sha256
        or receipt["nauty_version"] != "2.9.3"
        or _digest(receipt["nauty_tar_sha256"], name="runtime nauty archive SHA-256")
        != NAUTY_TAR_SHA256
        or _integer(receipt["smoke_order"], name="runtime smoke order") != 4
        or _integer(receipt["smoke_record_count"], name="runtime smoke record count") != 11
    ):
        raise CampaignError("runtime receipt violates the production prerequisite")
    _string(receipt["platform"], name="runtime platform")
    _string(receipt["python"], name="runtime Python version")
    _string(receipt["wheel_name"], name="runtime wheel name")
    geng_sha256 = _digest(receipt["geng_sha256"], name="runtime geng SHA-256")
    runtime_python_sha256 = _digest(receipt["runtime_python_sha256"], name="runtime Python SHA-256")
    toolkit = _toolkit_identity(receipt["toolkit_identity"], name="runtime toolkit identity")
    if toolkit["distribution_version"] != toolkit_version:
        raise CampaignError("runtime toolkit identity has the wrong distribution version")
    bootstrap_job_id = _string(receipt["bootstrap_job_id"], name="runtime bootstrap job id")
    if not bootstrap_job_id.isdigit():
        raise CampaignError("runtime bootstrap job id must be numeric")
    relative_python_raw = _string(receipt["runtime_python"], name="runtime Python path")
    relative_python = PurePosixPath(relative_python_raw)
    if relative_python.as_posix() != "venv/bin/python":
        raise CampaignError("runtime receipt has an unexpected Python entry point")
    if _launcher_digest(runtime / "launcher", bound_files=bound_files) != launcher_digest:
        raise CampaignError("sealed runtime launcher does not match the production request")
    geng = _bind_file(runtime / "bin" / "geng", name="runtime geng executable")
    bound_files.append(geng)
    if geng.sha256 != geng_sha256:
        raise CampaignError("runtime geng executable changed after order eight")
    python_entry = runtime / relative_python
    try:
        python_target = python_entry.resolve(strict=True)
    except OSError as exc:
        raise CampaignError(f"cannot resolve runtime Python executable: {exc}") from exc
    python = _bind_file(python_target, name="runtime Python executable")
    bound_files.append(python)
    if python.sha256 != runtime_python_sha256:
        raise CampaignError("runtime Python executable changed after order eight")
    require_no_python_bytecode(runtime)
    require_readonly_tree(runtime)
    return receipt, receipt_bound.sha256, python_target


def _validate_campaign_contract(
    path: Path,
    *,
    expected_sha256: str,
    scratch: Path,
    runtime: Path,
    code_commit: str,
    launcher_archive_sha256: str,
    launcher_digest: str,
    runtime_receipt_sha256: str,
    geng_sha256: str,
    toolkit_version: str,
    wheel_sha256: str,
    bound_files: list[_BoundFile],
) -> None:
    contract, bound = _load_bound_json(path, name="order-eight campaign contract")
    bound_files.append(bound)
    if bound.sha256 != expected_sha256:
        raise CampaignError("order-eight campaign contract does not match its receipt hash")
    _exact_keys(contract, {"environment", "profile", "schema_version"}, name="campaign contract")
    if (
        contract["schema_version"] != CAMPAIGN_CONTRACT_SCHEMA
        or contract["profile"] != "order8-smoke"
    ):
        raise CampaignError("order-eight campaign contract has the wrong schema or profile")
    environment = _mapping(contract["environment"], name="campaign contract environment")
    if not all(
        key.startswith("TC_")
        and key not in {"TC_CAMPAIGN_CONTRACT", "TC_CAMPAIGN_CONTRACT_SHA256"}
        and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise CampaignError("order-eight campaign contract environment is malformed")
    expected_environment = {
        "TC_CAMPAIGN_MODE": "scientific",
        "TC_CODE_COMMIT": code_commit,
        "TC_EXPECTED_CHECKS": "1542150",
        "TC_EXPECTED_PARTITIONS": "514050",
        "TC_EXPECTED_RECORDS": "12346",
        "TC_EXPECTED_SKIPPED": "424",
        "TC_EXPECTED_VERIFIED": "11922",
        "TC_LAUNCHER_ARCHIVE_SHA256": launcher_archive_sha256,
        "TC_LAUNCHER_SHA256": launcher_digest,
        "TC_GENG_SHA256": geng_sha256,
        "TC_ORDER": "8",
        "TC_RUNTIME": str(runtime),
        "TC_RUNTIME_RECEIPT_SHA256": runtime_receipt_sha256,
        "TC_SCRATCH": str(scratch),
        "TC_SHARDS": "64",
        "TC_SPLIT_DEPTH": "2",
        "TC_TOOLKIT_VERSION": toolkit_version,
        "TC_WHEEL_SHA256": wheel_sha256,
    }
    if any(environment.get(key) != value for key, value in expected_environment.items()):
        raise CampaignError("order-eight campaign contract does not match the sealed census")


def _validate_check_matrix(value: object) -> None:
    checks = _sequence(value, name="order-eight check matrix")
    checked_checks: list[dict[str, object]] = []
    for index, raw_check in enumerate(checks):
        check = _mapping(raw_check, name=f"order-eight check {index}")
        _exact_keys(check, {"backend_id", "palette_offset"}, name=f"order-eight check {index}")
        checked_checks.append(
            {
                "backend_id": _string(
                    check["backend_id"], name=f"order-eight check {index} backend id"
                ),
                "palette_offset": _integer(
                    check["palette_offset"], name=f"order-eight check {index} palette offset"
                ),
            }
        )
    if tuple(checked_checks) != EXPECTED_CHECKS:
        raise CampaignError("order-eight exact-union check matrix is not canonical")


def _validate_manifest(
    manifest: Mapping[str, Any],
    completion: Mapping[str, Any],
    *,
    index: int,
    shard: Mapping[str, Any],
    toolkit: Mapping[str, Any],
    geng_sha256: str,
) -> None:
    name = f"order-eight shard {index} manifest"
    _exact_keys(
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
        name=name,
    )
    if manifest["schema_version"] != UNIVERSAL_MANIFEST_SCHEMA or manifest["complete"] is not True:
        raise CampaignError(f"{name} has the wrong schema or completion state")
    if (
        _digest(manifest["run_fingerprint"], name=f"{name} run fingerprint")
        != shard["run_fingerprint"]
        or _integer(manifest["record_count"], name=f"{name} record count") != shard["record_count"]
        or _integer(manifest["partition_count"], name=f"{name} partition count")
        != shard["partition_count"]
        or _counts(manifest["counts"], name=f"{name} counts") != shard["counts"]
    ):
        raise CampaignError(f"{name} does not match its validation receipt")
    artifacts = _mapping(manifest["artifacts"], name=f"{name} artifacts")
    _exact_keys(
        artifacts, {"records_bytes", "records_path", "records_sha256"}, name=f"{name} artifacts"
    )
    if (
        artifacts["records_path"] != "records.jsonl"
        or _integer(artifacts["records_bytes"], name=f"{name} records bytes")
        != shard["records_bytes"]
        or _digest(artifacts["records_sha256"], name=f"{name} records SHA-256")
        != shard["records_sha256"]
    ):
        raise CampaignError(f"{name} artifact receipt is inconsistent")
    provenance = _mapping(manifest["provenance"], name=f"{name} provenance")
    _exact_keys(
        provenance,
        {"config", "generator", "objective", "shard", "toolkit"},
        name=f"{name} provenance",
    )
    if provenance["objective"] != "universal_auxiliary_extension":
        raise CampaignError(f"{name} has the wrong scientific objective")
    if _toolkit_identity(provenance["toolkit"], name=f"{name} toolkit") != toolkit:
        raise CampaignError(f"{name} has a foreign toolkit identity")
    provenance_shard = _mapping(provenance["shard"], name=f"{name} shard envelope")
    _exact_keys(provenance_shard, {"count", "index"}, name=f"{name} shard envelope")
    if (
        _integer(provenance_shard["count"], name=f"{name} shard count", positive=True) != 64
        or _integer(provenance_shard["index"], name=f"{name} shard index") != index
    ):
        raise CampaignError(f"{name} has a foreign shard envelope")
    generator = _mapping(provenance["generator"], name=f"{name} generator")
    _exact_keys(generator, {"arguments", "executable", "name", "sha256"}, name=f"{name} generator")
    arguments = _sequence(generator["arguments"], name=f"{name} generator arguments")
    if (
        generator["name"] != "nauty-geng"
        or generator["executable"] != "geng"
        or _digest(generator["sha256"], name=f"{name} generator SHA-256") != geng_sha256
        or not all(isinstance(argument, str) for argument in arguments)
    ):
        raise CampaignError(f"{name} has a malformed generator identity")
    config = _mapping(provenance["config"], name=f"{name} config")
    _exact_keys(
        config,
        {
            "checkpoint_interval",
            "checks",
            "filters",
            "fix_distinguished_colors",
            "generator_spec",
            "partition_enumerator",
            "search_limits",
        },
        name=f"{name} config",
    )
    if (
        _integer(config["checkpoint_interval"], name=f"{name} checkpoint interval", positive=True)
        < 1
        or config["fix_distinguished_colors"] is not True
        or config["partition_enumerator"] != "complement-matchings-lexicographic-v1"
    ):
        raise CampaignError(f"{name} has a malformed universal config")
    _validate_check_matrix(config["checks"])
    filters = _mapping(config["filters"], name=f"{name} filters")
    _exact_keys(filters, {"require_high_degree"}, name=f"{name} filters")
    if filters["require_high_degree"] is not True:
        raise CampaignError(f"{name} has a noncanonical graph filter")
    generator_spec = _mapping(config["generator_spec"], name=f"{name} generator spec")
    _exact_keys(
        generator_spec,
        {"connected", "max_degree", "min_degree", "order", "shard_count", "shard_index"},
        name=f"{name} generator spec",
    )
    if (
        generator_spec["connected"] is not False
        or generator_spec["max_degree"] is not None
        or generator_spec["min_degree"] is not None
        or _integer(generator_spec["order"], name=f"{name} generator order", positive=True) != 8
        or _integer(
            generator_spec["shard_count"], name=f"{name} generator shard count", positive=True
        )
        != 64
        or _integer(generator_spec["shard_index"], name=f"{name} generator shard index") != index
    ):
        raise CampaignError(f"{name} has a foreign generator spec")
    limits = _mapping(config["search_limits"], name=f"{name} search limits")
    _exact_keys(
        limits,
        {"max_nodes_per_check", "timeout_seconds_per_check"},
        name=f"{name} search limits",
    )
    if limits != {"max_nodes_per_check": None, "timeout_seconds_per_check": None}:
        raise CampaignError(f"{name} has noncanonical search limits")
    expected_fingerprint = hashlib.sha256(canonical_json_bytes(provenance)[:-1]).hexdigest()
    if expected_fingerprint != shard["run_fingerprint"]:
        raise CampaignError(f"{name} provenance does not produce its run fingerprint")

    completion_name = f"order-eight shard {index} completion"
    _exact_keys(
        completion,
        {"manifest_sha256", "record_count", "records_sha256", "run_fingerprint", "schema_version"},
        name=completion_name,
    )
    if (
        completion["schema_version"] != UNIVERSAL_COMPLETION_SCHEMA
        or _digest(completion["manifest_sha256"], name=f"{completion_name} manifest SHA-256")
        != shard["manifest_sha256"]
        or _integer(completion["record_count"], name=f"{completion_name} record count")
        != shard["record_count"]
        or _digest(completion["records_sha256"], name=f"{completion_name} records SHA-256")
        != shard["records_sha256"]
        or _digest(completion["run_fingerprint"], name=f"{completion_name} run fingerprint")
        != shard["run_fingerprint"]
    ):
        raise CampaignError(f"{completion_name} does not match its artifact chain")


def _validate_artifacts(
    scratch: Path,
    *,
    shards: Sequence[Mapping[str, Any]],
    code_commit: str,
    launcher_archive_sha256: str,
    launcher_digest: str,
    runtime_receipt_sha256: str,
    geng_sha256: str,
    wheel_sha256: str,
    toolkit: Mapping[str, Any],
    campaign_contract_sha256: str,
    bound_files: list[_BoundFile],
) -> Order8ArtifactInventory:
    status = scratch / "status"
    runs = scratch / "runs"
    _require_real_directory(status, name="order-eight status directory")
    _require_real_directory(runs, name="order-eight runs directory")
    entries: list[ArtifactInventoryEntry] = []

    def add(bound: _BoundFile) -> None:
        try:
            relative = bound.path.relative_to(scratch).as_posix()
        except ValueError as exc:
            raise CampaignError("order-eight artifact escaped its scratch root") from exc
        entries.append(
            ArtifactInventoryEntry(path=relative, size=bound.identity.size, sha256=bound.sha256)
        )
        bound_files.append(bound)

    validation_fields = {
        "campaign_contract_sha256",
        "check_evaluations",
        "code_commit",
        "completion_sha256",
        "counts",
        "geng_sha256",
        "launcher_archive_sha256",
        "launcher_sha256",
        "manifest_sha256",
        "order",
        "partition_count",
        "record_count",
        "records_bytes",
        "records_sha256",
        "run_fingerprint",
        "runtime_receipt_sha256",
        "schema_version",
        "shard_count",
        "shard_index",
        "split_depth",
        "status",
        "toolkit",
        "wheel_sha256",
    }
    for index, shard in enumerate(shards):
        validation_path = status / f"validation-complete-{index:03d}.json"
        validation, validation_bound = _load_bound_json(
            validation_path, name=f"order-eight validation receipt {index}"
        )
        add(validation_bound)
        _exact_keys(validation, validation_fields, name=f"order-eight validation receipt {index}")
        validation_counts = _counts(
            validation["counts"], name=f"order-eight validation receipt {index}.counts"
        )
        if (
            validation["schema_version"] != VALIDATION_SCHEMA
            or validation["status"] != "validation_complete"
            or _integer(validation["order"], name=f"order-eight validation receipt {index}.order")
            != 8
            or _integer(
                validation["shard_count"],
                name=f"order-eight validation receipt {index}.shard_count",
                positive=True,
            )
            != 64
            or _integer(
                validation["shard_index"],
                name=f"order-eight validation receipt {index}.shard_index",
            )
            != index
            or _integer(
                validation["split_depth"],
                name=f"order-eight validation receipt {index}.split_depth",
            )
            != 2
            or _commit(
                validation["code_commit"],
                name=f"order-eight validation receipt {index}.code_commit",
            )
            != code_commit
            or _digest(
                validation["campaign_contract_sha256"],
                name=f"order-eight validation receipt {index}.campaign contract SHA-256",
            )
            != campaign_contract_sha256
            or _digest(
                validation["launcher_archive_sha256"],
                name=f"order-eight validation receipt {index}.launcher archive SHA-256",
            )
            != launcher_archive_sha256
            or _digest(
                validation["launcher_sha256"],
                name=f"order-eight validation receipt {index}.launcher SHA-256",
            )
            != launcher_digest
            or _digest(
                validation["runtime_receipt_sha256"],
                name=f"order-eight validation receipt {index}.runtime receipt SHA-256",
            )
            != runtime_receipt_sha256
            or _digest(
                validation["geng_sha256"],
                name=f"order-eight validation receipt {index}.geng SHA-256",
            )
            != geng_sha256
            or _digest(
                validation["wheel_sha256"],
                name=f"order-eight validation receipt {index}.wheel SHA-256",
            )
            != wheel_sha256
            or _toolkit_identity(
                validation["toolkit"], name=f"order-eight validation receipt {index}.toolkit"
            )
            != toolkit
        ):
            raise CampaignError(f"order-eight validation receipt {index} violates the campaign")
        for field in (
            "check_evaluations",
            "partition_count",
            "record_count",
            "records_bytes",
        ):
            if (
                _integer(validation[field], name=f"order-eight validation receipt {index}.{field}")
                != shard[field]
            ):
                raise CampaignError(f"order-eight validation receipt {index} disagrees on {field}")
        for field in (
            "completion_sha256",
            "manifest_sha256",
            "records_sha256",
            "run_fingerprint",
        ):
            if (
                _digest(validation[field], name=f"order-eight validation receipt {index}.{field}")
                != shard[field]
            ):
                raise CampaignError(f"order-eight validation receipt {index} disagrees on {field}")
        if validation_counts != shard["counts"]:
            raise CampaignError(f"order-eight validation receipt {index} has foreign counts")

        directory = runs / f"shard-{index:03d}-of-064"
        _require_real_directory(directory, name=f"order-eight shard {index} directory")
        records = _bind_file(directory / "records.jsonl", name=f"order-eight shard {index} records")
        add(records)
        if (
            records.identity.size != shard["records_bytes"]
            or records.sha256 != shard["records_sha256"]
        ):
            raise CampaignError(f"order-eight shard {index} records do not match their receipt")
        manifest, manifest_bound = _load_bound_json(
            directory / "manifest.json", name=f"order-eight shard {index} manifest"
        )
        add(manifest_bound)
        completion, completion_bound = _load_bound_json(
            directory / "completion.json", name=f"order-eight shard {index} completion"
        )
        add(completion_bound)
        if (
            manifest_bound.sha256 != shard["manifest_sha256"]
            or completion_bound.sha256 != shard["completion_sha256"]
        ):
            raise CampaignError(f"order-eight shard {index} metadata hashes do not match")
        _validate_manifest(
            manifest,
            completion,
            index=index,
            shard=shard,
            toolkit=toolkit,
            geng_sha256=geng_sha256,
        )

    ordered = tuple(sorted(entries, key=lambda entry: entry.path))
    if len(ordered) != 256 or len({entry.path for entry in ordered}) != 256:
        raise CampaignError("order-eight artifact inventory must contain 256 unique files")
    payload = {
        "artifacts": [entry.to_dict() for entry in ordered],
        "schema_version": ORDER8_ARTIFACT_INVENTORY_SCHEMA,
    }
    root_sha256 = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return Order8ArtifactInventory(entries=ordered, root_sha256=root_sha256)


def validate_order8_prerequisite(
    receipt_path: Path,
    *,
    runtime: Path,
    code_commit: str,
    launcher_archive_sha256: str,
    launcher_digest: str,
    toolkit_version: str,
    wheel_sha256: str,
    verify_artifacts: bool = False,
) -> Order8Prerequisite:
    """Validate the order-eight chain, optionally binding every actual artifact.

    ``verify_artifacts=False`` is intentionally suitable for a login-node structural
    preflight.  The compute-node prerequisite task uses ``True`` and therefore
    requires all 64 validation receipts and all 192 shard artifacts on the same
    filesystem as the exact-union receipt.
    """

    if not isinstance(verify_artifacts, bool):
        raise CampaignError("verify_artifacts must be a boolean")
    code_commit = _commit(code_commit, name="requested code commit")
    launcher_archive_sha256 = _digest(
        launcher_archive_sha256, name="requested launcher archive SHA-256"
    )
    launcher_digest = _digest(launcher_digest, name="requested launcher SHA-256")
    wheel_sha256 = _digest(wheel_sha256, name="requested wheel SHA-256")
    toolkit_version = _string(toolkit_version, name="requested toolkit version")
    _require_real_directory(runtime, name="sealed runtime")
    bound_files: list[_BoundFile] = []
    runtime_receipt, runtime_receipt_sha256, python_target = _validate_runtime(
        runtime,
        code_commit=code_commit,
        launcher_archive_sha256=launcher_archive_sha256,
        launcher_digest=launcher_digest,
        toolkit_version=toolkit_version,
        wheel_sha256=wheel_sha256,
        bound_files=bound_files,
    )

    receipt_path = Path(receipt_path)
    receipt, receipt_bound = _load_bound_json(receipt_path, name="order-eight exact-union receipt")
    bound_files.append(receipt_bound)
    _exact_keys(
        receipt,
        {
            "campaign_contract_sha256",
            "checks",
            "code_commit",
            "generator",
            "geng_sha256",
            "job_id",
            "launcher_archive_sha256",
            "launcher_sha256",
            "order",
            "reduce_receipt_sha256",
            "runtime_receipt_sha256",
            "schema_version",
            "shard_count",
            "shards",
            "split_depth",
            "status",
            "toolkit",
            "totals",
            "wheel_sha256",
        },
        name="order-eight exact-union receipt",
    )
    receipt_code_commit = _commit(receipt["code_commit"], name="order-eight code commit")
    receipt_launcher_archive = _digest(
        receipt["launcher_archive_sha256"], name="order-eight launcher archive SHA-256"
    )
    receipt_launcher = _digest(receipt["launcher_sha256"], name="order-eight launcher SHA-256")
    receipt_wheel = _digest(receipt["wheel_sha256"], name="order-eight wheel SHA-256")
    receipt_runtime = _digest(
        receipt["runtime_receipt_sha256"], name="order-eight runtime receipt SHA-256"
    )
    campaign_contract_sha256 = _digest(
        receipt["campaign_contract_sha256"], name="order-eight campaign contract SHA-256"
    )
    if (
        receipt["schema_version"] != EXACT_UNION_SCHEMA
        or receipt["status"] != "exact_union_complete"
        or _integer(receipt["order"], name="order-eight order", positive=True) != 8
        or _integer(receipt["shard_count"], name="order-eight shard count", positive=True) != 64
        or _integer(receipt["split_depth"], name="order-eight split depth") != 2
        or receipt_code_commit != code_commit
        or receipt_launcher_archive != launcher_archive_sha256
        or receipt_launcher != launcher_digest
        or receipt_wheel != wheel_sha256
        or receipt_runtime != runtime_receipt_sha256
    ):
        raise CampaignError("order-eight exact-union receipt violates the production contract")
    receipt_toolkit = _toolkit_identity(receipt["toolkit"], name="order-eight toolkit identity")
    if receipt_toolkit != runtime_receipt["toolkit_identity"]:
        raise CampaignError("order-eight exact-union receipt binds a foreign toolkit")
    _validate_check_matrix(receipt["checks"])
    job_id = _string(receipt["job_id"], name="order-eight exact-union job id")
    if not job_id.isdigit():
        raise CampaignError("order-eight exact-union job id must be numeric")
    geng_sha256 = _digest(receipt["geng_sha256"], name="order-eight geng SHA-256")
    if geng_sha256 != runtime_receipt["geng_sha256"]:
        raise CampaignError("order-eight receipt and runtime bind different geng executables")
    generator = _mapping(receipt["generator"], name="order-eight generator")
    _exact_keys(generator, {"executable", "sha256"}, name="order-eight generator")
    if generator != {"executable": "geng", "sha256": geng_sha256}:
        raise CampaignError("order-eight generator identity is inconsistent")
    totals, total_counts = _totals(receipt["totals"], name="order-eight totals")
    expected_totals = {
        "check_evaluations": 1_542_150,
        "partition_count": 514_050,
        "record_count": 12_346,
    }
    expected_counts = {
        "candidate_unsat": 0,
        "error": 0,
        "skipped": 424,
        "unknown": 0,
        "verified_all": 11_922,
    }
    if any(totals[key] != value for key, value in expected_totals.items()):
        raise CampaignError("order-eight exact-union totals do not match the golden census")
    if total_counts != expected_counts:
        raise CampaignError("order-eight exact-union counts do not match the golden census")

    raw_shards = _sequence(receipt["shards"], name="order-eight shards")
    if len(raw_shards) != 64:
        raise CampaignError("order-eight exact-union receipt must contain 64 shard receipts")
    summed_totals = dict.fromkeys(totals, 0)
    summed_counts = dict.fromkeys(COUNT_NAMES, 0)
    reduced_receipts: list[dict[str, object]] = []
    checked_shards: list[Mapping[str, Any]] = []
    fingerprints: set[str] = set()
    shard_fields = {
        "check_evaluations",
        "completion_sha256",
        "counts",
        "manifest_sha256",
        "partition_count",
        "record_count",
        "records_bytes",
        "records_sha256",
        "run_fingerprint",
        "shard_index",
    }
    for index, raw_shard in enumerate(raw_shards):
        shard = _mapping(raw_shard, name=f"order-eight shard {index}")
        _exact_keys(shard, shard_fields, name=f"order-eight shard {index}")
        shard_index = _integer(shard["shard_index"], name=f"order-eight shard {index} index")
        if shard_index != index:
            raise CampaignError("order-eight shard indices must be exactly 0 through 63")
        shard_counts = _counts(shard["counts"], name=f"order-eight shard {index}.counts")
        shard_totals = {
            key: _integer(shard[key], name=f"order-eight shard {index}.{key}") for key in totals
        }
        if (
            shard_totals["check_evaluations"] != 3 * shard_totals["partition_count"]
            or sum(shard_counts.values()) != shard_totals["record_count"]
            or any(shard_counts[name] for name in ("candidate_unsat", "error", "unknown"))
        ):
            raise CampaignError(f"order-eight shard {index} has inconsistent scientific totals")
        for name in summed_totals:
            summed_totals[name] += shard_totals[name]
        for name in summed_counts:
            summed_counts[name] += shard_counts[name]
        run_fingerprint = _digest(
            shard["run_fingerprint"], name=f"order-eight shard {index} fingerprint"
        )
        if run_fingerprint in fingerprints:
            raise CampaignError("order-eight shard fingerprints must be unique")
        fingerprints.add(run_fingerprint)
        manifest_sha256 = _digest(
            shard["manifest_sha256"], name=f"order-eight shard {index} manifest SHA-256"
        )
        completion_sha256 = _digest(
            shard["completion_sha256"], name=f"order-eight shard {index} completion SHA-256"
        )
        records_sha256 = _digest(
            shard["records_sha256"], name=f"order-eight shard {index} records SHA-256"
        )
        checked = {
            **shard_totals,
            "completion_sha256": completion_sha256,
            "counts": shard_counts,
            "manifest_sha256": manifest_sha256,
            "records_sha256": records_sha256,
            "run_fingerprint": run_fingerprint,
            "shard_index": index,
        }
        checked_shards.append(checked)
        reduced_receipts.append(
            {
                "manifest_sha256": manifest_sha256,
                "record_count": shard_totals["record_count"],
                "run_fingerprint": run_fingerprint,
                "shard_index": index,
            }
        )
    if summed_totals != totals or summed_counts != total_counts:
        raise CampaignError("order-eight shard receipts do not sum to the exact-union totals")

    reduce_path = receipt_path.parent / "reduce-complete.json"
    reduce_receipt, reduce_bound = _load_bound_json(reduce_path, name="order-eight reduce receipt")
    bound_files.append(reduce_bound)
    _exact_keys(
        reduce_receipt,
        {
            "campaign_contract_sha256",
            "code_commit",
            "counts",
            "geng_sha256",
            "launcher_archive_sha256",
            "launcher_sha256",
            "order",
            "receipts",
            "runtime_receipt_sha256",
            "schema_version",
            "shard_count",
            "status",
            "toolkit",
            "totals",
            "wheel_sha256",
        },
        name="order-eight reduce receipt",
    )
    checked_reduce_counts = _counts(reduce_receipt["counts"], name="order-eight reduce counts")
    raw_reduce_totals = _mapping(reduce_receipt["totals"], name="order-eight reduce totals")
    _exact_keys(raw_reduce_totals, set(totals), name="order-eight reduce totals")
    checked_reduce_totals = {
        key: _integer(raw_reduce_totals[key], name=f"order-eight reduce totals.{key}")
        for key in totals
    }
    raw_reduce_receipts = _sequence(
        reduce_receipt["receipts"], name="order-eight reduced shard receipts"
    )
    checked_reduce_receipts: list[dict[str, object]] = []
    if len(raw_reduce_receipts) != 64:
        raise CampaignError("order-eight reducer must contain 64 shard receipts")
    for index, raw_reduced in enumerate(raw_reduce_receipts):
        reduced = _mapping(raw_reduced, name=f"order-eight reduced shard {index}")
        _exact_keys(
            reduced,
            {"manifest_sha256", "record_count", "run_fingerprint", "shard_index"},
            name=f"order-eight reduced shard {index}",
        )
        checked_reduce_receipts.append(
            {
                "manifest_sha256": _digest(
                    reduced["manifest_sha256"],
                    name=f"order-eight reduced shard {index} manifest SHA-256",
                ),
                "record_count": _integer(
                    reduced["record_count"], name=f"order-eight reduced shard {index} record count"
                ),
                "run_fingerprint": _digest(
                    reduced["run_fingerprint"],
                    name=f"order-eight reduced shard {index} fingerprint",
                ),
                "shard_index": _integer(
                    reduced["shard_index"], name=f"order-eight reduced shard {index} index"
                ),
            }
        )
    reduce_contract = _digest(
        reduce_receipt["campaign_contract_sha256"],
        name="order-eight reduce campaign contract SHA-256",
    )
    if (
        _digest(receipt["reduce_receipt_sha256"], name="order-eight reduce receipt SHA-256")
        != reduce_bound.sha256
        or reduce_receipt["schema_version"] != REDUCE_SCHEMA
        or reduce_receipt["status"] != "reduce_complete"
        or _integer(reduce_receipt["order"], name="order-eight reduce order", positive=True) != 8
        or _integer(
            reduce_receipt["shard_count"], name="order-eight reduce shard count", positive=True
        )
        != 64
        or _commit(reduce_receipt["code_commit"], name="order-eight reduce code commit")
        != code_commit
        or _digest(
            reduce_receipt["launcher_archive_sha256"],
            name="order-eight reduce launcher archive SHA-256",
        )
        != launcher_archive_sha256
        or _digest(reduce_receipt["launcher_sha256"], name="order-eight reduce launcher SHA-256")
        != launcher_digest
        or _digest(
            reduce_receipt["runtime_receipt_sha256"],
            name="order-eight reduce runtime receipt SHA-256",
        )
        != runtime_receipt_sha256
        or _digest(reduce_receipt["geng_sha256"], name="order-eight reduce geng SHA-256")
        != geng_sha256
        or _digest(reduce_receipt["wheel_sha256"], name="order-eight reduce wheel SHA-256")
        != wheel_sha256
        or _toolkit_identity(reduce_receipt["toolkit"], name="order-eight reduce toolkit")
        != receipt_toolkit
        or reduce_contract != campaign_contract_sha256
        or checked_reduce_counts != total_counts
        or checked_reduce_totals != totals
        or checked_reduce_receipts != reduced_receipts
    ):
        raise CampaignError("order-eight reduce receipt does not match the exact-union receipt")

    if receipt_path.parent.name != "status":
        raise CampaignError("order-eight exact-union receipt must live in the status directory")
    scratch = receipt_path.parent.parent
    _require_real_directory(scratch, name="order-eight scratch root")
    _require_real_directory(scratch / "sealed", name="order-eight sealed directory")
    _validate_campaign_contract(
        scratch / "sealed" / "campaign-contract.json",
        expected_sha256=campaign_contract_sha256,
        scratch=scratch,
        runtime=runtime,
        code_commit=code_commit,
        launcher_archive_sha256=launcher_archive_sha256,
        launcher_digest=launcher_digest,
        runtime_receipt_sha256=runtime_receipt_sha256,
        geng_sha256=geng_sha256,
        toolkit_version=toolkit_version,
        wheel_sha256=wheel_sha256,
        bound_files=bound_files,
    )
    artifact_inventory = None
    if verify_artifacts:
        artifact_inventory = _validate_artifacts(
            scratch,
            shards=checked_shards,
            code_commit=code_commit,
            launcher_archive_sha256=launcher_archive_sha256,
            launcher_digest=launcher_digest,
            runtime_receipt_sha256=runtime_receipt_sha256,
            geng_sha256=geng_sha256,
            wheel_sha256=wheel_sha256,
            toolkit=receipt_toolkit,
            campaign_contract_sha256=campaign_contract_sha256,
            bound_files=bound_files,
        )

    _require_bound_files_unchanged(bound_files)
    if _launcher_digest(runtime / "launcher") != launcher_digest:
        raise CampaignError("sealed runtime launcher changed during prerequisite validation")
    try:
        stable_python_target = (runtime / "venv" / "bin" / "python").resolve(strict=True)
    except OSError as exc:
        raise CampaignError(f"cannot re-resolve runtime Python executable: {exc}") from exc
    if stable_python_target != python_target:
        raise CampaignError("runtime Python entry point changed during prerequisite validation")

    return Order8Prerequisite(
        geng_sha256=geng_sha256,
        receipt_sha256=receipt_bound.sha256,
        runtime_receipt_sha256=runtime_receipt_sha256,
        code_commit=code_commit,
        wheel_sha256=wheel_sha256,
        launcher_sha256=launcher_digest,
        launcher_archive_sha256=launcher_archive_sha256,
        order8_campaign_contract_sha256=campaign_contract_sha256,
        artifact_inventory=artifact_inventory,
    )


def write_order8_gate(
    path: Path,
    prerequisite: Order8Prerequisite,
    *,
    job_id: str,
    campaign_contract_sha256: str,
    order8_replay_sha256: str,
) -> tuple[Order8Gate, str]:
    """Atomically emit a strict gate after full artifact verification."""

    if not isinstance(prerequisite, Order8Prerequisite):
        raise CampaignError("prerequisite must be an Order8Prerequisite")
    root_sha256 = prerequisite.order8_artifact_root_sha256
    if root_sha256 is None:
        raise CampaignError("an order-eight gate requires full actual-artifact verification")
    job_id = _string(job_id, name="order-eight prerequisite job id")
    if not job_id.isdigit():
        raise CampaignError("order-eight prerequisite job id must be numeric")
    gate = Order8Gate(
        job_id=job_id,
        order8_receipt_sha256=prerequisite.receipt_sha256,
        order8_artifact_root_sha256=root_sha256,
        order8_replay_sha256=_digest(
            order8_replay_sha256, name="order-eight semantic replay SHA-256"
        ),
        runtime_receipt_sha256=prerequisite.runtime_receipt_sha256,
        code_commit=prerequisite.code_commit,
        geng_sha256=prerequisite.geng_sha256,
        wheel_sha256=prerequisite.wheel_sha256,
        launcher_sha256=prerequisite.launcher_sha256,
        launcher_archive_sha256=prerequisite.launcher_archive_sha256,
        campaign_contract_sha256=_digest(
            campaign_contract_sha256, name="current campaign contract SHA-256"
        ),
    )
    atomic_json(path, gate.to_dict())
    bound = _bind_file(path, name="order-eight prerequisite gate")
    return gate, bound.sha256


def validate_order8_gate(
    status_or_path: Path,
    *,
    runtime_receipt: Mapping[str, Any],
    expected_receipt_sha256: str,
    expected_campaign_contract_sha256: str | None = None,
) -> tuple[Order8Gate, str]:
    """Validate a gate receipt and return its parsed identity plus file hash."""

    requested = Path(status_or_path)
    path = requested / ORDER8_GATE_FILENAME if requested.is_dir() else requested
    payload, bound = _load_bound_json(path, name="order-eight prerequisite gate")
    _exact_keys(
        payload,
        {
            "campaign_contract_sha256",
            "code_commit",
            "geng_sha256",
            "job_id",
            "launcher_archive_sha256",
            "launcher_sha256",
            "order8_artifact_root_sha256",
            "order8_receipt_sha256",
            "order8_replay_sha256",
            "runtime_receipt_sha256",
            "schema_version",
            "status",
            "wheel_sha256",
        },
        name="order-eight prerequisite gate",
    )
    if (
        payload["schema_version"] != ORDER8_GATE_SCHEMA
        or payload["status"] != "order8_prerequisite_complete"
    ):
        raise CampaignError("order-eight prerequisite gate has the wrong schema or status")
    job_id = _string(payload["job_id"], name="order-eight prerequisite gate job id")
    if not job_id.isdigit():
        raise CampaignError("order-eight prerequisite gate job id must be numeric")
    expected_receipt_sha256 = _digest(
        expected_receipt_sha256, name="expected order-eight receipt SHA-256"
    )
    runtime = _mapping(runtime_receipt, name="runtime receipt")
    runtime_sha256 = hashlib.sha256(canonical_json_bytes(runtime)).hexdigest()
    gate = Order8Gate(
        job_id=job_id,
        order8_receipt_sha256=_digest(
            payload["order8_receipt_sha256"], name="gate order-eight receipt SHA-256"
        ),
        order8_artifact_root_sha256=_digest(
            payload["order8_artifact_root_sha256"], name="gate artifact root SHA-256"
        ),
        order8_replay_sha256=_digest(
            payload["order8_replay_sha256"], name="gate semantic replay SHA-256"
        ),
        runtime_receipt_sha256=_digest(
            payload["runtime_receipt_sha256"], name="gate runtime receipt SHA-256"
        ),
        code_commit=_commit(payload["code_commit"], name="gate code commit"),
        geng_sha256=_digest(payload["geng_sha256"], name="gate geng SHA-256"),
        wheel_sha256=_digest(payload["wheel_sha256"], name="gate wheel SHA-256"),
        launcher_sha256=_digest(payload["launcher_sha256"], name="gate launcher SHA-256"),
        launcher_archive_sha256=_digest(
            payload["launcher_archive_sha256"], name="gate launcher archive SHA-256"
        ),
        campaign_contract_sha256=_digest(
            payload["campaign_contract_sha256"], name="gate campaign contract SHA-256"
        ),
    )
    runtime_identity = {
        "code_commit": _commit(runtime.get("code_commit"), name="runtime code commit"),
        "geng_sha256": _digest(runtime.get("geng_sha256"), name="runtime geng SHA-256"),
        "wheel_sha256": _digest(runtime.get("wheel_sha256"), name="runtime wheel SHA-256"),
        "launcher_sha256": _digest(runtime.get("launcher_sha256"), name="runtime launcher SHA-256"),
        "launcher_archive_sha256": _digest(
            runtime.get("launcher_archive_sha256"), name="runtime launcher archive SHA-256"
        ),
    }
    if (
        gate.order8_receipt_sha256 != expected_receipt_sha256
        or gate.runtime_receipt_sha256 != runtime_sha256
        or any(getattr(gate, name) != value for name, value in runtime_identity.items())
    ):
        raise CampaignError("order-eight prerequisite gate does not match the runtime contract")
    if expected_campaign_contract_sha256 is not None and gate.campaign_contract_sha256 != _digest(
        expected_campaign_contract_sha256, name="expected campaign contract SHA-256"
    ):
        raise CampaignError("order-eight prerequisite gate binds a foreign campaign contract")
    final = _bind_file(path, name="order-eight prerequisite gate")
    if final.identity != bound.identity or final.sha256 != bound.sha256:
        raise CampaignError("order-eight prerequisite gate changed during validation")
    return gate, bound.sha256


__all__ = [
    "EXACT_UNION_SCHEMA",
    "ORDER8_ARTIFACT_INVENTORY_SCHEMA",
    "ORDER8_GATE_FILENAME",
    "ORDER8_GATE_SCHEMA",
    "REDUCE_SCHEMA",
    "RUNTIME_SCHEMA",
    "VALIDATION_SCHEMA",
    "ArtifactInventoryEntry",
    "FileIdentity",
    "Order8ArtifactInventory",
    "Order8Gate",
    "Order8Prerequisite",
    "validate_order8_gate",
    "validate_order8_prerequisite",
    "write_order8_gate",
]
