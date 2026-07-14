from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.easley.common import (
    CAMPAIGN_CONTRACT_SCHEMA,
    canonical_json_bytes,
    launcher_archive_bytes,
    launcher_sha256,
    sha256_file,
)
from scripts.easley.submit import _snapshot_loader_program, _wrapper

ROOT = Path(__file__).resolve().parents[2]
PROBE_MODULE = "scripts.easley.sealed_probe"


@dataclass(frozen=True, slots=True)
class _SealedCampaign:
    environment: dict[str, str]
    launcher: Path
    marker: Path
    modified_launcher: Path
    modified_launcher_marker: Path
    python: Path
    runtime_launcher_marker: Path
    wheel: Path


def _deterministic_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100444 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _fixture_wheel_bytes() -> bytes:
    package = ROOT / "src" / "total_coloring"
    members = {
        f"total_coloring/{path.relative_to(package).as_posix()}": path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed")
    }
    dist_info = "total_coloring_toolkit-0.2.0.dist-info"
    members[f"{dist_info}/METADATA"] = (
        b"Metadata-Version: 2.4\nName: total-coloring-toolkit\nVersion: 0.2.0\n"
    )
    members[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\n"
        b"Generator: sealed-loader-test\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n"
    )
    records = []
    for name, data in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(data)}")
    records.append(f"{dist_info}/RECORD,,")
    members[f"{dist_info}/RECORD"] = ("\n".join(records) + "\n").encode()
    return _deterministic_zip(members)


def _probe_source() -> str:
    return """from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

Path(os.environ["PROBE_MARKER"]).write_text(
    "sealed launcher executed" + chr(10), encoding="utf-8"
)
Path(os.environ["TC_LAUNCHER_ARCHIVE"]).write_bytes(
    Path(os.environ["REPLACEMENT_LAUNCHER"]).read_bytes()
)

from scripts.easley.late_payload import VALUE as launcher_value
from scripts.easley.common import require_campaign_contract
from total_coloring.census import detect_toolkit_identity


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


contract = require_campaign_contract()
required_seals = (
    getattr(fcntl, "F_SEAL_SEAL", 1)
    | getattr(fcntl, "F_SEAL_SHRINK", 2)
    | getattr(fcntl, "F_SEAL_GROW", 4)
    | getattr(fcntl, "F_SEAL_WRITE", 8)
)
snapshots = {}
for name in (
    "TC_INTERNAL_CAMPAIGN_CONTRACT_FD",
    "TC_INTERNAL_LAUNCHER_ARCHIVE_FD",
    "TC_INTERNAL_WHEEL_FD",
    "TC_INTERNAL_RUNTIME_RECEIPT_FD",
    "TC_INTERNAL_GENG_FD",
):
    descriptor = int(os.environ[name])
    status = os.fstat(descriptor)
    try:
        os.write(descriptor, b"not writable")
    except OSError as error:
        write_errno = error.errno
    else:
        write_errno = None
    snapshots[name] = {
        "regular": stat.S_ISREG(status.st_mode),
        "seals": fcntl.fcntl(descriptor, getattr(fcntl, "F_GET_SEALS", 1034)),
        "write_errno": write_errno,
    }

identity = detect_toolkit_identity()
print(
    json.dumps(
        {
            "contract_profile": contract["profile"],
            "flags": {
                "dont_write_bytecode": sys.flags.dont_write_bytecode,
                "isolated": sys.flags.isolated,
                "no_site": sys.flags.no_site,
            },
            "launcher_path": os.environ["TC_INTERNAL_LAUNCHER_ARCHIVE_PATH"],
            "launcher_sha256": file_sha256(
                os.environ["TC_INTERNAL_LAUNCHER_ARCHIVE_PATH"]
            ),
            "launcher_value": launcher_value,
            "pid": os.getpid(),
            "required_seals": required_seals,
            "snapshots": snapshots,
            "toolkit_source_sha256": identity.source_sha256,
            "wheel_path": os.environ["TC_INTERNAL_WHEEL_PATH"],
            "wheel_sha256": file_sha256(os.environ["TC_INTERNAL_WHEEL_PATH"]),
        },
        sort_keys=True,
    )
)
"""


