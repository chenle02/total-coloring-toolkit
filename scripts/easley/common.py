"""Shared, dependency-free safety helpers for Easley jobs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_METADATA_BYTES = 4 * 1024 * 1024
CAMPAIGN_CONTRACT_SCHEMA = "total-coloring.easley-campaign.v1"
_INTERNAL_ENV_PREFIX = "TC_INTERNAL_"


class CampaignError(RuntimeError):
    """The immutable campaign contract or an expected artifact is invalid."""


@dataclass(frozen=True, slots=True)
class JsonFileSnapshot:
    """Descriptor-bracketed identity of one canonical JSON artifact."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


def _stat_identity(status: os.stat_result, *, sha256: str) -> JsonFileSnapshot:
    return JsonFileSnapshot(
        device=status.st_dev,
        inode=status.st_ino,
        mode=status.st_mode,
        size=status.st_size,
        mtime_ns=status.st_mtime_ns,
        ctime_ns=status.st_ctime_ns,
        sha256=sha256,
    )


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise CampaignError(f"required environment variable is missing: {name}")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise CampaignError(f"environment variable contains unsafe characters: {name}")
    return value


def positive_env(name: str) -> int:
    raw = require_env(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise CampaignError(f"{name} must be a positive integer") from exc
    if value <= 0 or str(value) != raw:
        raise CampaignError(f"{name} must be a canonical positive integer")
    return value


def nonnegative_env(name: str) -> int:
    raw = require_env(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise CampaignError(f"{name} must be a nonnegative integer") from exc
    if value < 0 or str(value) != raw:
        raise CampaignError(f"{name} must be a canonical nonnegative integer")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sealed_snapshot_fd(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        descriptor = int(raw)
    except ValueError as exc:
        raise CampaignError(f"{name} must name a canonical file descriptor") from exc
    if descriptor < 0 or str(descriptor) != raw:
        raise CampaignError(f"{name} must name a canonical file descriptor")
    try:
        status = os.fstat(descriptor)
    except OSError as exc:
        raise CampaignError(f"{name} does not name an open file descriptor") from exc
    if not stat.S_ISREG(status.st_mode):
        raise CampaignError(f"{name} does not name a regular snapshot")
    try:
        import fcntl

        seals = fcntl.fcntl(descriptor, getattr(fcntl, "F_GET_SEALS", 1034))
        required = (
            getattr(fcntl, "F_SEAL_SEAL", 1)
            | getattr(fcntl, "F_SEAL_SHRINK", 2)
            | getattr(fcntl, "F_SEAL_GROW", 4)
            | getattr(fcntl, "F_SEAL_WRITE", 8)
        )
    except (AttributeError, OSError) as exc:
        raise CampaignError(f"{name} is not a sealed Linux memory file") from exc
    if seals & required != required:
        raise CampaignError(f"{name} is not immutable")
    return descriptor


def _read_snapshot(descriptor: int, *, maximum: int | None = None) -> bytes:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            size += len(block)
            if maximum is not None and size > maximum:
                raise CampaignError(f"sealed snapshot exceeds {maximum} bytes")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def sha256_snapshot(descriptor: int) -> str:
    return hashlib.sha256(_read_snapshot(descriptor)).hexdigest()


def require_regular_file(path: Path, *, expected_sha256: str | None = None) -> Path:
    try:
        status = path.lstat()
    except OSError as exc:
        raise CampaignError(f"cannot inspect required file {path}: {exc}") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise CampaignError(f"required path is not a regular non-symlink file: {path}")
    if expected_sha256 is not None:
        if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
            raise CampaignError("expected SHA-256 must be 64 lowercase hexadecimal characters")
        actual = sha256_file(path)
        if actual != expected_sha256:
            raise CampaignError(
                f"SHA-256 mismatch for {path}: expected {expected_sha256}, got {actual}"
            )
    return path


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def launcher_files(code_root: Path) -> tuple[Path, ...]:
    scripts = code_root / "scripts"
    directory = scripts / "easley"
    root_init = require_regular_file(scripts / "__init__.py")
    if directory.is_symlink() or not directory.is_dir():
        raise CampaignError("Easley launcher package is missing or symbolic")
    files = [root_init]
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise CampaignError(f"Easley launcher package contains a symbolic link: {path}")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix != ".py":
            raise CampaignError(f"Easley launcher package contains a non-Python file: {path}")
        files.append(require_regular_file(path))
    if len(files) == 1:
        raise CampaignError("Easley launcher source inventory is empty")
    return tuple(files)


def launcher_sha256(code_root: Path) -> str:
    inventory = [
        {
            "path": path.relative_to(code_root).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in launcher_files(code_root)
    ]
    return hashlib.sha256(canonical_json_bytes(inventory)).hexdigest()


def launcher_archive_bytes(code_root: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True
    ) as archive:
        for path in launcher_files(code_root):
            info = zipfile.ZipInfo(
                path.relative_to(code_root).as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100444 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def require_readonly_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            continue
        if status.st_mode & 0o222:
            raise CampaignError(f"runtime contains a writable path: {path}")


def require_no_python_bytecode(root: Path) -> None:
    for path in root.rglob("*"):
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}:
            raise CampaignError(f"sealed runtime contains Python bytecode: {path}")


def atomic_json(path: Path, value: object, *, mode: int = 0o644) -> None:
    atomic_bytes(path, canonical_json_bytes(value), mode=mode)


def atomic_bytes(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def load_json_with_snapshot(path: Path) -> tuple[Mapping[str, Any], JsonFileSnapshot]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CampaignError(f"cannot inspect JSON artifact {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CampaignError(f"JSON artifact is not a regular non-symlink file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignError(f"cannot open JSON artifact {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened, sha256="") != _stat_identity(before, sha256=""):
            raise CampaignError(f"JSON artifact changed between lstat and open: {path}")
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, MAX_METADATA_BYTES + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > MAX_METADATA_BYTES:
                break
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > MAX_METADATA_BYTES:
        raise CampaignError(f"JSON artifact exceeds {MAX_METADATA_BYTES} bytes: {path}")
    digest = hashlib.sha256(data).hexdigest()
    snapshot = _stat_identity(opened, sha256=digest)
    if _stat_identity(opened_after, sha256=digest) != snapshot or len(data) != opened.st_size:
        raise CampaignError(f"JSON artifact changed while it was read: {path}")
    try:
        after = path.lstat()
    except OSError as exc:
        raise CampaignError(f"cannot re-inspect JSON artifact {path}: {exc}") from exc
    if _stat_identity(after, sha256=digest) != snapshot:
        raise CampaignError(f"JSON artifact changed while it was read: {path}")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != data:
        raise CampaignError(f"artifact is not canonical JSON with one trailing LF: {path}")
    return value, snapshot


def load_json(path: Path) -> Mapping[str, Any]:
    value, _ = load_json_with_snapshot(path)
    return value


def _load_snapshot_json(descriptor: int) -> Mapping[str, Any]:
    data = _read_snapshot(descriptor, maximum=MAX_METADATA_BYTES)
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignError(f"invalid JSON in sealed file descriptor {descriptor}: {exc}") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != data:
        raise CampaignError("sealed JSON snapshot is not canonical with one trailing LF")
    return value


def require_campaign_contract() -> Mapping[str, Any]:
    expected_sha256 = require_env("TC_CAMPAIGN_CONTRACT_SHA256")
    snapshot = _sealed_snapshot_fd("TC_INTERNAL_CAMPAIGN_CONTRACT_FD")
    if snapshot is None:
        path = require_regular_file(
            Path(require_env("TC_CAMPAIGN_CONTRACT")).resolve(strict=True),
            expected_sha256=expected_sha256,
        )
        contract = load_json(path)
    else:
        if sha256_snapshot(snapshot) != expected_sha256:
            raise CampaignError("sealed campaign contract snapshot has the wrong SHA-256")
        contract = _load_snapshot_json(snapshot)
    if set(contract) != {"environment", "profile", "schema_version"}:
        raise CampaignError("campaign contract has an unexpected field set")
    if contract.get("schema_version") != CAMPAIGN_CONTRACT_SCHEMA:
        raise CampaignError("campaign contract has an unsupported schema")
    environment = contract.get("environment")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str)
        and key.startswith("TC_")
        and not key.startswith(_INTERNAL_ENV_PREFIX)
        and isinstance(value, str)
        and key not in {"TC_CAMPAIGN_CONTRACT", "TC_CAMPAIGN_CONTRACT_SHA256"}
        for key, value in environment.items()
    ):
        raise CampaignError("campaign contract environment is malformed")
    if contract.get("profile") != environment.get("TC_PROFILE"):
        raise CampaignError("campaign contract profile does not match its environment")
    actual = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("TC_")
        and not key.startswith(_INTERNAL_ENV_PREFIX)
        and key not in {"TC_CAMPAIGN_CONTRACT", "TC_CAMPAIGN_CONTRACT_SHA256"}
    }
    if actual != environment:
        raise CampaignError("job environment does not equal the sealed campaign contract")
    if snapshot is None:
        if sha256_file(path) != expected_sha256:
            raise CampaignError("campaign contract changed while it was validated")
    elif sha256_snapshot(snapshot) != expected_sha256:
        raise CampaignError("sealed campaign contract snapshot changed while it was validated")
    return contract


def runtime_receipt_sha256() -> str:
    snapshot = _sealed_snapshot_fd("TC_INTERNAL_RUNTIME_RECEIPT_FD")
    if snapshot is not None:
        return sha256_snapshot(snapshot)
    runtime = Path(require_env("TC_RUNTIME")).resolve(strict=True)
    return sha256_file(require_regular_file(runtime / "runtime-receipt.json"))


def runtime_paths() -> tuple[Path, Path, Mapping[str, Any]]:
    require_campaign_contract()
    if require_env("TC_CAMPAIGN_MODE") != "scientific":
        raise CampaignError("scientific jobs require a pinned scientific campaign")
    require_env("TC_GENG_SHA256")
    require_env("TC_RUNTIME_RECEIPT_SHA256")
    runtime = Path(require_env("TC_RUNTIME")).resolve(strict=True)
    receipt_path = runtime / "runtime-receipt.json"
    receipt_snapshot = _sealed_snapshot_fd("TC_INTERNAL_RUNTIME_RECEIPT_FD")
    receipt = (
        load_json(receipt_path)
        if receipt_snapshot is None
        else _load_snapshot_json(receipt_snapshot)
    )
    receipt_sha256 = runtime_receipt_sha256()
    expected_receipt_sha256 = os.environ.get("TC_RUNTIME_RECEIPT_SHA256")
    if expected_receipt_sha256 is not None and receipt_sha256 != expected_receipt_sha256:
        raise CampaignError("runtime receipt does not match the sealed campaign contract")
    python = runtime / "venv" / "bin" / "python"
    if not python.is_file():
        raise CampaignError(f"runtime Python is missing: {python}")
    receipt_geng_sha256 = receipt.get("geng_sha256")
    if not isinstance(receipt_geng_sha256, str):
        raise CampaignError("runtime receipt has no geng SHA-256")
    requested_geng_sha256 = os.environ.get("TC_GENG_SHA256", receipt_geng_sha256)
    geng_snapshot = _sealed_snapshot_fd("TC_INTERNAL_GENG_FD")
    if geng_snapshot is None:
        geng = require_regular_file(
            runtime / "bin" / "geng",
            expected_sha256=requested_geng_sha256,
        )
    else:
        if sha256_snapshot(geng_snapshot) != requested_geng_sha256:
            raise CampaignError("sealed geng snapshot has the wrong SHA-256")
        geng = Path(f"/proc/{os.getpid()}/fd/{geng_snapshot}")
    if receipt_geng_sha256 != requested_geng_sha256:
        raise CampaignError("runtime receipt does not bind the requested geng SHA-256")
    if receipt.get("wheel_sha256") != require_env("TC_WHEEL_SHA256"):
        raise CampaignError("runtime receipt does not bind the requested wheel SHA-256")
    if receipt.get("toolkit_version") != require_env("TC_TOOLKIT_VERSION"):
        raise CampaignError("runtime receipt does not bind the requested toolkit version")
    if receipt.get("code_commit") != require_env("TC_CODE_COMMIT"):
        raise CampaignError("runtime receipt does not bind the requested code commit")
    if receipt.get("launcher_archive_sha256") != require_env("TC_LAUNCHER_ARCHIVE_SHA256"):
        raise CampaignError("runtime receipt does not bind the submitted launcher archive")
    launcher = runtime / "launcher"
    requested_launcher_sha256 = require_env("TC_LAUNCHER_SHA256")
    initial_launcher_sha256 = launcher_sha256(launcher)
    if (
        receipt.get("launcher_sha256") != requested_launcher_sha256
        or initial_launcher_sha256 != requested_launcher_sha256
    ):
        raise CampaignError("sealed Easley launcher changed after runtime bootstrap")
    runtime_python_sha256 = receipt.get("runtime_python_sha256")
    if not isinstance(runtime_python_sha256, str):
        raise CampaignError("runtime receipt has no Python executable SHA-256")
    require_regular_file(python.resolve(strict=True), expected_sha256=runtime_python_sha256)
    from total_coloring.census import detect_toolkit_identity

    if receipt.get("toolkit_identity") != detect_toolkit_identity().to_dict():
        raise CampaignError("installed toolkit source identity changed after runtime bootstrap")
    require_no_python_bytecode(runtime)
    require_readonly_tree(runtime)
    if launcher_sha256(launcher) != initial_launcher_sha256:
        raise CampaignError("sealed runtime launcher changed while it was validated")
    if runtime_receipt_sha256() != receipt_sha256:
        raise CampaignError("runtime receipt changed while it was validated")
    return python, geng, receipt


def shard_directory(index: int, count: int) -> Path:
    scratch = Path(require_env("TC_SCRATCH")).resolve()
    width = max(3, len(str(count - 1)))
    return scratch / "runs" / f"shard-{index:0{width}d}-of-{count:0{width}d}"


def manifest_totals(path: Path) -> dict[str, int | dict[str, int]]:
    manifest = load_json(path)
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or not all(
        isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
        for key, value in counts.items()
    ):
        raise CampaignError(f"manifest has malformed counts: {path}")
    record_count = manifest.get("record_count")
    partition_count = manifest.get("partition_count")
    if (
        isinstance(record_count, bool)
        or not isinstance(record_count, int)
        or record_count < 0
        or isinstance(partition_count, bool)
        or not isinstance(partition_count, int)
        or partition_count < 0
    ):
        raise CampaignError(f"manifest has malformed totals: {path}")
    return {
        "counts": counts,
        "partition_count": partition_count,
        "record_count": record_count,
    }
