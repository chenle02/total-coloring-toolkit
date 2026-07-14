from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import cast
from unittest.mock import patch

import pytest

from total_coloring.publishing import (
    BundleVerificationError,
    ConcurrentModificationError,
    PublicationConfig,
    PublicationFile,
    PublicationPlan,
    RepositoryStateError,
    _assert_plan_fresh,
    apply_promotion,
    plan_promotion,
    promote,
)


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.rstrip("\n")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            "https://chenle02.github.io/total-coloring-data/schemas/dataset-manifest-v1.schema.json"
        ),
        "title": "Total Coloring Dataset Manifest",
        "description": "Complete inventory and provenance for one curated dataset release.",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "$schema",
            "schema_version",
            "dataset",
            "release",
            "managed_roots",
            "artifacts",
        ],
        "properties": {
            "$schema": {"const": "schemas/dataset-manifest-v1.schema.json"},
            "schema_version": {"const": "1.0.0"},
            "dataset": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "license", "repository"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    },
                    "title": {"type": "string", "minLength": 1},
                    "license": {"const": "CC-BY-4.0"},
                    "repository": {"type": "string", "format": "uri"},
                },
            },
            "release": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "version",
                    "status",
                    "created_utc",
                    "code_repository",
                    "code_commit",
                ],
                "properties": {
                    "version": {
                        "type": "string",
                        "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(?:-[0-9A-Za-z.-]+)?$",
                    },
                    "status": {"enum": ["development", "candidate", "published"]},
                    "created_utc": {"type": "string", "format": "utc-date-time"},
                    "code_repository": {"type": "string", "format": "uri"},
                    "code_commit": {
                        "type": "string",
                        "pattern": "^(?:UNSET|(?!0{40}$)[0-9a-f]{40})$",
                    },
                },
            },
            "managed_roots": {"type": "array", "const": ["reports", "results"]},
            "artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "path",
                        "role",
                        "media_type",
                        "bytes",
                        "sha256",
                        "description",
                    ],
                    "properties": {
                        "path": {"type": "string", "format": "relative-path"},
                        "role": {"enum": ["result", "report", "certificate", "fixture"]},
                        "media_type": {"type": "string", "minLength": 1},
                        "bytes": {"type": "integer", "minimum": 0},
                        "sha256": {"type": "string", "format": "sha256"},
                        "schema": {"type": "string", "format": "relative-path"},
                        "records": {"type": "integer", "minimum": 0},
                        "description": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    }


def _record_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://chenle02.github.io/total-coloring-data/schemas/result-v1.schema.json",
        "title": "Total Coloring Result Record",
        "description": (
            "One finite coloring outcome with producer provenance. The standalone verifier "
            "enforces the cross-field rule that witness records have a nonempty certificate "
            "and all other statuses have a null certificate."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "record_id",
            "problem_digest",
            "status",
            "producer",
            "parameters",
            "certificate",
        ],
        "properties": {
            "schema_version": {"const": "1.0.0"},
            "record_id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$",
            },
            "problem_digest": {"type": "string", "format": "sha256"},
            "status": {"enum": ["witness", "candidate_unsat", "unknown", "error"]},
            "producer": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repository", "commit", "version"],
                "properties": {
                    "repository": {"type": "string", "format": "uri"},
                    "commit": {
                        "type": "string",
                        "pattern": "^(?!0{40}$)[0-9a-f]{40}$",
                    },
                    "version": {"type": "string", "minLength": 1},
                },
            },
            "parameters": {"type": "object"},
            "certificate": {
                "description": (
                    "A nonempty witness certificate when status is witness; null for "
                    "candidate_unsat, unknown, and error."
                ),
                "type": ["object", "null"],
            },
        },
    }


def _result_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "record_id": "fixture:triangle",
        "problem_digest": "a" * 64,
        "status": "witness",
        "producer": {
            "repository": "https://github.com/chenle02/total-coloring-toolkit",
            "commit": "b" * 40,
            "version": "0.1.0",
        },
        "parameters": {"colors": 3, "order": 3},
        "certificate": {"kind": "test-witness"},
    }


