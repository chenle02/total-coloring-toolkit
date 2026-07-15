#!/usr/bin/env python3
"""Dry-run-first submission of a guarded Easley universal-census campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from scripts.easley.common import (
    CAMPAIGN_CONTRACT_SCHEMA,
    CampaignError,
    atomic_bytes,
    atomic_json,
    canonical_json_bytes,
    launcher_archive_bytes,
    launcher_files,
    launcher_sha256,
    load_json_with_snapshot,
    require_easley_shard_count,
    require_no_python_bytecode,
    require_readonly_tree,
    require_regular_file,
    sha256_file,
    slurm_command,
)
from scripts.easley.prerequisite import RUNTIME_SCHEMA, validate_order8_prerequisite


@dataclass(frozen=True, slots=True)
class Profile:
    order: int
    shards: int
    split_depth: int
    array_concurrency: int
    array_partition: str
    records: int
    verified: int
    skipped: int
    partitions: int
    checks: int
    census_time: str
    exact_partition: str
    exact_time: str


PROFILES: Final = {
    "order8-smoke": Profile(
        order=8,
        shards=64,
        split_depth=2,
        array_concurrency=64,
        array_partition="nova_short",
        records=12_346,
        verified=11_922,
        skipped=424,
        partitions=514_050,
        checks=1_542_150,
        census_time="01:00:00",
        exact_partition="nova_short",
        exact_time="02:00:00",
    ),
    "order9-production": Profile(
        order=9,
        shards=2048,
        split_depth=2,
        array_concurrency=2048,
        array_partition="nova_short",
        records=274_668,
        verified=259_197,
        skipped=15_471,
        partitions=26_634_630,
        checks=79_903_890,
        census_time="02:00:00",
        exact_partition="nova_long",
        exact_time="1-00:00:00",
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--profile", required=True, choices=tuple(PROFILES))
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--toolkit-version", required=True)
    parser.add_argument("--nauty-tar", required=True)
    parser.add_argument("--geng-sha256")
    parser.add_argument("--runtime-receipt-sha256")
    parser.add_argument(
        "--order8-receipt",
        help="required exact-union completion receipt for order9-production",
    )
    parser.add_argument(
        "--array-concurrency",
        type=int,
        help="maximum simultaneously running array tasks; defaults to the profile target",
    )
    parser.add_argument(
        "--bootstrap-only",
        action="store_true",
        help="build a non-scientific runtime; submit no census jobs",
    )
    parser.add_argument("--submit", action="store_true", help="submit jobs; default is dry-run")
    return parser


def _safe_export(environment: dict[str, str]) -> str:
    for name, value in environment.items():
        if not name.startswith("TC_") or any(character in value for character in ",\n\r\x00"):
            raise CampaignError(f"unsafe Slurm export value: {name}")
    return ",".join(f"{name}={value}" for name, value in sorted(environment.items()))


def _order8_geng_pin(
    arguments: argparse.Namespace,
    *,
    runtime: Path,
    launcher_archive_sha256: str,
    launcher_digest: str,
) -> tuple[str | None, str | None, Path | None, str | None]:
    if arguments.profile != "order9-production":
        requested = arguments.geng_sha256
        if requested is not None and (
            not isinstance(requested, str)
            or len(requested) != 64
            or any(character not in "0123456789abcdef" for character in requested)
        ):
            raise CampaignError("geng SHA-256 must be 64 lowercase hexadecimal characters")
        if arguments.bootstrap_only:
            if arguments.runtime_receipt_sha256 is not None:
                raise CampaignError("bootstrap-only mode must not pin a prior runtime receipt")
            return requested, None, None, None
        if requested is None:
            raise CampaignError("scientific order8 requires --geng-sha256")
        runtime_receipt_sha256 = arguments.runtime_receipt_sha256
        if runtime_receipt_sha256 is None:
            raise CampaignError("scientific order8 requires --runtime-receipt-sha256")
        if len(runtime_receipt_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in runtime_receipt_sha256
        ):
            raise CampaignError("runtime receipt SHA-256 must be canonical lowercase hexadecimal")
        return requested, None, None, runtime_receipt_sha256
    if not arguments.order8_receipt:
        raise CampaignError("order9-production requires --order8-receipt")
    receipt_path = Path(arguments.order8_receipt).resolve(strict=True)
    prerequisite = validate_order8_prerequisite(
        receipt_path,
        runtime=runtime.resolve(strict=True),
        code_commit=arguments.code_commit,
        launcher_archive_sha256=launcher_archive_sha256,
        launcher_digest=launcher_digest,
        toolkit_version=arguments.toolkit_version,
        wheel_sha256=arguments.wheel_sha256,
    )
    if arguments.geng_sha256 is not None and arguments.geng_sha256 != prerequisite.geng_sha256:
        raise CampaignError("requested geng digest disagrees with the order-eight prerequisite")
    if (
        arguments.runtime_receipt_sha256 is not None
        and arguments.runtime_receipt_sha256 != prerequisite.runtime_receipt_sha256
    ):
        raise CampaignError("requested runtime receipt disagrees with the order-eight prerequisite")
    return (
        prerequisite.geng_sha256,
        prerequisite.receipt_sha256,
        receipt_path,
        prerequisite.runtime_receipt_sha256,
    )


def _validate_pinned_runtime(
    runtime: Path,
    *,
    runtime_receipt_sha256: str,
    code_commit: str,
    launcher_archive_sha256: str,
    launcher_digest: str,
    toolkit_version: str,
    wheel_sha256: str,
    geng_sha256: str,
) -> None:
    receipt, snapshot = load_json_with_snapshot(runtime / "runtime-receipt.json")
    if snapshot.sha256 != runtime_receipt_sha256:
        raise CampaignError("runtime receipt does not match --runtime-receipt-sha256")
    expected_fields = {
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
    }
    if set(receipt) != expected_fields:
        raise CampaignError("pinned runtime receipt has an unexpected field set")
    expected = {
        "code_commit": code_commit,
        "geng_sha256": geng_sha256,
        "launcher_archive_sha256": launcher_archive_sha256,
        "launcher_sha256": launcher_digest,
        "toolkit_version": toolkit_version,
        "wheel_sha256": wheel_sha256,
    }
    if any(receipt.get(name) != value for name, value in expected.items()):
        raise CampaignError("pinned runtime receipt disagrees with the scientific campaign")
    if (
        receipt.get("schema_version") != RUNTIME_SCHEMA
        or receipt.get("nauty_tar_sha256")
        != "9fc4edae04f88a0f5883985be3b39cf7f898fd6cc96e96b9ee25452743cc1b5b"
        or receipt.get("nauty_version") != "2.9.3"
        or receipt.get("smoke_order") != 4
        or receipt.get("smoke_record_count") != 11
        or receipt.get("runtime_python") != "venv/bin/python"
    ):
        raise CampaignError("pinned runtime was not built from the required nauty source")
    bootstrap_job_id = receipt.get("bootstrap_job_id")
    if not isinstance(bootstrap_job_id, str) or not bootstrap_job_id.isdigit():
        raise CampaignError("pinned runtime has a malformed bootstrap job id")
    toolkit_identity = receipt.get("toolkit_identity")
    if (
        not isinstance(toolkit_identity, dict)
        or set(toolkit_identity)
        != {
            "distribution_version",
            "python_implementation",
            "python_version",
            "source_sha256",
        }
        or toolkit_identity.get("distribution_version") != toolkit_version
        or not isinstance(toolkit_identity.get("python_implementation"), str)
        or not isinstance(toolkit_identity.get("python_version"), str)
        or not isinstance(toolkit_identity.get("source_sha256"), str)
        or len(toolkit_identity["source_sha256"]) != 64
        or any(
            character not in "0123456789abcdef" for character in toolkit_identity["source_sha256"]
        )
    ):
        raise CampaignError("pinned runtime has a malformed toolkit identity")
    if launcher_sha256(runtime / "launcher") != launcher_digest:
        raise CampaignError("pinned runtime launcher does not match the release checkout")
    if sha256_file(require_regular_file(runtime / "bin" / "geng")) != geng_sha256:
        raise CampaignError("pinned runtime geng executable has the wrong SHA-256")
    runtime_python_sha256 = receipt.get("runtime_python_sha256")
    if not isinstance(runtime_python_sha256, str):
        raise CampaignError("pinned runtime receipt has no Python executable SHA-256")
    python = require_regular_file((runtime / "venv" / "bin" / "python").resolve(strict=True))
    if sha256_file(python) != runtime_python_sha256:
        raise CampaignError("pinned runtime Python executable has the wrong SHA-256")
    require_no_python_bytecode(runtime)
    require_readonly_tree(runtime)
    final_receipt, final_snapshot = load_json_with_snapshot(runtime / "runtime-receipt.json")
    if final_receipt != receipt or final_snapshot != snapshot:
        raise CampaignError("pinned runtime changed during submission preflight")


def _wrapper(
    runtime: Path,
    module: str,
    *,
    launcher_archive: Path | None = None,
    launcher_archive_sha256: str | None = None,
) -> str:
    del runtime
    if launcher_archive is not None:
        if launcher_archive_sha256 is None:
            raise CampaignError("bootstrap wrapper requires a launcher archive digest")
        bootstrap = True
    else:
        if launcher_archive_sha256 is not None:
            raise CampaignError("non-bootstrap wrapper received a launcher archive digest")
        bootstrap = False
    prefix = (
        ". /etc/profile.d/modules.sh; module purge >/dev/null 2>&1; "
        "module load python/anaconda/3.12.7"
    )
    isolated = _snapshot_loader_program(module, bootstrap=bootstrap)
    command = f"exec python -I -B -S -c {shlex.quote(isolated)}"
    body = "; ".join((prefix, command))
    return f"set -eu; {body}"


def _snapshot_loader_program(module: str, *, bootstrap: bool) -> str:
    """Return a stdlib-only loader that imports only sealed memory snapshots."""

    program = """import ctypes,fcntl,hashlib,io,json,os,runpy,stat,sys,zipfile
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 1)
F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 2)
F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 4)
F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 8)
def fail(message):
    raise SystemExit("sealed loader: " + message)
