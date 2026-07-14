#!/usr/bin/env python3
"""Build and audit release artifacts from the sdist boundary.

This script has no third-party imports. Run it from the locked development
environment, which supplies ``build`` and ``twine``::

    uv run python scripts/package_gate.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

PUBLIC_SCHEMA_NAMES = (
    "census-completion-v1.schema.json",
    "census-manifest-v1.schema.json",
    "census-record-v1.schema.json",
    "graph-v1.schema.json",
    "total-coloring-certificate-v1.schema.json",
)

ROOT_SDIST_FILES = frozenset(
    {
        ".gitignore",
        "CHANGELOG.md",
        "CITATION.cff",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "pyproject.toml",
    }
)
TREE_SUFFIXES = {
    "docs": frozenset({".md"}),
    "schemas": frozenset({".json"}),
    "scripts": frozenset({".py"}),
    "src/total_coloring": frozenset({".py", ".typed"}),
    "tests": frozenset({".py"}),
}
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".github",
        ".hypothesis",
        ".mypy_cache",
        ".omx",
        ".pi-subagents",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "hpc",
        "htmlcov",
        "private",
        "raw",
        "results",
        "runs",
        "secrets",
    }
)
FORBIDDEN_SUFFIXES = (".key", ".log", ".pem", ".pyc", ".pyo", ".tmp")
MAX_ARCHIVE_BYTES = 50_000_000
MAX_MEMBER_BYTES = 5_000_000
MAX_MEMBERS = 1_000


class PackageGateError(RuntimeError):
    """A release artifact violates the package contract."""


def _safe_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run(command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=_safe_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        rendered = " ".join(command)
        raise PackageGateError(
            f"command failed ({completed.returncode}): {rendered}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _project_identity(project_root: Path) -> tuple[str, str]:
    document = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = document.get("project")
    if not isinstance(project, dict):
        raise PackageGateError("pyproject.toml is missing [project]")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise PackageGateError("project name and version must be strings")
    normalized_name = re.sub(r"[-_.]+", "_", name)
    if not re.fullmatch(r"[a-z0-9_]+", normalized_name) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)*", version
    ):
        raise PackageGateError(
            "package gate requires a normalized name and numeric release version"
        )
    return normalized_name, version


def _has_forbidden_part(path: PurePosixPath) -> bool:
    return any(part.lower() in FORBIDDEN_PARTS for part in path.parts)


def _assert_safe_member_names(names: Sequence[str]) -> None:
    if len(names) > MAX_MEMBERS:
        raise PackageGateError(f"archive has too many members: {len(names)}")
    if len(names) != len(set(names)):
        raise PackageGateError("archive contains duplicate member names")
    for name in names:
        if not name or "\\" in name or "\x00" in name:
            raise PackageGateError(f"unsafe archive member name: {name!r}")
        raw_parts = name.split("/")
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts):
            raise PackageGateError(f"unsafe archive member path: {name!r}")
        if _has_forbidden_part(path) or name.lower().endswith(FORBIDDEN_SUFFIXES):
            raise PackageGateError(f"forbidden archive member: {name!r}")


def _assert_exact_members(actual: Iterable[str], expected: Iterable[str], label: str) -> None:
    actual_set = set(actual)
    expected_set = set(expected)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    if missing or extra:
        raise PackageGateError(f"{label} membership mismatch; missing={missing!r}; extra={extra!r}")


def _source_files(project_root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for relative in ROOT_SDIST_FILES:
        path = project_root / relative
        if not path.is_file() or path.is_symlink():
            raise PackageGateError(f"required source file is missing or a symlink: {relative}")
        files[relative] = path

    for root_name, suffixes in TREE_SUFFIXES.items():
        root = project_root / root_name
        if not root.is_dir() or root.is_symlink():
            raise PackageGateError(
                f"required source directory is missing or a symlink: {root_name}"
            )
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(project_root).as_posix()
            relative_path = PurePosixPath(relative)
            if _has_forbidden_part(relative_path) or path.suffix not in suffixes:
                continue
            if path.is_symlink():
                raise PackageGateError(f"public source file must not be a symlink: {relative}")
            files[relative] = path

    actual_schemas = tuple(
        sorted(PurePosixPath(name).name for name in files if name.startswith("schemas/"))
    )
    if actual_schemas != PUBLIC_SCHEMA_NAMES:
        raise PackageGateError(
            f"public schema contract mismatch: expected={PUBLIC_SCHEMA_NAMES!r}, "
            f"actual={actual_schemas!r}"
        )
    required = {
        "LICENSE",
        "pyproject.toml",
        "scripts/package_gate.py",
        "src/total_coloring/py.typed",
        "src/total_coloring/schema_resources.py",
    }
    missing = sorted(required - files.keys())
    if missing:
        raise PackageGateError(f"required source surface is missing: {missing!r}")
    return files


def _read_sdist(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PackageGateError("sdist exceeds archive size limit")
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        _assert_safe_member_names(names)
        if any(not member.isfile() for member in members):
            raise PackageGateError("sdist must contain regular files only")
        if any(member.size > MAX_MEMBER_BYTES for member in members):
            raise PackageGateError("sdist member exceeds size limit")
        result: dict[str, bytes] = {}
        for member in members:
            extracted = archive.extractfile(member)
            if extracted is None:
                raise PackageGateError(f"could not read sdist member {member.name!r}")
            result[member.name] = extracted.read()
        return result


def _read_wheel(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PackageGateError("wheel exceeds archive size limit")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        _assert_safe_member_names(names)
        for member in members:
            mode = (member.external_attr >> 16) & 0o170000
            if member.is_dir() or mode == stat.S_IFLNK:
                raise PackageGateError("wheel must contain regular files only")
            if member.file_size > MAX_MEMBER_BYTES:
                raise PackageGateError("wheel member exceeds size limit")
        return {member.filename: archive.read(member) for member in members}


def _extract_sdist(files: dict[str, bytes], destination: Path) -> None:
    for name, content in files.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _one_artifact(directory: Path, pattern: str, label: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise PackageGateError(f"expected exactly one {label}, found {len(matches)}")
    return matches[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run_package_gate(project_root: Path, work_dir: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    normalized_name, version = _project_identity(project_root)
    prefix = f"{normalized_name}-{version}"
    source_files = _source_files(project_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    if any(work_dir.iterdir()):
        raise PackageGateError(f"work directory must be empty: {work_dir}")
    dist = work_dir / "dist"
    dist.mkdir()

    _run([sys.executable, "-m", "build", "--sdist", "--outdir", str(dist), str(project_root)])
    sdist_path = _one_artifact(dist, "*.tar.gz", "sdist")
    sdist_files = _read_sdist(sdist_path)
    expected_sdist = {f"{prefix}/{name}" for name in source_files}
    expected_sdist.add(f"{prefix}/PKG-INFO")
    _assert_exact_members(sdist_files, expected_sdist, "sdist")
    for relative, source in source_files.items():
        if sdist_files[f"{prefix}/{relative}"] != source.read_bytes():
            raise PackageGateError(f"sdist bytes differ from source: {relative}")

    unpacked = work_dir / "unpacked"
    _extract_sdist(sdist_files, unpacked)
    sdist_root = unpacked / prefix
    _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(sdist_root)])
    wheel_path = _one_artifact(dist, "*.whl", "wheel")
    wheel_files = _read_wheel(wheel_path)

    dist_info = f"{prefix}.dist-info"
    expected_wheel = {
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/licenses/LICENSE",
    }
    byte_pairs: dict[str, str] = {}
    for relative in source_files:
        if relative.startswith("src/total_coloring/"):
            wheel_name = relative.removeprefix("src/")
            expected_wheel.add(wheel_name)
            byte_pairs[wheel_name] = relative
        elif relative.startswith("schemas/"):
            wheel_name = f"total_coloring/_schemas/{PurePosixPath(relative).name}"
            expected_wheel.add(wheel_name)
            byte_pairs[wheel_name] = relative
    _assert_exact_members(wheel_files, expected_wheel, "wheel")
    for wheel_name, source_name in byte_pairs.items():
        if wheel_files[wheel_name] != source_files[source_name].read_bytes():
            raise PackageGateError(f"wheel bytes differ from canonical source: {wheel_name}")
    if wheel_files[f"{dist_info}/licenses/LICENSE"] != source_files["LICENSE"].read_bytes():
        raise PackageGateError("wheel license bytes differ from canonical LICENSE")

    _run([sys.executable, "-m", "twine", "check", str(sdist_path), str(wheel_path)])

    venv = work_dir / "venv"
    _run([sys.executable, "-m", "venv", str(venv)])
    python = _venv_python(venv)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            str(wheel_path),
        ]
    )
    _run([str(python), "-m", "pip", "check"])
    foreign = work_dir / "foreign"
    foreign.mkdir()
    _run(
        [str(python), str(sdist_root / "tests" / "installed" / "schema_resources_smoke.py")],
        cwd=foreign,
    )
    graph = foreign / "edge.json"
    graph.write_text(
        '{"edges":[[0,1]],"order":2,"schema_version":"total-coloring.simple-graph.v1"}\n',
        encoding="utf-8",
    )
    solved = _run(
        [
            str(python),
            "-m",
            "total_coloring",
            "solve",
            "--graph",
            str(graph),
            "--colors",
            "3",
        ],
        cwd=foreign,
    )
    payload = json.loads(solved.stdout)
    if not isinstance(payload, dict) or payload.get("status") != "witness":
        raise PackageGateError("installed CLI semantic smoke did not return a witness")

    return {
        "python": sys.version.split()[0],
        "sdist": {
            "filename": sdist_path.name,
            "members": sorted(sdist_files),
            "sha256": _sha256(sdist_path),
        },
        "wheel": {
            "filename": wheel_path.name,
            "members": sorted(wheel_files),
            "sha256": _sha256(wheel_path),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "--work-dir", type=Path, help="empty directory in which to retain artifacts"
    )
    arguments = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    try:
        if arguments.work_dir is not None:
            receipt = run_package_gate(project_root, arguments.work_dir.resolve())
        else:
            with tempfile.TemporaryDirectory(prefix="total-coloring-package-gate-") as temporary:
                receipt = run_package_gate(project_root, Path(temporary))
    except (
        OSError,
        PackageGateError,
        json.JSONDecodeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"package gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