def _make_source(root: Path) -> Path:
    for directory in ("results", "reports", "manifests", "schemas"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    _write_json(root / "schemas/dataset-manifest-v1.schema.json", _manifest_schema())
    _write_json(root / "schemas/result-v1.schema.json", _record_schema())
    result = root / "results/fixture.json"
    _write_json(result, _result_payload())
    digest = _sha256(result)
    _write_json(
        root / "manifests/dataset-manifest.json",
        {
            "$schema": "schemas/dataset-manifest-v1.schema.json",
            "schema_version": "1.0.0",
            "dataset": {
                "id": "total-coloring-data",
                "title": "Total Coloring Data",
                "license": "CC-BY-4.0",
                "repository": "https://github.com/chenle02/total-coloring-data",
            },
            "release": {
                "version": "1.0.0",
                "status": "candidate",
                "created_utc": "2026-07-14T00:00:00Z",
                "code_repository": "https://github.com/chenle02/total-coloring-toolkit",
                "code_commit": "b" * 40,
            },
            "managed_roots": ["reports", "results"],
            "artifacts": [
                {
                    "path": "results/fixture.json",
                    "role": "result",
                    "media_type": "application/json",
                    "bytes": result.stat().st_size,
                    "sha256": digest,
                    "schema": "schemas/result-v1.schema.json",
                    "records": 1,
                    "description": "A deterministic test witness.",
                }
            ],
        },
    )
    (root / "SHA256SUMS").write_text(f"{digest}  results/fixture.json\n", encoding="utf-8")
    return root


def _make_destination(root: Path) -> Path:
    (root / "results").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "README.md").write_text("destination\n", encoding="utf-8")
    (root / "SHA256SUMS").write_text("# unreleased\n", encoding="utf-8")
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "user.name", "Test User")
    _run_git(root, "config", "user.email", "test@example.invalid")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-m", "initial scaffold")
    return root


def _config(source: Path, destination: Path, *allowed: str) -> PublicationConfig:
    return PublicationConfig(
        source_root=source,
        destination_root=destination,
        allowed_dirty_paths=tuple(PurePosixPath(path) for path in allowed),
    )


def _working_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _manifest(source: Path) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((source / "manifests/dataset-manifest.json").read_text(encoding="utf-8")),
    )


def _store_manifest(source: Path, manifest: dict[str, object]) -> None:
    _write_json(source / "manifests/dataset-manifest.json", manifest)


def _replace_result(source: Path, record: dict[str, object]) -> None:
    result = source / "results/fixture.json"
    payload = _result_payload()
    payload.update(record)
    _write_json(result, payload)
    _refresh_result_integrity(source)


def _refresh_result_integrity(source: Path) -> None:
    result = source / "results/fixture.json"
    digest = _sha256(result)
    manifest = _manifest(source)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    artifact = artifacts[0]
    assert isinstance(artifact, dict)
    artifact["bytes"] = result.stat().st_size
    artifact["sha256"] = digest
    _store_manifest(source, manifest)
    (source / "SHA256SUMS").write_text(f"{digest}  results/fixture.json\n", encoding="utf-8")


def test_dry_run_is_default_and_performs_no_writes(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)

    result = promote(_config(source, destination))

    assert result.applied is False
    assert result.changed_paths == (
        PurePosixPath("SHA256SUMS"),
        PurePosixPath("manifests/dataset-manifest.json"),
        PurePosixPath("results/fixture.json"),
        PurePosixPath("schemas/dataset-manifest-v1.schema.json"),
        PurePosixPath("schemas/result-v1.schema.json"),
    )
    assert _working_files(destination) == before
    assert _run_git(destination, "status", "--porcelain") == ""


