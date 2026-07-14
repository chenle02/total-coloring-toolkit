#!/usr/bin/env python3
"""Build an immutable nauty/toolkit runtime on an Easley compute node."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from scripts.easley.common import (
    CampaignError,
    atomic_json,
    canonical_json_bytes,
    launcher_sha256,
    load_json,
    positive_env,
    require_campaign_contract,
    require_env,
    require_no_python_bytecode,
    require_readonly_tree,
    require_regular_file,
    runtime_paths,
    sha256_file,
)
from scripts.easley.prerequisite import RUNTIME_SCHEMA, validate_order8_gate

NAUTY_VERSION = "2.9.3"
NAUTY_TAR_SHA256 = "9fc4edae04f88a0f5883985be3b39cf7f898fd6cc96e96b9ee25452743cc1b5b"


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("PYTHON"):
            environment.pop(name)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    isolated_command = list(command)
    if Path(isolated_command[0]).name.startswith("python") and "-I" not in isolated_command[1:]:
        isolated_command[1:1] = ["-I", "-B"]
    completed = subprocess.run(
        isolated_command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise CampaignError(
            f"command failed ({completed.returncode}): "
            f"{' '.join(isolated_command)}\n{completed.stdout}"
        )
    return completed


def _submitted_toolkit_identity() -> dict[str, object]:
    from total_coloring.census import detect_toolkit_identity

    return detect_toolkit_identity().to_dict()


def _submitted_artifact_path(*, internal: str, external: str, expected_sha256: str) -> Path:
    snapshot = os.environ.get(internal)
    if snapshot is None:
        return require_regular_file(
            Path(require_env(external)).resolve(strict=True),
            expected_sha256=expected_sha256,
        )
    path = Path(snapshot)
    if sha256_file(path) != expected_sha256:
        raise CampaignError(f"sealed {external} snapshot has the wrong SHA-256")
    return path


def _freeze_runtime(runtime: Path) -> None:
    paths = sorted(runtime.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            continue
        if path.is_dir():
            path.chmod(0o555)
        else:
            path.chmod(0o555 if status.st_mode & 0o111 else 0o444)
    runtime.chmod(0o555)
    require_readonly_tree(runtime)


def _existing_runtime_is_exact(
    runtime: Path,
    wheel_sha256: str,
    version: str,
    receipt: Mapping[str, object],
) -> bool:
    geng_sha256 = receipt.get("geng_sha256")
    python = runtime / "venv" / "bin" / "python"
    requested_geng_sha256 = os.environ.get("TC_GENG_SHA256")
    exact = bool(
        receipt.get("nauty_tar_sha256") == NAUTY_TAR_SHA256
        and receipt.get("wheel_sha256") == wheel_sha256
        and receipt.get("toolkit_version") == version
        and receipt.get("code_commit") == require_env("TC_CODE_COMMIT")
        and receipt.get("launcher_archive_sha256") == require_env("TC_LAUNCHER_ARCHIVE_SHA256")
        and receipt.get("launcher_sha256") == require_env("TC_LAUNCHER_SHA256")
        and launcher_sha256(runtime / "launcher") == require_env("TC_LAUNCHER_SHA256")
        and isinstance(geng_sha256, str)
        and sha256_file(require_regular_file(runtime / "bin" / "geng")) == geng_sha256
        and (requested_geng_sha256 is None or requested_geng_sha256 == geng_sha256)
        and python.is_file()
        and receipt.get("runtime_python_sha256")
        == sha256_file(require_regular_file(python.resolve(strict=True)))
        and receipt.get("toolkit_identity") == _submitted_toolkit_identity()
    )
    if exact:
        require_no_python_bytecode(runtime)
        require_readonly_tree(runtime)
    return exact


def main() -> int:
    if "SLURM_JOB_ID" not in os.environ:
        raise CampaignError("bootstrap must run as a Slurm compute job")
    if "login" in platform.node().lower():
        raise CampaignError("bootstrap refuses to compile on a login node")
    require_campaign_contract()
    order = positive_env("TC_ORDER")
    if order not in {8, 9}:
        raise CampaignError("Easley bootstrap only supports orders eight and nine")
    campaign_mode = require_env("TC_CAMPAIGN_MODE")
    if campaign_mode not in {"bootstrap_only", "scientific"}:
        raise CampaignError("Easley bootstrap received an unsupported campaign mode")
    if campaign_mode == "bootstrap_only" and order != 8:
        raise CampaignError("bootstrap-only mode is available only for order eight")
    if order == 8 and any(
        name in os.environ for name in ("TC_ORDER8_RECEIPT_PATH", "TC_ORDER8_RECEIPT_SHA256")
    ):
        raise CampaignError("an order-eight campaign must not carry an order-eight prerequisite")

    runtime = Path(require_env("TC_RUNTIME")).resolve()
    if campaign_mode == "bootstrap_only" and runtime.exists():
        raise CampaignError("bootstrap-only mode requires a fresh nonexistent runtime")
    if campaign_mode == "scientific" and not runtime.exists():
        raise CampaignError("scientific mode requires a separately bootstrapped pinned runtime")
    requested_launcher_sha256 = require_env("TC_LAUNCHER_SHA256")
    launcher_archive_sha256 = require_env("TC_LAUNCHER_ARCHIVE_SHA256")
    launcher_archive = _submitted_artifact_path(
        internal="TC_INTERNAL_LAUNCHER_ARCHIVE_PATH",
        external="TC_LAUNCHER_ARCHIVE",
        expected_sha256=launcher_archive_sha256,
    )
    wheel_sha256 = require_env("TC_WHEEL_SHA256")
    toolkit_version = require_env("TC_TOOLKIT_VERSION")
    wheel = _submitted_artifact_path(
        internal="TC_INTERNAL_WHEEL_PATH",
        external="TC_WHEEL",
        expected_sha256=wheel_sha256,
    )
    nauty_tar = _submitted_artifact_path(
        internal="TC_INTERNAL_NAUTY_TAR_PATH",
        external="TC_NAUTY_TAR",
        expected_sha256=NAUTY_TAR_SHA256,
    )
    if runtime.exists():
        _, _, verified_receipt = runtime_paths()
        validated_gate = None
        if order == 9:
            require_env("TC_ORDER8_RECEIPT_PATH")
            status = Path(require_env("TC_SCRATCH")).resolve(strict=True) / "status"
            validated_gate = validate_order8_gate(
                status,
                runtime_receipt=verified_receipt,
                expected_receipt_sha256=require_env("TC_ORDER8_RECEIPT_SHA256"),
                expected_campaign_contract_sha256=require_env("TC_CAMPAIGN_CONTRACT_SHA256"),
            )
        if _existing_runtime_is_exact(
            runtime,
            wheel_sha256,
            toolkit_version,
            verified_receipt,
        ):
            if order == 9:
                assert validated_gate is not None
                final_gate = validate_order8_gate(
                    status,
                    runtime_receipt=verified_receipt,
                    expected_receipt_sha256=require_env("TC_ORDER8_RECEIPT_SHA256"),
                    expected_campaign_contract_sha256=require_env("TC_CAMPAIGN_CONTRACT_SHA256"),
                )
                if final_gate != validated_gate:
                    raise CampaignError(
                        "order-eight prerequisite gate changed during bootstrap validation"
                    )
            sys.stdout.buffer.write(canonical_json_bytes(verified_receipt))
            return 0
        raise CampaignError(f"runtime already exists but does not match this campaign: {runtime}")
    if campaign_mode != "bootstrap_only":
        raise CampaignError("only bootstrap-only mode may create an Easley runtime")

    runtime.parent.mkdir(parents=True, exist_ok=True)
    build_root = Path(require_env("TC_SCRATCH")).resolve() / "bootstrap"
    build_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nauty-build-", dir=build_root) as raw_build:
        build = Path(raw_build)
        wheel_name = Path(require_env("TC_WHEEL")).name
        if not wheel_name.endswith(".whl") or Path(wheel_name).name != wheel_name:
            raise CampaignError("submitted wheel has an unsafe filename")
        staged_wheel = build / wheel_name
        shutil.copyfile(wheel, staged_wheel)
        staged_wheel.chmod(0o444)
        if sha256_file(staged_wheel) != wheel_sha256:
            raise CampaignError("private wheel snapshot changed while it was staged")
        with tarfile.open(nauty_tar, "r:gz") as archive:
            archive.extractall(build, filter="data")
        source = build / "nauty2_9_3"
        if not source.is_dir():
            raise CampaignError("nauty archive did not contain the expected source directory")
        configure = require_regular_file(source / "configure")
        _run([str(configure)], cwd=source)
        workers = max(1, int(os.environ.get("SLURM_CPUS_PER_TASK", "1")))
        _run(["make", f"-j{workers}", "geng"], cwd=source)
        built_geng = require_regular_file(source / "geng")

        temporary = Path(tempfile.mkdtemp(prefix=f".{runtime.name}.", dir=runtime.parent))
        try:
            launcher = temporary / "launcher"
            with zipfile.ZipFile(launcher_archive) as archive:
                members = archive.infolist()
                if not members:
                    raise CampaignError("sealed Easley launcher archive is empty")
                for member in members:
                    relative = PurePosixPath(member.filename)
                    if (
                        member.is_dir()
                        or relative.is_absolute()
                        or any(part in {"", ".", ".."} for part in relative.parts)
                    ):
                        raise CampaignError("sealed Easley launcher archive has an unsafe member")
                    destination = launcher.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(member))
            for destination in launcher.rglob("*.py"):
                destination.chmod(0o444)
            if launcher_sha256(launcher) != requested_launcher_sha256:
                raise CampaignError("copied Easley launcher does not match its submitted identity")
            (temporary / "bin").mkdir()
            shutil.copy2(built_geng, temporary / "bin" / "geng")
            _run([sys.executable, "-m", "venv", str(temporary / "venv")])
            python = temporary / "venv" / "bin" / "python"
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--no-index",
                    str(staged_wheel),
                ]
            )
            if sha256_file(staged_wheel) != wheel_sha256:
                raise CampaignError("private wheel snapshot changed during installation")
            version_output = _run([str(python), "-m", "total_coloring", "--version"]).stdout.strip()
            if version_output != f"total-coloring {toolkit_version}":
                raise CampaignError(
                    f"installed toolkit version mismatch: expected {toolkit_version}, "
                    f"got {version_output!r}"
                )
            toolkit_identity = _submitted_toolkit_identity()
            runtime_python_sha256 = sha256_file(require_regular_file(python.resolve(strict=True)))
            smoke = build / "order-4-runtime-smoke"
            command = [
                str(python),
                "-m",
                "total_coloring",
                "universal-census",
                "--order",
                "4",
                "--output",
                str(smoke),
                "--geng",
                str(temporary / "bin" / "geng"),
                "--checkpoint-interval",
                "2",
            ]
            _run(command)
            _run(command)
            manifest = load_json(smoke / "manifest.json")
            if manifest.get("record_count") != 11:
                raise CampaignError("order-four runtime smoke did not enumerate 11 graphs")
            geng_sha256 = sha256_file(temporary / "bin" / "geng")
            requested_geng_sha256 = os.environ.get("TC_GENG_SHA256")
            if requested_geng_sha256 is not None and requested_geng_sha256 != geng_sha256:
                raise CampaignError(
                    "compiled geng SHA-256 does not match the requested prior digest: "
                    f"expected {requested_geng_sha256}, got {geng_sha256}"
                )
            bytecode_directories = sorted(
                temporary.rglob("__pycache__"),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for bytecode_directory in bytecode_directories:
                if bytecode_directory.exists():
                    shutil.rmtree(bytecode_directory)
            for bytecode in tuple(temporary.rglob("*.py[co]")):
                bytecode.unlink()
            require_no_python_bytecode(temporary)
            receipt = {
                "bootstrap_job_id": os.environ["SLURM_JOB_ID"],
                "code_commit": require_env("TC_CODE_COMMIT"),
                "geng_sha256": geng_sha256,
                "launcher_archive_sha256": launcher_archive_sha256,
                "launcher_sha256": requested_launcher_sha256,
                "nauty_tar_sha256": NAUTY_TAR_SHA256,
                "nauty_version": NAUTY_VERSION,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "runtime_python": str(python.relative_to(temporary)),
                "runtime_python_sha256": runtime_python_sha256,
                "schema_version": RUNTIME_SCHEMA,
                "smoke_order": 4,
                "smoke_record_count": 11,
                "toolkit_version": toolkit_version,
                "toolkit_identity": toolkit_identity,
                "wheel_name": wheel_name,
                "wheel_sha256": wheel_sha256,
            }
            atomic_json(temporary / "runtime-receipt.json", receipt)
            os.replace(temporary, runtime)
            temporary = Path()
            _freeze_runtime(runtime)
        finally:
            if temporary != Path() and temporary.exists():
                shutil.rmtree(temporary)

    sys.stdout.buffer.write(canonical_json_bytes(receipt))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        CampaignError,
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"bootstrap error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
