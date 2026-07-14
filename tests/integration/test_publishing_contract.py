from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from total_coloring.publishing import (
    BundleVerificationError,
    PublicationConfig,
    plan_promotion,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


def _data_repository() -> Path:
    configured = os.environ.get("TOTAL_COLORING_DATA_REPO")
    candidate = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[2].parent / "total-coloring-data"
    )
    if not (candidate / "scripts/verify_release.py").is_file():
        pytest.skip(
            "set TOTAL_COLORING_DATA_REPO to run the cross-repository publication contract test"
        )
    return candidate.resolve()


def _candidate_from_actual_scaffold(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source = tmp_path / "candidate"
    shutil.copytree(
        _data_repository(),
        source,
        ignore=shutil.ignore_patterns(".git", ".mypy_cache", ".ruff_cache", "__pycache__", "*.pyc"),
    )
    for placeholder in (source / "reports/.gitkeep", source / "results/.gitkeep"):
        placeholder.unlink(missing_ok=True)

    result = source / "results/cross-verifier.json"
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "record_id": "cross-verifier:triangle",
        "problem_digest": "a" * 64,
        "status": "witness",
        "producer": {
            "repository": "https://github.com/chenle02/total-coloring-toolkit",
            "commit": "b" * 40,
            "version": "0.1.0",
        },
        "parameters": {"colors": 3, "order": 3},
        "certificate": {"assignment": [0, 1, 2]},
    }
    _write_json(result, payload)
    digest = _sha256(result)
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    manifest["release"] = {
        "version": "0.1.0-rc.1",
        "status": "candidate",
        "created_utc": "2026-07-14T03:00:00Z",
        "code_repository": "https://github.com/chenle02/total-coloring-toolkit",
        "code_commit": "b" * 40,
    }
    manifest["artifacts"] = [
        {
            "path": "results/cross-verifier.json",
            "role": "result",
            "media_type": "application/json",
            "bytes": result.stat().st_size,
            "sha256": digest,
            "schema": "schemas/result-v1.schema.json",
            "records": 1,
            "description": "Cross-verifier contract fixture.",
        }
    ]
    _write_json(manifest_path, manifest)
    (source / "SHA256SUMS").write_text(f"{digest}  results/cross-verifier.json\n", encoding="utf-8")
    return source, payload


def _destination(root: Path) -> Path:
    root.mkdir()
    (root / "results").mkdir()
    (root / "reports").mkdir()
    (root / "README.md").write_text("cross-verifier destination\n", encoding="utf-8")
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "user.name", "Cross Verifier")
    _run_git(root, "config", "user.email", "cross-verifier@example.invalid")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-m", "bootstrap")
    return root


def _run_standalone_verifier(source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(source / "scripts/verify_release.py"),
            "--root",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_actual_data_scaffold_agrees_with_publisher_on_acceptance_and_provenance(
    tmp_path: Path,
) -> None:
    source, payload = _candidate_from_actual_scaffold(tmp_path)
    destination = _destination(tmp_path / "destination")
    standalone = _run_standalone_verifier(source)
    assert standalone.returncode == 0, standalone.stderr

    config = PublicationConfig(
        source_root=source,
        destination_root=destination,
        expected_code_commit="b" * 40,
    )
    plan = plan_promotion(config)
    assert PurePosixPath("schemas/result-v1.schema.json") in tuple(item.path for item in plan.files)
    assert PurePosixPath("results/cross-verifier.json") in plan.changed_paths

    producer = payload["producer"]
    assert isinstance(producer, dict)
    producer["commit"] = "c" * 40
    result = source / "results/cross-verifier.json"
    _write_json(result, payload)
    digest = _sha256(result)
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    manifest["artifacts"][0]["bytes"] = result.stat().st_size
    manifest["artifacts"][0]["sha256"] = digest
    _write_json(manifest_path, manifest)
    (source / "SHA256SUMS").write_text(f"{digest}  results/cross-verifier.json\n", encoding="utf-8")

    standalone = _run_standalone_verifier(source)
    assert standalone.returncode == 1
    assert "result-producer-commit" in standalone.stderr
    with pytest.raises(BundleVerificationError, match=r"producer\.commit"):
        plan_promotion(config)