def digest(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        fail(name + " is not a canonical SHA-256")
    return value
def create_memfd(name):
    flags = getattr(os, "MFD_CLOEXEC", 1) | getattr(os, "MFD_ALLOW_SEALING", 2)
    native = getattr(os, "memfd_create", None)
    if native is not None:
        return native(name, flags)
    if os.uname().machine not in ("x86_64", "amd64"):
        fail("memfd syscall fallback only supports Easley's x86_64 nodes")
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    descriptor = libc.syscall(
        ctypes.c_long(319),
        ctypes.c_char_p(name.encode("ascii")),
        ctypes.c_uint(flags),
    )
    if descriptor < 0:
        error = ctypes.get_errno()
        fail("memfd_create syscall failed: " + os.strerror(error))
    return int(descriptor)
def snapshot(path, expected, name):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source = os.open(path, flags)
    except OSError as error:
        fail("cannot open " + name + ": " + str(error))
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode):
            fail(name + " is not a regular file")
        blocks = []
        while True:
            block = os.read(source, 1048576)
            if not block:
                break
            blocks.append(block)
        after = os.fstat(source)
    finally:
        os.close(source)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after):
        fail(name + " changed while it was snapshotted")
    data = b"".join(blocks)
    actual = hashlib.sha256(data).hexdigest()
    if expected is not None and actual != digest(expected, name + " digest"):
        fail(name + " SHA-256 mismatch")
    target = create_memfd(name)
    view = memoryview(data)
    while view:
        written = os.write(target, view)
        view = view[written:]
    os.lseek(target, 0, os.SEEK_SET)
    seals = F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE
    fcntl.fcntl(target, F_ADD_SEALS, seals)
    return target, data, actual