def test_apply_replaces_files_but_never_stages_or_commits(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    head = _run_git(destination, "rev-parse", "HEAD")

    result = promote(_config(source, destination), apply=True)

    assert result.applied is True
    assert (destination / "results/fixture.json").read_bytes() == (
        source / "results/fixture.json"
    ).read_bytes()
    assert _run_git(destination, "rev-parse", "HEAD") == head
    assert _run_git(destination, "diff", "--cached", "--name-only") == ""
    assert not (destination / ".git/total-coloring-publish.lock").exists()
    assert set(
        _run_git(destination, "status", "--porcelain", "--untracked-files=all").splitlines()
    ) == {
        " M SHA256SUMS",
        "?? manifests/dataset-manifest.json",
        "?? results/fixture.json",
        "?? schemas/dataset-manifest-v1.schema.json",
        "?? schemas/result-v1.schema.json",
    }


def test_second_plan_is_idempotent_with_allowlisted_publication_changes(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    first = promote(_config(source, destination), apply=True)
    allowed = tuple(str(path) for path in first.changed_paths)

    second = promote(_config(source, destination, *allowed))

    assert second.changed_paths == ()
    assert second.applied is False
    applied_noop = apply_promotion(second.plan)
    assert applied_noop.applied is True
    assert applied_noop.changed_paths == ()


def test_bad_source_hash_fails_before_destination_writes(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["sha256"] = "0" * 64
    _write_json(manifest_path, manifest)
    (source / "SHA256SUMS").write_text(f"{'0' * 64}  results/fixture.json\n", encoding="utf-8")

    with pytest.raises(BundleVerificationError, match="SHA-256 mismatch"):
        plan_promotion(_config(source, destination))

    assert _working_files(destination) == before


def test_weakened_result_schema_fails_trusted_digest_check(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    schema_path = source / "schemas/result-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["oneOf"] = []
    _write_json(schema_path, schema)

    with pytest.raises(BundleVerificationError, match="trusted result schema"):
        plan_promotion(_config(source, destination))


def test_result_artifact_rejects_alternate_schema_path(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    shutil.copy2(
        source / "schemas/result-v1.schema.json",
        source / "schemas/alternate-result.json",
    )
    manifest = _manifest(source)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    artifact = artifacts[0]
    assert isinstance(artifact, dict)
    artifact["schema"] = "schemas/alternate-result.json"
    _store_manifest(source, manifest)

    with pytest.raises(BundleVerificationError, match="must use schemas/result-v1"):
        plan_promotion(_config(source, destination))


def test_nonallowlisted_dirty_destination_is_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (destination / "README.md").write_text("human edit\n", encoding="utf-8")

    with pytest.raises(RepositoryStateError, match="non-allowlisted"):
        plan_promotion(_config(source, destination))


def test_exact_unrelated_dirty_allowlist_is_preserved(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (destination / "README.md").write_text("human edit\n", encoding="utf-8")

    result = promote(_config(source, destination, "README.md"), apply=True)

    assert result.applied is True
    assert (destination / "README.md").read_text(encoding="utf-8") == "human edit\n"


def test_dirty_allowlist_cannot_authorize_target_overwrite(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (destination / "SHA256SUMS").write_text("human edit\n", encoding="utf-8")

    with pytest.raises(RepositoryStateError, match="overlap publication targets"):
        plan_promotion(_config(source, destination, "SHA256SUMS"))


def test_stale_managed_destination_artifact_is_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (destination / "results/stale.json").write_text("{}\n", encoding="utf-8")
    _run_git(destination, "add", "results/stale.json")
    _run_git(destination, "commit", "-m", "add old artifact")

    with pytest.raises(RepositoryStateError, match="absent from the candidate manifest"):
        plan_promotion(_config(source, destination))


def test_candidate_release_requires_code_commit(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release"]["code_commit"] = "UNSET"
    _write_json(manifest_path, manifest)

    with pytest.raises(BundleVerificationError, match="exact code commit"):
        plan_promotion(_config(source, destination))


def test_source_change_after_plan_is_detected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    plan = plan_promotion(_config(source, destination))
    (source / "results/fixture.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ConcurrentModificationError):
        apply_promotion(plan)

    assert not (destination / "results/fixture.json").exists()


def test_destination_head_change_after_plan_is_detected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    plan = plan_promotion(_config(source, destination))
    (destination / "later.txt").write_text("new commit\n", encoding="utf-8")
    _run_git(destination, "add", "later.txt")
    _run_git(destination, "commit", "-m", "concurrent update")

    with pytest.raises(ConcurrentModificationError):
        apply_promotion(plan)

    assert not (destination / "results/fixture.json").exists()


def test_install_failure_rolls_back_all_replaced_files(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)
    plan = plan_promotion(_config(source, destination))
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replacement failure")
        real_replace(source_path, destination_path)

    with (
        patch("total_coloring.publishing.os.replace", side_effect=fail_second_replace),
        pytest.raises(OSError, match="injected replacement failure"),
    ):
        apply_promotion(plan)

    assert _working_files(destination) == before
    assert _run_git(destination, "status", "--porcelain") == ""


def test_late_install_failure_restores_existing_destination_file(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)
    plan = plan_promotion(_config(source, destination))
    real_replace = os.replace
    calls = 0

    def fail_manifest_replace(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("injected late failure")
        real_replace(source_path, destination_path)

    with (
        patch("total_coloring.publishing.os.replace", side_effect=fail_manifest_replace),
        pytest.raises(OSError, match="injected late failure"),
    ):
        apply_promotion(plan)

    assert _working_files(destination) == before
    assert (destination / "SHA256SUMS").read_text(encoding="utf-8") == "# unreleased\n"


def test_staging_corruption_is_detected_before_destination_writes(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)
    plan = plan_promotion(_config(source, destination))
    real_copyfile = shutil.copyfile

    def corrupt_copy(source_path: Path, destination_path: Path) -> Path:
        copied = real_copyfile(source_path, destination_path)
        Path(destination_path).write_bytes(b"corrupted")
        return copied

    with (
        patch("total_coloring.publishing.shutil.copyfile", side_effect=corrupt_copy),
        pytest.raises(ConcurrentModificationError, match="while staging"),
    ):
        apply_promotion(plan)

    assert _working_files(destination) == before


def test_destination_change_after_staging_is_detected_and_preserved(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    plan = plan_promotion(_config(source, destination))
    real_assert_fresh = _assert_plan_fresh
    calls = 0

    def mutate_after_second_check(active_plan: PublicationPlan) -> PublicationPlan:
        nonlocal calls
        calls += 1
        fresh = real_assert_fresh(active_plan)
        if calls == 2:
            (destination / "SHA256SUMS").write_text("concurrent edit\n", encoding="utf-8")
        return fresh

    with (
        patch(
            "total_coloring.publishing._assert_plan_fresh",
            side_effect=mutate_after_second_check,
        ),
        pytest.raises(ConcurrentModificationError, match="before replacing"),
    ):
        apply_promotion(plan)

    assert (destination / "SHA256SUMS").read_text(encoding="utf-8") == "concurrent edit\n"
    assert not (destination / "results/fixture.json").exists()


def test_file_fsync_failure_occurs_before_destination_writes(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)
    plan = plan_promotion(_config(source, destination))

    with (
        patch("total_coloring.publishing._fsync_file", side_effect=OSError("file fsync failed")),
        pytest.raises(OSError, match="file fsync failed"),
    ):
        apply_promotion(plan)

    assert _working_files(destination) == before


def test_directory_fsync_failure_rolls_back_the_just_replaced_file(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)
    plan = plan_promotion(_config(source, destination))
    from total_coloring import publishing

    real_fsync_directory = publishing._fsync_directory
    calls = 0

    def fail_first_directory_fsync(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync failed")
        real_fsync_directory(path)

    with (
        patch(
            "total_coloring.publishing._fsync_directory",
            side_effect=fail_first_directory_fsync,
        ),
        pytest.raises(OSError, match="directory fsync failed"),
    ):
        apply_promotion(plan)

    assert _working_files(destination) == before
    assert _run_git(destination, "status", "--porcelain") == ""


def test_forged_plan_entries_are_rejected_before_writes(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "README.md").write_text("forged overwrite\n", encoding="utf-8")
    before = _working_files(destination)
    plan = plan_promotion(_config(source, destination))
    forged_file = PublicationFile(
        path=PurePosixPath("README.md"),
        kind="artifact",
        bytes=(source / "README.md").stat().st_size,
        sha256=_sha256(source / "README.md"),
        destination_sha256=_sha256(destination / "README.md"),
    )
    forged = replace(plan, files=(*plan.files, forged_file))

    with pytest.raises(ConcurrentModificationError, match="freshly inspected plan"):
        apply_promotion(forged)

    assert _working_files(destination) == before


def test_existing_promotion_lock_fails_closed(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    before = _working_files(destination)
    plan = plan_promotion(_config(source, destination))
    (destination / ".git/total-coloring-publish.lock").write_text(
        "held by another process\n", encoding="utf-8"
    )

    with pytest.raises(RepositoryStateError, match="another promotion is active"):
        apply_promotion(plan)

    assert _working_files(destination) == before


@pytest.mark.parametrize("hidden_name", [".secret-token", ".gitkeep"])
def test_candidate_bundle_rejects_hidden_managed_files(tmp_path: Path, hidden_name: str) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "results" / hidden_name).write_text("hidden\n", encoding="utf-8")

    expected = "placeholder forbidden" if hidden_name == ".gitkeep" else "hidden managed file"
    with pytest.raises(BundleVerificationError, match=expected):
        plan_promotion(_config(source, destination))


def test_development_bundle_allows_only_root_gitkeep_placeholder(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest = _manifest(source)
    release = manifest["release"]
    assert isinstance(release, dict)
    release["status"] = "development"
    _store_manifest(source, manifest)
    (source / "results/.gitkeep").write_text("\n", encoding="utf-8")

    plan = plan_promotion(_config(source, destination))

    assert PurePosixPath("results/.gitkeep") not in tuple(item.path for item in plan.files)


def test_expected_code_commit_is_enforced(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    config = replace(_config(source, destination), expected_code_commit="c" * 40)

    with pytest.raises(BundleVerificationError, match="configured generating commit"):
        plan_promotion(config)

    with pytest.raises(ValueError, match="nonzero lowercase"):
        replace(config, expected_code_commit="0" * 40)


def test_expected_commit_applies_to_source_not_previous_destination_release(
    tmp_path: Path,
) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "results/fixture.json").unlink()
    manifest = _manifest(source)
    manifest["artifacts"] = []
    _store_manifest(source, manifest)
    (source / "SHA256SUMS").write_text("# empty release\n", encoding="utf-8")
    promote(_config(source, destination), apply=True)
    _run_git(destination, "add", ".")
    _run_git(destination, "commit", "-m", "publish old commit")
    release = manifest["release"]
    assert isinstance(release, dict)
    release["version"] = "1.1.0"
    release["code_commit"] = "c" * 40
    _store_manifest(source, manifest)
    config = replace(_config(source, destination), expected_code_commit="c" * 40)

    plan = plan_promotion(config)

    assert PurePosixPath("manifests/dataset-manifest.json") in plan.changed_paths


def test_machine_result_requires_schema_and_consistent_certificate(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest = _manifest(source)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    artifact = artifacts[0]
    assert isinstance(artifact, dict)
    artifact.pop("schema")
    _store_manifest(source, manifest)
    with pytest.raises(BundleVerificationError, match="must use schemas/result-v1"):
        plan_promotion(_config(source, destination))

    source = _make_source(tmp_path / "source-null-witness")
    _replace_result(
        source,
        {
            "schema_version": "1.0.0",
            "status": "witness",
            "problem_digest": "a" * 64,
            "certificate": None,
        },
    )
    with pytest.raises(BundleVerificationError, match="witness status requires"):
        plan_promotion(_config(source, destination))

    source = _make_source(tmp_path / "source-empty-witness")
    _replace_result(source, {"certificate": {}})
    with pytest.raises(BundleVerificationError, match="nonempty object certificate"):
        plan_promotion(_config(source, destination))

    source = _make_source(tmp_path / "source-candidate-with-certificate")
    _replace_result(
        source,
        {
            "schema_version": "1.0.0",
            "status": "candidate_unsat",
            "problem_digest": "a" * 64,
            "certificate": {"claimed": "negative-proof"},
        },
    )
    with pytest.raises(BundleVerificationError, match="requires a null certificate"):
        plan_promotion(_config(source, destination))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repository", "https://example.invalid/other-toolkit", "producer.repository"),
        ("commit", "c" * 40, "producer.commit"),
    ],
)
def test_result_producer_must_match_release_provenance(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    producer = {
        "repository": "https://github.com/chenle02/total-coloring-toolkit",
        "commit": "b" * 40,
        "version": "a-build-version-independent-of-the-dataset-version",
    }
    producer[field] = value
    _replace_result(source, {"producer": producer})

    with pytest.raises(BundleVerificationError, match=message):
        plan_promotion(_config(source, destination))


def test_duplicate_result_record_id_across_artifacts_is_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    second_result = source / "results/fixture-2.json"
    _write_json(second_result, _result_payload())
    second_digest = _sha256(second_result)
    manifest = _manifest(source)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    first_artifact = artifacts[0]
    assert isinstance(first_artifact, dict)
    second_artifact = dict(first_artifact)
    second_artifact.update(
        {
            "path": "results/fixture-2.json",
            "bytes": second_result.stat().st_size,
            "sha256": second_digest,
            "description": "Duplicate record identifier fixture.",
        }
    )
    artifacts.append(second_artifact)
    artifacts.sort(key=lambda artifact: artifact["path"])
    _store_manifest(source, manifest)
    first_digest = _sha256(source / "results/fixture.json")
    (source / "SHA256SUMS").write_text(
        f"{second_digest}  results/fixture-2.json\n{first_digest}  results/fixture.json\n",
        encoding="utf-8",
    )

    with pytest.raises(BundleVerificationError, match="duplicate result record_id"):
        plan_promotion(_config(source, destination))


@pytest.mark.parametrize(
    "nonstandard_value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "infinity", "negative-infinity"],
)
def test_result_json_rejects_nonstandard_numeric_constants(
    tmp_path: Path, nonstandard_value: float
) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    payload = _result_payload()
    parameters = payload["parameters"]
    assert isinstance(parameters, dict)
    parameters["timeout"] = nonstandard_value
    (source / "results/fixture.json").write_text(
        json.dumps(payload, allow_nan=True) + "\n", encoding="utf-8"
    )
    _refresh_result_integrity(source)

    with pytest.raises(BundleVerificationError, match="nonstandard JSON constant"):
        plan_promotion(_config(source, destination))


def test_json_duplicate_keys_are_rejected_in_results_and_manifests(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source-result")
    destination = _make_destination(tmp_path / "destination")
    raw_result = json.dumps(_result_payload(), sort_keys=True).replace(
        '"status": "witness"',
        '"status": "error", "status": "witness"',
    )
    (source / "results/fixture.json").write_text(raw_result + "\n", encoding="utf-8")
    _refresh_result_integrity(source)
    with pytest.raises(BundleVerificationError, match="duplicate JSON object key 'status'"):
        plan_promotion(_config(source, destination))

    source = _make_source(tmp_path / "source-manifest")
    manifest_path = source / "manifests/dataset-manifest.json"
    raw_manifest = manifest_path.read_text(encoding="utf-8").replace(
        '"schema_version": "1.0.0"',
        '"schema_version": "0.0.0",\n  "schema_version": "1.0.0"',
        1,
    )
    manifest_path.write_text(raw_manifest, encoding="utf-8")
    with pytest.raises(BundleVerificationError, match="duplicate JSON object key 'schema_version'"):
        plan_promotion(_config(source, destination))


def test_immutable_artifact_path_cannot_be_reused_for_different_bytes(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    promote(_config(source, destination), apply=True)
    _run_git(destination, "add", ".")
    _run_git(destination, "commit", "-m", "publish candidate")
    _replace_result(
        source,
        {
            "schema_version": "1.0.0",
            "status": "candidate_unsat",
            "problem_digest": "d" * 64,
            "certificate": None,
        },
    )

    with pytest.raises(RepositoryStateError, match="immutable artifact path"):
        plan_promotion(_config(source, destination))


def test_release_identity_downgrade_and_status_regression_are_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest = _manifest(source)
    release = manifest["release"]
    assert isinstance(release, dict)
    release["version"] = "2.0.0"
    release["status"] = "published"
    _store_manifest(source, manifest)
    promote(_config(source, destination), apply=True)
    _run_git(destination, "add", ".")
    _run_git(destination, "commit", "-m", "publish version two")

    release["version"] = "1.9.0"
    _store_manifest(source, manifest)
    with pytest.raises(RepositoryStateError, match="downgrade forbidden"):
        plan_promotion(_config(source, destination))

    release["version"] = "2.0.0"
    release["status"] = "candidate"
    _store_manifest(source, manifest)
    with pytest.raises(RepositoryStateError, match="status regression"):
        plan_promotion(_config(source, destination))

    release["status"] = "published"
    dataset = manifest["dataset"]
    assert isinstance(dataset, dict)
    dataset["title"] = "Mutated published metadata"
    _store_manifest(source, manifest)
    with pytest.raises(RepositoryStateError, match="published release version is immutable"):
        plan_promotion(_config(source, destination))

    dataset["title"] = "Total Coloring Data"
    dataset["repository"] = "https://example.invalid/other-data"
    _store_manifest(source, manifest)
    with pytest.raises(BundleVerificationError, match="configured public repository"):
        plan_promotion(_config(source, destination))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("created_utc", "2026-07-14T00:00:00+00:00", "canonical UTC"),
        ("code_commit", "0" * 40, "nonzero exact code commit"),
    ],
)
def test_release_provenance_guards_are_independent_of_schema_validation(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest = _manifest(source)
    release = manifest["release"]
    assert isinstance(release, dict)
    release[field] = value
    _store_manifest(source, manifest)

    with (
        patch("total_coloring.publishing._validate_document"),
        pytest.raises(BundleVerificationError, match=message),
    ):
        plan_promotion(_config(source, destination))


def test_config_rejects_ambiguous_or_unsafe_allowlists(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unique and path-sorted"):
        PublicationConfig(
            source_root=tmp_path / "source",
            destination_root=tmp_path / "destination",
            allowed_dirty_paths=(PurePosixPath("z"), PurePosixPath("a")),
        )
    with pytest.raises(ValueError, match="safe relative"):
        PublicationConfig(
            source_root=tmp_path / "source",
            destination_root=tmp_path / "destination",
            allowed_dirty_paths=(PurePosixPath("../escape"),),
        )
