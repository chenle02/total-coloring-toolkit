from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from total_coloring.geng import GengSpec
from total_coloring.publishing import (
    BundleVerificationError,
    ExternalArtifactFile,
    PublicationConfig,
    plan_promotion,
)
from total_coloring.universal_census import UniversalCensusConfig, run_universal_census
from total_coloring.universal_release import UniversalReleaseConfig, export_universal_release

CODE_COMMIT = "b" * 40
EXTERNAL_NAME = PurePosixPath("archives/order-2-universal-census-replay-v1.tar.gz")


def _data_repository() -> Path:
    configured = os.environ.get("TOTAL_COLORING_DATA_REPO")
    if configured is None:
        pytest.skip("set TOTAL_COLORING_DATA_REPO for cross-repository release tests")
    root = Path(configured).expanduser().resolve()
    if not (root / "scripts/verify_release.py").is_file():
        pytest.skip("TOTAL_COLORING_DATA_REPO does not contain the standalone verifier")
    return root


def _destination(root: Path) -> Path:
    root.mkdir()
    (root / "reports").mkdir()
    (root / "results").mkdir()
    (root / "README.md").write_text("destination\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "bootstrap"], check=True)
    return root


def _standalone(bundle: Path, archive: Path) -> subprocess.CompletedProcess[str]:
    verifier = _data_repository() / "scripts/verify_release.py"
    return subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--root",
            str(bundle),
            "--external-file",
            f"{EXTERNAL_NAME.as_posix()}={archive}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="ascii",
    )


def _refresh_summary_integrity(bundle: Path, summary: dict[str, Any]) -> None:
    summary_path = next((bundle / "results").glob("*-universal-census-summary-v1.json"))
    _write_json(summary_path, summary)
    summary_digest = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    summary_relative = summary_path.relative_to(bundle).as_posix()
    manifest_path = bundle / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(item for item in manifest["artifacts"] if item["path"] == summary_relative)
    artifact["bytes"] = summary_path.stat().st_size
    artifact["sha256"] = summary_digest
    _write_json(manifest_path, manifest)
    (bundle / "SHA256SUMS").write_text(f"{summary_digest}  {summary_relative}\n", encoding="ascii")


@pytest.mark.skipif(
    shutil.which("geng") is None and shutil.which("nauty-geng") is None,
    reason="nauty geng is required for actual-output integration",
)
def test_actual_universal_run_is_accepted_by_both_public_verifiers_and_tamper_rejected(
    tmp_path: Path,
) -> None:
    _data_repository()
    run = tmp_path / "run-order-2"
    run_universal_census(UniversalCensusConfig(GengSpec(2)), run)
    release = export_universal_release(
        (run,),
        UniversalReleaseConfig(
            bundle_root=tmp_path / "bundle",
            archive_path=tmp_path / "order-2-replay.tar.gz",
            summary_id="order-2-universal-census",
            created_utc="2026-07-14T12:00:00Z",
            release_version="1.0.0-rc.1",
            code_commit=CODE_COMMIT,
            external_artifact=EXTERNAL_NAME,
            external_url=(
                "https://github.com/chenle02/total-coloring-data/releases/download/"
                "v1.0.0-rc.1/order-2-universal-census-replay-v1.tar.gz"
            ),
        ),
    )
    standalone = _standalone(release.bundle_root, release.archive_path)
    assert standalone.returncode == 0, standalone.stderr

    publication = PublicationConfig(
        source_root=release.bundle_root,
        destination_root=_destination(tmp_path / "destination"),
        expected_code_commit=CODE_COMMIT,
        external_files=(ExternalArtifactFile(EXTERNAL_NAME, release.archive_path),),
    )
    plan_promotion(publication)

    release.archive_path.write_bytes(release.archive_path.read_bytes() + b"tamper")
    standalone = _standalone(release.bundle_root, release.archive_path)
    assert standalone.returncode == 1
    assert "external-file-bytes" in standalone.stderr
    with pytest.raises(BundleVerificationError, match="byte count mismatch"):
        plan_promotion(publication)


@pytest.mark.skipif(
    shutil.which("geng") is None and shutil.which("nauty-geng") is None,
    reason="nauty geng is required for actual-output integration",
)
def test_cross_repository_contract_fails_closed_on_adversarial_metadata(
    tmp_path: Path,
) -> None:
    _data_repository()
    run = tmp_path / "run-order-2"
    run_universal_census(UniversalCensusConfig(GengSpec(2)), run)
    baseline = export_universal_release(
        (run,),
        UniversalReleaseConfig(
            bundle_root=tmp_path / "baseline-bundle",
            archive_path=tmp_path / "baseline-replay.tar.gz",
            summary_id="order-2-universal-census",
            created_utc="2026-07-14T12:00:00Z",
            release_version="1.0.0-rc.1",
            code_commit=CODE_COMMIT,
            external_artifact=EXTERNAL_NAME,
            external_url=(
                "https://github.com/chenle02/total-coloring-data/releases/download/"
                "v1.0.0-rc.1/order-2-universal-census-replay-v1.tar.gz"
            ),
        ),
    )

    cases: tuple[tuple[str, object], ...] = (
        ("zero-order", 0),
        ("boolean-order", True),
        ("floating-order", 2.0),
        ("build-metadata", "1.0.0+build.1"),
        ("trailing-newline-version", "1.0.0\n"),
        ("lone-surrogate", "\ud800"),
        (
            "nonnormal-url",
            "https://user@github.com/order-2-universal-census-replay-v1.tar.gz",
        ),
        (
            "uppercase-url",
            "HTTPS://github.com/order-2-universal-census-replay-v1.tar.gz",
        ),
        (
            "percent-url",
            "https://github.com/v%31/order-2-universal-census-replay-v1.tar.gz",
        ),
    )
    for name, adversarial in cases:
        bundle = tmp_path / f"bundle-{name}"
        archive = tmp_path / f"archive-{name}.tar.gz"
        shutil.copytree(baseline.bundle_root, bundle)
        shutil.copyfile(baseline.archive_path, archive)
        summary_path = next((bundle / "results").glob("*-universal-census-summary-v1.json"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        manifest_path = bundle / "manifests/dataset-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if name.endswith("order"):
            summary["runs"][0]["order"] = adversarial
            summary["claims"][0]["orders"] = [adversarial]
        elif name == "lone-surrogate":
            summary["summary_id"] = adversarial
        elif name in {"build-metadata", "trailing-newline-version"}:
            manifest["release"]["version"] = adversarial
            _write_json(manifest_path, manifest)
        else:
            summary["replay_archive"]["url"] = adversarial
            manifest["external_artifacts"][0]["url"] = adversarial
            _write_json(manifest_path, manifest)
        _refresh_summary_integrity(bundle, summary)

        standalone = _standalone(bundle, archive)
        assert standalone.returncode == 1, name
        publication = PublicationConfig(
            source_root=bundle,
            destination_root=_destination(tmp_path / f"destination-{name}"),
            expected_code_commit=CODE_COMMIT,
            external_files=(ExternalArtifactFile(EXTERNAL_NAME, archive),),
        )
        with pytest.raises(BundleVerificationError):
            plan_promotion(publication)