def _memfd_python() -> Path:
    return Path(sys.executable).resolve()


def _build_campaign(tmp_path: Path) -> _SealedCampaign:
    python = _memfd_python()
    launcher_root = tmp_path / "launcher-root"
    easley = launcher_root / "scripts" / "easley"
    easley.mkdir(parents=True)
    (launcher_root / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (easley / "__init__.py").write_text("", encoding="utf-8")
    (easley / "common.py").write_bytes((ROOT / "scripts" / "easley" / "common.py").read_bytes())
    (easley / "late_payload.py").write_text(
        'VALUE = "sealed-launcher-snapshot"\n', encoding="utf-8"
    )
    (easley / "sealed_probe.py").write_text(_probe_source(), encoding="utf-8")

    launcher = tmp_path / "easley-launcher.zip"
    launcher.write_bytes(launcher_archive_bytes(launcher_root))
    launcher_digest = launcher_sha256(launcher_root)
    launcher_archive_sha256 = sha256_file(launcher)

    modified_launcher_marker = tmp_path / "modified-launcher-executed"
    (easley / "late_payload.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['MODIFIED_LAUNCHER_MARKER']).write_text('executed')\n"
        'VALUE = "modified-launcher"\n',
        encoding="utf-8",
    )
    modified_launcher = tmp_path / "modified-launcher.zip"
    modified_launcher.write_bytes(launcher_archive_bytes(launcher_root))

    wheel = tmp_path / "total_coloring_toolkit-0.2.0-py3-none-any.whl"
    wheel.write_bytes(_fixture_wheel_bytes())
    wheel_sha256 = sha256_file(wheel)

    runtime = tmp_path / "runtime"
    geng = runtime / "bin" / "geng"
    geng.parent.mkdir(parents=True)
    geng.write_bytes(b"deterministic sealed geng fixture\n")
    geng.chmod(0o755)
    geng_sha256 = sha256_file(geng)
    runtime_launcher_marker = tmp_path / "runtime-launcher-executed"
    runtime_easley = runtime / "launcher" / "scripts" / "easley"
    runtime_easley.mkdir(parents=True)
    (runtime / "launcher" / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (runtime_easley / "__init__.py").write_text("", encoding="utf-8")
    (runtime_easley / "sealed_probe.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['RUNTIME_LAUNCHER_MARKER']).write_text('executed')\n"
        "raise RuntimeError('mutable runtime launcher was imported')\n",
        encoding="utf-8",
    )

    code_commit = "a" * 40
    toolkit_version = "0.2.0"
    runtime_receipt = runtime / "runtime-receipt.json"
    runtime_receipt.write_bytes(
        canonical_json_bytes(
            {
                "code_commit": code_commit,
                "geng_sha256": geng_sha256,
                "launcher_archive_sha256": launcher_archive_sha256,
                "launcher_sha256": launcher_digest,
                "runtime_python_sha256": sha256_file(python),
                "toolkit_version": toolkit_version,
                "wheel_sha256": wheel_sha256,
            }
        )
    )

    profile = "sealed-loader-unit"
    campaign_environment = {
        "TC_CAMPAIGN_MODE": "scientific",
        "TC_CODE_COMMIT": code_commit,
        "TC_GENG_SHA256": geng_sha256,
        "TC_LAUNCHER_ARCHIVE": str(launcher),
        "TC_LAUNCHER_ARCHIVE_SHA256": launcher_archive_sha256,
        "TC_LAUNCHER_SHA256": launcher_digest,
        "TC_PROFILE": profile,
        "TC_RUNTIME": str(runtime),
        "TC_RUNTIME_RECEIPT_SHA256": sha256_file(runtime_receipt),
        "TC_TOOLKIT_VERSION": toolkit_version,
        "TC_WHEEL": str(wheel),
        "TC_WHEEL_SHA256": wheel_sha256,
    }
    contract = tmp_path / "campaign-contract.json"
    contract.write_bytes(
        canonical_json_bytes(
            {
                "environment": campaign_environment,
                "profile": profile,
                "schema_version": CAMPAIGN_CONTRACT_SCHEMA,
            }
        )
    )
    environment = {name: value for name, value in os.environ.items() if not name.startswith("TC_")}
    environment.update(campaign_environment)
    environment.update(
        {
            "MODIFIED_LAUNCHER_MARKER": str(modified_launcher_marker),
            "PROBE_MARKER": str(tmp_path / "probe-executed"),
            "PYTHONPATH": str(runtime / "launcher"),
            "REPLACEMENT_LAUNCHER": str(modified_launcher),
            "RUNTIME_LAUNCHER_MARKER": str(runtime_launcher_marker),
            "TC_CAMPAIGN_CONTRACT": str(contract),
            "TC_CAMPAIGN_CONTRACT_SHA256": sha256_file(contract),
        }
    )
    return _SealedCampaign(
        environment=environment,
        launcher=launcher,
        marker=Path(environment["PROBE_MARKER"]),
        modified_launcher=modified_launcher,
        modified_launcher_marker=modified_launcher_marker,
        python=python,
        runtime_launcher_marker=runtime_launcher_marker,
        wheel=wheel,
    )


def _run_loader(
    campaign: _SealedCampaign,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(campaign.python),
            "-I",
            "-B",
            "-S",
            "-c",
            _snapshot_loader_program(PROBE_MODULE, bootstrap=False),
        ],
        cwd=campaign.launcher.parent,
        env=campaign.environment if environment is None else environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _reseal_contract(environment: dict[str, str]) -> None:
    contract_environment = {
        key: value
        for key, value in environment.items()
        if key.startswith("TC_")
        and not key.startswith("TC_INTERNAL_")
        and key not in {"TC_CAMPAIGN_CONTRACT", "TC_CAMPAIGN_CONTRACT_SHA256"}
    }
    contract = Path(environment["TC_CAMPAIGN_CONTRACT"])
    contract.write_bytes(
        canonical_json_bytes(
            {
                "environment": contract_environment,
                "profile": contract_environment["TC_PROFILE"],
                "schema_version": CAMPAIGN_CONTRACT_SCHEMA,
            }
        )
    )
    environment["TC_CAMPAIGN_CONTRACT_SHA256"] = sha256_file(contract)


def _launcher_source_sha256(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        inventory = [
            {
                "path": member.filename,
                "sha256": hashlib.sha256(archive.open(member).read()).hexdigest(),
            }
            for member in archive.infolist()
        ]
    return hashlib.sha256(canonical_json_bytes(inventory)).hexdigest()


def test_wrapper_uses_isolated_no_bytecode_no_site_python() -> None:
    wrapper = _wrapper(Path("/ignored/runtime"), PROBE_MODULE)

    assert "exec python -I -B -S -c " in wrapper


def test_loader_executes_only_sealed_snapshots_and_exposes_sealed_internal_fds(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    original_launcher_sha256 = sha256_file(campaign.launcher)

    completed = _run_loader(campaign)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["contract_profile"] == "sealed-loader-unit"
    assert payload["flags"] == {
        "dont_write_bytecode": 1,
        "isolated": 1,
        "no_site": 1,
    }
    assert payload["launcher_value"] == "sealed-launcher-snapshot"
    assert campaign.marker.read_text(encoding="utf-8") == "sealed launcher executed\n"
    assert not campaign.modified_launcher_marker.exists()
    assert not campaign.runtime_launcher_marker.exists()
    assert campaign.launcher.read_bytes() == campaign.modified_launcher.read_bytes()
    assert payload["launcher_sha256"] == original_launcher_sha256
    assert payload["wheel_sha256"] == sha256_file(campaign.wheel)
    assert payload["launcher_path"].startswith(f"/proc/{payload['pid']}/fd/")
    assert payload["wheel_path"].startswith(f"/proc/{payload['pid']}/fd/")
    for snapshot in payload["snapshots"].values():
        assert snapshot == {
            "regular": True,
            "seals": payload["required_seals"],
            "write_errno": errno.EPERM,
        }


@pytest.mark.parametrize(
    ("environment_name", "artifact_name"),
    [
        ("TC_CAMPAIGN_CONTRACT_SHA256", "campaign-contract"),
        ("TC_LAUNCHER_ARCHIVE_SHA256", "easley-launcher"),
        ("TC_WHEEL_SHA256", "toolkit-wheel"),
    ],
)
def test_wrong_snapshot_hash_fails_before_import(
    tmp_path: Path,
    environment_name: str,
    artifact_name: str,
) -> None:
    campaign = _build_campaign(tmp_path)
    environment = dict(campaign.environment)
    environment[environment_name] = "0" * 64
    if environment_name != "TC_CAMPAIGN_CONTRACT_SHA256":
        _reseal_contract(environment)

    completed = _run_loader(campaign, environment=environment)

    assert completed.returncode != 0
    assert f"sealed loader: {artifact_name} SHA-256 mismatch" in completed.stderr
    assert not campaign.marker.exists()
    assert not campaign.modified_launcher_marker.exists()
    assert not campaign.runtime_launcher_marker.exists()


def test_wrong_launcher_source_inventory_digest_fails_before_import(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    environment = dict(campaign.environment)
    environment["TC_LAUNCHER_SHA256"] = "0" * 64
    _reseal_contract(environment)

    completed = _run_loader(campaign, environment=environment)

    assert completed.returncode != 0
    assert (
        "sealed loader: launcher archive does not match the release source inventory"
        in completed.stderr
    )
    assert not campaign.marker.exists()
    assert not campaign.modified_launcher_marker.exists()
    assert not campaign.runtime_launcher_marker.exists()


def test_consistent_live_artifact_substitution_fails_before_import(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    environment = dict(campaign.environment)
    environment["TC_LAUNCHER_ARCHIVE"] = str(campaign.modified_launcher)
    environment["TC_LAUNCHER_ARCHIVE_SHA256"] = sha256_file(campaign.modified_launcher)
    environment["TC_LAUNCHER_SHA256"] = _launcher_source_sha256(campaign.modified_launcher)
    runtime_receipt = Path(environment["TC_RUNTIME"]) / "runtime-receipt.json"
    runtime_payload = json.loads(runtime_receipt.read_bytes())
    runtime_payload["launcher_archive_sha256"] = environment["TC_LAUNCHER_ARCHIVE_SHA256"]
    runtime_payload["launcher_sha256"] = environment["TC_LAUNCHER_SHA256"]
    runtime_receipt.write_bytes(canonical_json_bytes(runtime_payload))
    environment["TC_RUNTIME_RECEIPT_SHA256"] = sha256_file(runtime_receipt)

    completed = _run_loader(campaign, environment=environment)

    assert completed.returncode != 0
    assert "live job environment does not equal the campaign contract" in completed.stderr
    assert not campaign.marker.exists()
    assert not campaign.modified_launcher_marker.exists()
    assert not campaign.runtime_launcher_marker.exists()


def test_toolkit_identity_matches_extracted_and_sealed_memfd_wheel(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    sealed = _run_loader(campaign)
    assert sealed.returncode == 0, sealed.stderr
    sealed_identity = json.loads(sealed.stdout)["toolkit_source_sha256"]

    extracted = tmp_path / "extracted-wheel"
    with zipfile.ZipFile(campaign.wheel) as archive:
        archive.extractall(extracted)
    program = (
        "import sys;"
        f"sys.path.insert(0, {str(extracted)!r});"
        "from total_coloring.census import detect_toolkit_identity;"
        "print(detect_toolkit_identity().source_sha256)"
    )
    installed = subprocess.run(
        [str(campaign.python), "-I", "-B", "-S", "-c", program],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert installed.returncode == 0, installed.stderr
    assert sealed_identity == installed.stdout.strip()