def fdpath(descriptor, *, child=False):
    owner = str(os.getpid()) if child else "self"
    return "/proc/" + owner + "/fd/" + str(descriptor)
contract_fd, contract_bytes, _ = snapshot(
    os.environ["TC_CAMPAIGN_CONTRACT"],
    os.environ["TC_CAMPAIGN_CONTRACT_SHA256"],
    "campaign-contract",
)
os.environ["TC_INTERNAL_CAMPAIGN_CONTRACT_FD"] = str(contract_fd)
try:
    contract = json.loads(contract_bytes)
except (UnicodeDecodeError, json.JSONDecodeError) as error:
    fail("campaign contract is invalid JSON: " + str(error))
canonical_contract = json.dumps(
    contract,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8") + b"\\n"
if canonical_contract != contract_bytes:
    fail("campaign contract is not canonical JSON")
if set(contract) != {"environment", "profile", "schema_version"}:
    fail("campaign contract has an unexpected field set")
if contract.get("schema_version") != "total-coloring.easley-campaign.v1":
    fail("campaign contract has an unsupported schema")
sealed_environment = contract.get("environment")
if not isinstance(sealed_environment, dict) or not all(
    isinstance(key, str)
    and key.startswith("TC_")
    and not key.startswith("TC_INTERNAL_")
    and key not in ("TC_CAMPAIGN_CONTRACT", "TC_CAMPAIGN_CONTRACT_SHA256")
    and isinstance(value, str)
    for key, value in sealed_environment.items()
):
    fail("campaign contract environment is malformed")
if contract.get("profile") != sealed_environment.get("TC_PROFILE"):
    fail("campaign contract profile does not match its environment")
actual_environment = {
    key: value
    for key, value in os.environ.items()
    if key.startswith("TC_")
    and not key.startswith("TC_INTERNAL_")
    and key not in ("TC_CAMPAIGN_CONTRACT", "TC_CAMPAIGN_CONTRACT_SHA256")
}
if actual_environment != sealed_environment:
    fail("live job environment does not equal the campaign contract")
launcher_fd, launcher_bytes, _ = snapshot(
    os.environ["TC_LAUNCHER_ARCHIVE"],
    os.environ["TC_LAUNCHER_ARCHIVE_SHA256"],
    "easley-launcher",
)
with zipfile.ZipFile(io.BytesIO(launcher_bytes)) as launcher_zip:
    inventory = []
    seen = set()
    for member in launcher_zip.infolist():
        name = member.filename
        if (
            name in seen
            or name.startswith("/")
            or ".." in name.split("/")
            or not (
                name == "scripts/__init__.py"
                or (name.startswith("scripts/easley/") and name.endswith(".py"))
            )
        ):
            fail("launcher archive has an unsafe source inventory")
        seen.add(name)
        source = launcher_zip.open(member).read()
        inventory.append({"path": name, "sha256": hashlib.sha256(source).hexdigest()})
launcher_source = json.dumps(
    inventory,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8") + b"\\n"
if hashlib.sha256(launcher_source).hexdigest() != os.environ["TC_LAUNCHER_SHA256"]:
    fail("launcher archive does not match the release source inventory")
os.environ["TC_INTERNAL_LAUNCHER_ARCHIVE_FD"] = str(launcher_fd)
wheel_fd, _, _ = snapshot(os.environ["TC_WHEEL"], os.environ["TC_WHEEL_SHA256"], "toolkit-wheel")
os.environ["TC_INTERNAL_WHEEL_FD"] = str(wheel_fd)
"""
    if bootstrap:
        program += """nauty_fd, _, _ = snapshot(os.environ["TC_NAUTY_TAR"], None, "nauty-source")
os.environ["TC_INTERNAL_NAUTY_TAR_FD"] = str(nauty_fd)
os.environ["TC_INTERNAL_NAUTY_TAR_PATH"] = fdpath(nauty_fd, child=True)
"""
    else:
        program += """runtime_receipt_path = os.path.join(
    os.environ["TC_RUNTIME"], "runtime-receipt.json"
)
runtime_fd, runtime_bytes, runtime_sha256 = snapshot(
    runtime_receipt_path,
    os.environ.get("TC_RUNTIME_RECEIPT_SHA256"),
    "runtime-receipt",
)
os.environ["TC_INTERNAL_RUNTIME_RECEIPT_FD"] = str(runtime_fd)
try:
    runtime_receipt = json.loads(runtime_bytes)
except (UnicodeDecodeError, json.JSONDecodeError) as error:
    fail("runtime receipt is invalid JSON: " + str(error))
canonical_runtime = json.dumps(
    runtime_receipt,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8") + b"\\n"
if canonical_runtime != runtime_bytes:
    fail("runtime receipt is not canonical JSON")
bound_environment = (
    ("code_commit", "TC_CODE_COMMIT"),
    ("launcher_archive_sha256", "TC_LAUNCHER_ARCHIVE_SHA256"),
    ("launcher_sha256", "TC_LAUNCHER_SHA256"),
    ("toolkit_version", "TC_TOOLKIT_VERSION"),
    ("wheel_sha256", "TC_WHEEL_SHA256"),
)
for key, environment_name in bound_environment:
    if runtime_receipt.get(key) != os.environ[environment_name]:
        fail("runtime receipt disagrees with " + environment_name)
geng_sha256 = digest(runtime_receipt.get("geng_sha256"), "runtime geng digest")
if os.environ.get("TC_GENG_SHA256", geng_sha256) != geng_sha256:
    fail("runtime geng disagrees with TC_GENG_SHA256")
python_sha256 = digest(runtime_receipt.get("runtime_python_sha256"), "runtime Python digest")
_, _, _ = snapshot(os.path.realpath(sys.executable), python_sha256, "cluster-python")
geng_fd, _, _ = snapshot(os.path.join(os.environ["TC_RUNTIME"], "bin", "geng"), geng_sha256, "geng")
os.environ["TC_INTERNAL_GENG_FD"] = str(geng_fd)
"""
    program += f"""sys.path[:0] = [fdpath(launcher_fd), fdpath(wheel_fd)]
os.environ["TC_INTERNAL_WHEEL_PATH"] = fdpath(wheel_fd, child=True)
os.environ["TC_INTERNAL_LAUNCHER_ARCHIVE_PATH"] = fdpath(launcher_fd, child=True)
runpy.run_module({module!r}, run_name="__main__")
"""
    return program


def _base_sbatch(
    *,
    name: str,
    partition: str,
    time: str,
    memory: str,
    log: Path,
    environment: dict[str, str],
) -> list[str]:
    return [
        "sbatch",
        "--parsable",
        f"--job-name={name}",
        f"--partition={partition}",
        "--nodes=1",
        "--ntasks=1",
        "--cpus-per-task=1",
        f"--mem={memory}",
        f"--time={time}",
        f"--output={log}",
        "--open-mode=append",
        f"--export={_safe_export(environment)}",
    ]


def _submit(command: list[str]) -> str:
    resolved = [slurm_command("sbatch"), *command[1:]]
    completed = subprocess.run(
        resolved,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CampaignError(
            f"sbatch failed ({completed.returncode}): {' '.join(resolved)}\n{completed.stderr}"
        )
    job_id = completed.stdout.strip().split(";", 1)[0]
    if not job_id.isdigit():
        raise CampaignError(f"sbatch returned a malformed job id: {completed.stdout!r}")
    return job_id


def _require_immutable_code_checkout(
    code_root: Path,
    expected_commit: str,
    expected_launcher_sha256: str,
) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=code_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if head.returncode != 0 or head.stdout.strip() != expected_commit:
        raise CampaignError("code root HEAD does not equal --code-commit")
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        cwd=code_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout:
        raise CampaignError("staged toolkit checkout must be completely clean")
    if launcher_sha256(code_root) != expected_launcher_sha256:
        raise CampaignError("Easley launcher digest changed before submission")
    for path in (code_root / "scripts" / "easley").rglob("*"):
        if "__pycache__" in path.parts or (path.is_file() and path.suffix != ".py"):
            raise CampaignError(f"Easley launcher checkout contains an execution byproduct: {path}")
    directories = (
        code_root,
        code_root / "scripts",
        *(path for path in (code_root / "scripts" / "easley").rglob("*") if path.is_dir()),
        code_root / "scripts" / "easley",
    )
    for path in directories:
        if path.is_symlink() or path.lstat().st_mode & 0o222:
            raise CampaignError(f"Easley launcher directory must be read-only: {path}")
    for path in launcher_files(code_root):
        if path.lstat().st_mode & 0o222:
            raise CampaignError(f"Easley launcher source must be read-only: {path}")


class SubmissionInterrupted(RuntimeError):
    """A controlled terminal signal interrupted dependency-graph submission."""


_SUBMISSION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _interrupt_submission(signum: int, _frame: object) -> None:
    raise SubmissionInterrupted(f"submission interrupted by signal {signum}")


@contextmanager
def _defer_submission_signals() -> Iterator[None]:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _SUBMISSION_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    profile = PROFILES[arguments.profile]
    shard_count = require_easley_shard_count(
        profile.shards, name=f"{arguments.profile} shard count"
    )
    array_concurrency = (
        profile.array_concurrency
        if arguments.array_concurrency is None
        else arguments.array_concurrency
    )
    if array_concurrency <= 0 or array_concurrency > shard_count:
        raise CampaignError("array concurrency must lie between 1 and the shard count")
    if arguments.bootstrap_only and arguments.profile != "order8-smoke":
        raise CampaignError("bootstrap-only mode is available only for the order8 profile")
    code_root = Path(arguments.code_root).resolve(strict=True)
    if not (code_root / "scripts" / "easley" / "submit.py").is_file():
        raise CampaignError("code root does not contain the Easley launch modules")
    if len(arguments.code_commit) != 40 or any(
        character not in "0123456789abcdef" for character in arguments.code_commit
    ):
        raise CampaignError("code commit must be a 40-character lowercase Git object id")
    launcher_digest = launcher_sha256(code_root)
    launcher_bytes = launcher_archive_bytes(code_root)
    launcher_archive_sha256 = hashlib.sha256(launcher_bytes).hexdigest()
    requested_scratch = Path(arguments.scratch)
    if requested_scratch.is_symlink():
        raise CampaignError("scratch campaign root must not be a symbolic link")
    scratch = requested_scratch.resolve()
    runtime = Path(arguments.runtime).resolve()
    wheel = require_regular_file(
        Path(arguments.wheel).resolve(strict=True), expected_sha256=arguments.wheel_sha256
    )
    nauty_tar = require_regular_file(Path(arguments.nauty_tar).resolve(strict=True))
    (
        geng_sha256,
        order8_receipt_sha256,
        order8_receipt_path,
        runtime_receipt_sha256,
    ) = _order8_geng_pin(
        arguments,
        runtime=runtime,
        launcher_archive_sha256=launcher_archive_sha256,
        launcher_digest=launcher_digest,
    )
    if arguments.bootstrap_only:
        if runtime.exists():
            raise CampaignError("bootstrap-only submission requires a nonexistent runtime")
    else:
        assert geng_sha256 is not None
        assert runtime_receipt_sha256 is not None
        _validate_pinned_runtime(
            runtime,
            runtime_receipt_sha256=runtime_receipt_sha256,
            code_commit=arguments.code_commit,
            launcher_archive_sha256=launcher_archive_sha256,
            launcher_digest=launcher_digest,
            toolkit_version=arguments.toolkit_version,
            wheel_sha256=arguments.wheel_sha256,
            geng_sha256=geng_sha256,
        )
    environment = {
        "TC_CAMPAIGN_MODE": "bootstrap_only" if arguments.bootstrap_only else "scientific",
        "TC_ARRAY_CONCURRENCY": str(array_concurrency),
        "TC_CHECKPOINT_INTERVAL": "8",
        "TC_CODE_COMMIT": arguments.code_commit,
        "TC_EXPECTED_CHECKS": str(profile.checks),
        "TC_EXPECTED_PARTITIONS": str(profile.partitions),
        "TC_EXPECTED_RECORDS": str(profile.records),
        "TC_EXPECTED_SKIPPED": str(profile.skipped),
        "TC_EXPECTED_VERIFIED": str(profile.verified),
        "TC_MAX_UNION_GRAPHS": "1000000",
        "TC_LAUNCHER_SHA256": launcher_digest,
        "TC_NAUTY_TAR": str(nauty_tar),
        "TC_ORDER": str(profile.order),
        "TC_PROFILE": arguments.profile,
        "TC_RUNTIME": str(runtime),
        "TC_SCRATCH": str(scratch),
        "TC_SHARDS": str(shard_count),
        "TC_SPLIT_DEPTH": str(profile.split_depth),
        "TC_TOOLKIT_VERSION": arguments.toolkit_version,
        "TC_WHEEL": str(wheel),
        "TC_WHEEL_SHA256": arguments.wheel_sha256,
    }
    if geng_sha256:
        environment["TC_GENG_SHA256"] = geng_sha256
    if order8_receipt_sha256:
        environment["TC_ORDER8_RECEIPT_SHA256"] = order8_receipt_sha256
    if order8_receipt_path:
        environment["TC_ORDER8_RECEIPT_PATH"] = str(order8_receipt_path)
    if runtime_receipt_sha256:
        environment["TC_RUNTIME_RECEIPT_SHA256"] = runtime_receipt_sha256
    logs = scratch / "logs"
    launcher_archive = scratch / "sealed" / "easley-launcher.zip"
    environment["TC_LAUNCHER_ARCHIVE"] = str(launcher_archive)
    environment["TC_LAUNCHER_ARCHIVE_SHA256"] = launcher_archive_sha256
    campaign_contract = scratch / "sealed" / "campaign-contract.json"
    campaign_contract_bytes = canonical_json_bytes(
        {
            "environment": dict(environment),
            "profile": arguments.profile,
            "schema_version": CAMPAIGN_CONTRACT_SCHEMA,
        }
    )
    campaign_contract_sha256 = hashlib.sha256(campaign_contract_bytes).hexdigest()
    environment["TC_CAMPAIGN_CONTRACT"] = str(campaign_contract)
    environment["TC_CAMPAIGN_CONTRACT_SHA256"] = campaign_contract_sha256

    commands: list[dict[str, object]] = []
    prerequisite_command: list[str] | None = None
    if profile.order == 9:
        prerequisite_command = [
            *_base_sbatch(
                name="tc-o9-order8-prerequisite",
                partition="nova_short",
                time="02:00:00",
                memory="4G",
                log=logs / "order8-prerequisite-%j.out",
                environment=environment,
            ),
            "--wrap",
            _wrapper(runtime, "scripts.easley.prerequisite_task"),
        ]
        commands.append({"name": "order8-prerequisite", "command": prerequisite_command})
    bootstrap = [
        *_base_sbatch(
            name=f"tc-o{profile.order}-bootstrap",
            partition="nova_short",
            time="01:00:00",
            memory="8G",
            log=logs / "bootstrap-%j.out",
            environment=environment,
        ),
        "--wrap",
        _wrapper(
            runtime,
            "scripts.easley.bootstrap",
            launcher_archive=launcher_archive,
            launcher_archive_sha256=launcher_archive_sha256,
        ),
    ]
    bootstrap_plan: dict[str, object] = {"name": "bootstrap", "command": bootstrap}
    if prerequisite_command is not None:
        bootstrap_plan["dependency"] = "afterok:<order8-prerequisite>"
    commands.append(bootstrap_plan)

    if not arguments.submit:
        planned_jobs: list[dict[str, object]] = list(commands)
        if not arguments.bootstrap_only:
            planned_jobs.extend(
                (
                    {"name": "census-array", "dependency": "afterok:<bootstrap>"},
                    {"name": "validation-array", "dependency": "afterany:<census-array>"},
                    {"name": "reduce", "dependency": "afterany:<validation-array>"},
                    {"name": "exact-union", "dependency": "afterok:<reduce>"},
                )
            )
        payload = {
            "environment": environment,
            "jobs": planned_jobs,
            "profile": arguments.profile,
            "status": "dry_run",
        }
        sys.stdout.buffer.write(canonical_json_bytes(payload))
        return 0

    _require_immutable_code_checkout(code_root, arguments.code_commit, launcher_digest)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        scratch.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise CampaignError("submission requires a nonexistent scratch campaign root") from exc
    logs.mkdir()
    (scratch / "runs").mkdir()
    status = scratch / "status"
    status.mkdir()
    sealed = scratch / "sealed"
    sealed.mkdir(mode=0o700)
    atomic_bytes(launcher_archive, launcher_bytes, mode=0o444)
    atomic_bytes(campaign_contract, campaign_contract_bytes, mode=0o444)
    sealed.chmod(0o555)
    journal_path = status / "submission.json"
    job_ids: dict[str, str] = {}

    def write_journal(state: str, **extra: object) -> None:
        atomic_json(
            journal_path,
            {
                "environment": environment,
                "jobs": job_ids,
                "profile": arguments.profile,
                "status": state,
                **extra,
            },
        )

    write_journal("submitting")
    previous_handlers = {
        signum: signal.signal(signum, _interrupt_submission) for signum in _SUBMISSION_SIGNALS
    }

    def submit_stage(name: str, command: list[str]) -> str:
        with _defer_submission_signals():
            job_id = _submit(command)
            job_ids[name] = job_id
            write_journal("submitting")
        return job_id

    try:
        prerequisite_id: str | None = None
        if prerequisite_command is not None:
            prerequisite_id = submit_stage("order8_prerequisite", prerequisite_command)
        if prerequisite_id is not None:
            bootstrap.insert(-2, f"--dependency=afterok:{prerequisite_id}")
        bootstrap_id = submit_stage("bootstrap", bootstrap)
        if arguments.bootstrap_only:
            write_journal("submitted")
        else:
            census = [
                *_base_sbatch(
                    name=f"tc-o{profile.order}-census",
                    partition=profile.array_partition,
                    time=profile.census_time,
                    memory="4G",
                    log=logs / "census-%A_%a.out",
                    environment=environment,
                ),
                f"--array=0-{shard_count - 1}%{array_concurrency}",
                f"--dependency=afterok:{bootstrap_id}",
                "--signal=B:USR1@300",
                "--requeue",
                "--wrap",
                _wrapper(runtime, "scripts.easley.census_task"),
            ]
            census_id = submit_stage("census_array", census)

            validation = [
                *_base_sbatch(
                    name=f"tc-o{profile.order}-validate",
                    partition=profile.array_partition,
                    time="01:00:00",
                    memory="4G",
                    log=logs / "validate-%A_%a.out",
                    environment=environment,
                ),
                f"--array=0-{shard_count - 1}%{array_concurrency}",
                f"--dependency=afterany:{census_id}",
                "--wrap",
                _wrapper(runtime, "scripts.easley.validate_task"),
            ]
            validation_id = submit_stage("validation_array", validation)

            reduce = [
                *_base_sbatch(
                    name=f"tc-o{profile.order}-reduce",
                    partition="nova_short",
                    time="00:30:00",
                    memory="4G",
                    log=logs / "reduce-%j.out",
                    environment=environment,
                ),
                f"--dependency=afterany:{validation_id}",
                "--wrap",
                _wrapper(runtime, "scripts.easley.reduce"),
            ]
            reduce_id = submit_stage("reduce", reduce)

            exact = [
                *_base_sbatch(
                    name=f"tc-o{profile.order}-exact",
                    partition=profile.exact_partition,
                    time=profile.exact_time,
                    memory="16G",
                    log=logs / "exact-union-%j.out",
                    environment=environment,
                ),
                f"--dependency=afterok:{reduce_id}",
                "--wrap",
                _wrapper(runtime, "scripts.easley.exact_union"),
            ]
            submit_stage("exact_union", exact)
            write_journal("submitted")
    except (CampaignError, KeyboardInterrupt, OSError, SubmissionInterrupted) as exc:
        for signum in _SUBMISSION_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        cancellations: dict[str, object] = {}
        for name, job_id in reversed(tuple(job_ids.items())):
            cancelled = subprocess.run(
                [slurm_command("scancel"), job_id],
                text=True,
                capture_output=True,
                check=False,
            )
            cancellations[name] = {
                "job_id": job_id,
                "returncode": cancelled.returncode,
                "stderr": cancelled.stderr.strip(),
            }
        write_journal(
            "submission_failed",
            cancellations=cancellations,
            error=str(exc),
        )
        raise CampaignError("partial submission was cancelled; inspect submission.json") from exc
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    payload = {
        "environment": environment,
        "jobs": job_ids,
        "profile": arguments.profile,
        "status": "submitted",
    }
    sys.stdout.buffer.write(canonical_json_bytes(payload))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CampaignError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "status": "error"}), file=sys.stderr)
        raise SystemExit(2) from exc
