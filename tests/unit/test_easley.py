from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.easley.common as easley_common
from scripts.easley.bootstrap import _run as bootstrap_run
from scripts.easley.common import (
    MAX_METADATA_BYTES,
    CampaignError,
    atomic_json,
    canonical_json_bytes,
    launcher_archive_bytes,
    launcher_files,
    launcher_sha256,
    load_json,
    require_easley_shard_count,
    sha256_file,
    slurm_command,
)
from scripts.easley.exact_union import main as exact_union_main
from scripts.easley.prerequisite import (
    EXACT_UNION_SCHEMA,
    ORDER8_GATE_FILENAME,
    ORDER8_GATE_SCHEMA,
    REDUCE_SCHEMA,
    RUNTIME_SCHEMA,
    VALIDATION_SCHEMA,
    validate_order8_gate,
    validate_order8_prerequisite,
    write_order8_gate,
)
from scripts.easley.prerequisite_task import main as prerequisite_main
from scripts.easley.submit import (
    SubmissionInterrupted,
    _require_immutable_code_checkout,
    _safe_export,
    main,
)


def _freeze_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444)
    root.chmod(0o555)


@pytest.mark.parametrize("value", [1, 2, 64, 2048])
def test_easley_shard_count_accepts_scheduler_safe_powers_of_two(value: int) -> None:
    assert require_easley_shard_count(value) == value


@pytest.mark.parametrize("value", [True, 0, -2, 3, 2049, 4096, "64"])
def test_easley_shard_count_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(CampaignError, match="shard count"):
        require_easley_shard_count(value)


def test_slurm_command_uses_easley_fallback_after_path_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler_bin = tmp_path / "slurm" / "bin"
    scheduler_bin.mkdir(parents=True)
    sbatch = scheduler_bin / "sbatch"
    sbatch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sbatch.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(easley_common, "_SLURM_FALLBACK_DIRECTORIES", (scheduler_bin,))

    assert slurm_command("sbatch") == str(sbatch)
    with pytest.raises(CampaignError, match="unsupported Slurm command"):
        slurm_command("srun")


def _build_order8_chain(
    root: Path,
    code_root: Path,
    *,
    code_commit: str,
    wheel_sha256: str,
    materialize_artifacts: bool = False,
) -> tuple[Path, Path, str]:
    runtime = root / "runtime"
    for source in launcher_files(code_root):
        destination = runtime / "launcher" / source.relative_to(code_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    python = runtime / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"sealed python")
    python.chmod(0o755)
    geng = runtime / "bin" / "geng"
    geng.parent.mkdir()
    geng.write_bytes(b"sealed geng")
    geng.chmod(0o755)
    geng_sha256 = sha256_file(geng)
    launcher_digest = launcher_sha256(code_root)
    launcher_archive_sha256 = hashlib.sha256(launcher_archive_bytes(code_root)).hexdigest()
    toolkit = {
        "distribution_version": "0.2.0",
        "python_implementation": "CPython",
        "python_version": "3.12.3",
        "source_sha256": "e" * 64,
    }
    runtime_receipt = runtime / "runtime-receipt.json"
    atomic_json(
        runtime_receipt,
        {
            "bootstrap_job_id": "100",
            "code_commit": code_commit,
            "geng_sha256": geng_sha256,
            "launcher_archive_sha256": launcher_archive_sha256,
            "launcher_sha256": launcher_digest,
            "nauty_tar_sha256": (
                "9fc4edae04f88a0f5883985be3b39cf7f898fd6cc96e96b9ee25452743cc1b5b"
            ),
            "nauty_version": "2.9.3",
            "platform": "test-linux",
            "python": "3.12.3",
            "runtime_python": "venv/bin/python",
            "runtime_python_sha256": sha256_file(python),
            "schema_version": RUNTIME_SCHEMA,
            "smoke_order": 4,
            "smoke_record_count": 11,
            "toolkit_identity": toolkit,
            "toolkit_version": "0.2.0",
            "wheel_name": "candidate.whl",
            "wheel_sha256": wheel_sha256,
        },
    )
    runtime_receipt_sha256 = sha256_file(runtime_receipt)
    _freeze_tree(runtime)

    scratch = root / "order8"
    status = scratch / "status"
    status.mkdir(parents=True)
    sealed = scratch / "sealed"
    sealed.mkdir()
    campaign_contract = sealed / "campaign-contract.json"
    atomic_json(
        campaign_contract,
        {
            "environment": {
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
                "TC_PROFILE": "order8-smoke",
                "TC_RUNTIME": str(runtime),
                "TC_RUNTIME_RECEIPT_SHA256": runtime_receipt_sha256,
                "TC_SCRATCH": str(scratch),
                "TC_SHARDS": "64",
                "TC_SPLIT_DEPTH": "2",
                "TC_TOOLKIT_VERSION": "0.2.0",
                "TC_WHEEL_SHA256": wheel_sha256,
            },
            "profile": "order8-smoke",
            "schema_version": "total-coloring.easley-campaign.v1",
        },
    )
    campaign_contract_sha256 = sha256_file(campaign_contract)
    shards: list[dict[str, object]] = []
    reduced: list[dict[str, object]] = []
    records_bytes = 0
    remaining_skipped = 424
    for index in range(64):
        record_count = 193 if index < 58 else 192
        partition_count = 8_033 if index < 2 else 8_032
        skipped = min(record_count, remaining_skipped)
        remaining_skipped -= skipped
        shard_counts = {
            "candidate_unsat": 0,
            "error": 0,
            "skipped": skipped,
            "unknown": 0,
            "verified_all": record_count - skipped,
        }
        shard_bytes = record_count * 128
        records_bytes += shard_bytes
        records_sha256 = hashlib.sha256(f"records-{index}".encode()).hexdigest()
        manifest_sha256 = hashlib.sha256(f"manifest-{index}".encode()).hexdigest()
        completion_sha256 = hashlib.sha256(f"complete-{index}".encode()).hexdigest()
        run_fingerprint = hashlib.sha256(f"run-{index}".encode()).hexdigest()
        if materialize_artifacts:
            directory = scratch / "runs" / f"shard-{index:03d}-of-064"
            directory.mkdir(parents=True)
            records = directory / "records.jsonl"
            records.write_bytes(bytes([index]) * shard_bytes)
            records_sha256 = sha256_file(records)
            provenance = {
                "config": {
                    "checkpoint_interval": 8,
                    "checks": [
                        {"backend_id": "dsatur-iterative-v1", "palette_offset": 1},
                        {"backend_id": "dsatur-iterative-v1", "palette_offset": 2},
                        {"backend_id": "static-order-iterative-v1", "palette_offset": 1},
                    ],
                    "filters": {"require_high_degree": True},
                    "fix_distinguished_colors": True,
                    "generator_spec": {
                        "connected": False,
                        "max_degree": None,
                        "min_degree": None,
                        "order": 8,
                        "shard_count": 64,
                        "shard_index": index,
                    },
                    "partition_enumerator": "complement-matchings-lexicographic-v1",
                    "search_limits": {
                        "max_nodes_per_check": None,
                        "timeout_seconds_per_check": None,
                    },
                },
                "generator": {
                    "arguments": ["-q", "-X2", "8", f"{index}/64"],
                    "executable": "geng",
                    "name": "nauty-geng",
                    "sha256": geng_sha256,
                },
                "objective": "universal_auxiliary_extension",
                "shard": {"count": 64, "index": index},
                "toolkit": toolkit,
            }
            run_fingerprint = hashlib.sha256(canonical_json_bytes(provenance)[:-1]).hexdigest()
            manifest = directory / "manifest.json"
            atomic_json(
                manifest,
                {
                    "artifacts": {
                        "records_bytes": shard_bytes,
                        "records_path": "records.jsonl",
                        "records_sha256": records_sha256,
                    },
                    "complete": True,
                    "counts": shard_counts,
                    "partition_count": partition_count,
                    "provenance": provenance,
                    "record_count": record_count,
                    "run_fingerprint": run_fingerprint,
                    "schema_version": "total-coloring.universal-census-manifest.v1",
                },
            )
            manifest_sha256 = sha256_file(manifest)
            completion = directory / "completion.json"
            atomic_json(
                completion,
                {
                    "manifest_sha256": manifest_sha256,
                    "record_count": record_count,
                    "records_sha256": records_sha256,
                    "run_fingerprint": run_fingerprint,
                    "schema_version": "total-coloring.universal-census-completion.v1",
                },
            )
            completion_sha256 = sha256_file(completion)
        shard_receipt = {
            "check_evaluations": 3 * partition_count,
            "completion_sha256": completion_sha256,
            "counts": shard_counts,
            "manifest_sha256": manifest_sha256,
            "partition_count": partition_count,
            "record_count": record_count,
            "records_bytes": shard_bytes,
            "records_sha256": records_sha256,
            "run_fingerprint": run_fingerprint,
            "shard_index": index,
        }
        shards.append(shard_receipt)
        if materialize_artifacts:
            atomic_json(
                status / f"validation-complete-{index:03d}.json",
                {
                    "campaign_contract_sha256": campaign_contract_sha256,
                    "check_evaluations": 3 * partition_count,
                    "code_commit": code_commit,
                    "completion_sha256": completion_sha256,
                    "counts": shard_counts,
                    "geng_sha256": geng_sha256,
                    "launcher_archive_sha256": launcher_archive_sha256,
                    "launcher_sha256": launcher_digest,
                    "manifest_sha256": manifest_sha256,
                    "order": 8,
                    "partition_count": partition_count,
                    "record_count": record_count,
                    "records_bytes": shard_bytes,
                    "records_sha256": records_sha256,
                    "run_fingerprint": run_fingerprint,
                    "runtime_receipt_sha256": runtime_receipt_sha256,
                    "schema_version": VALIDATION_SCHEMA,
                    "shard_count": 64,
                    "shard_index": index,
                    "split_depth": 2,
                    "status": "validation_complete",
                    "toolkit": toolkit,
                    "wheel_sha256": wheel_sha256,
                },
            )
        reduced.append(
            {
                "manifest_sha256": manifest_sha256,
                "record_count": record_count,
                "run_fingerprint": run_fingerprint,
                "shard_index": index,
            }
        )
    counts = {
        "candidate_unsat": 0,
        "error": 0,
        "skipped": 424,
        "unknown": 0,
        "verified_all": 11_922,
    }
    reduced_totals = {
        "check_evaluations": 1_542_150,
        "partition_count": 514_050,
        "record_count": 12_346,
        "records_bytes": records_bytes,
    }
    reduce_receipt = status / "reduce-complete.json"
    atomic_json(
        reduce_receipt,
        {
            "campaign_contract_sha256": campaign_contract_sha256,
            "code_commit": code_commit,
            "counts": counts,
            "geng_sha256": geng_sha256,
            "launcher_archive_sha256": launcher_archive_sha256,
            "launcher_sha256": launcher_digest,
            "order": 8,
            "receipts": reduced,
            "runtime_receipt_sha256": runtime_receipt_sha256,
            "schema_version": REDUCE_SCHEMA,
            "shard_count": 64,
            "status": "reduce_complete",
            "toolkit": toolkit,
            "totals": reduced_totals,
            "wheel_sha256": wheel_sha256,
        },
    )
    receipt = status / "exact-union-complete.json"
    atomic_json(
        receipt,
        {
            "campaign_contract_sha256": campaign_contract_sha256,
            "checks": [
                {"backend_id": "dsatur-iterative-v1", "palette_offset": 1},
                {"backend_id": "dsatur-iterative-v1", "palette_offset": 2},
                {"backend_id": "static-order-iterative-v1", "palette_offset": 1},
            ],
            "code_commit": code_commit,
            "generator": {"executable": "geng", "sha256": geng_sha256},
            "geng_sha256": geng_sha256,
            "job_id": "200",
            "launcher_archive_sha256": launcher_archive_sha256,
            "launcher_sha256": launcher_digest,
            "order": 8,
            "reduce_receipt_sha256": sha256_file(reduce_receipt),
            "runtime_receipt_sha256": runtime_receipt_sha256,
            "schema_version": EXACT_UNION_SCHEMA,
            "shard_count": 64,
            "shards": shards,
            "split_depth": 2,
            "status": "exact_union_complete",
            "toolkit": toolkit,
            "totals": {**reduced_totals, "counts": counts},
            "wheel_sha256": wheel_sha256,
        },
    )
    return receipt, runtime, geng_sha256


def test_atomic_campaign_json_is_canonical_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "status" / "receipt.json"
    atomic_json(path, {"z": 1, "a": [2, 3]})

    assert path.read_bytes() == b'{"a":[2,3],"z":1}\n'
    assert load_json(path) == {"a": [2, 3], "z": 1}

    path.write_bytes(b"{" + b" " * MAX_METADATA_BYTES + b"}")
    with pytest.raises(CampaignError, match="exceeds"):
        load_json(path)


def test_launcher_identity_is_stable_and_export_rejects_commas() -> None:
    root = Path(__file__).resolve().parents[2]

    assert launcher_sha256(root) == launcher_sha256(root)
    assert root / "scripts" / "__init__.py" in launcher_files(root)
    assert launcher_archive_bytes(root) == launcher_archive_bytes(root)
    with pytest.raises(CampaignError, match="unsafe"):
        _safe_export({"TC_PATH": "bad,value"})


def test_launcher_archive_imports_in_isolated_no_bytecode_mode(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "launcher.zip"
    archive.write_bytes(launcher_archive_bytes(root))
    code = (
        "import sys;"
        f"sys.path.insert(0,{str(archive)!r});"
        "from scripts.easley.common import launcher_sha256;"
        "print(launcher_sha256)"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "function launcher_sha256" in completed.stdout


def test_bootstrap_child_python_ignores_pythonpath_sitecustomize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "sitecustomize-executed"
    (tmp_path / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['BOOTSTRAP_CHILD_MARKER']).write_text('executed')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("PYTHONSTARTUP", str(tmp_path / "sitecustomize.py"))
    monkeypatch.setenv("BOOTSTRAP_CHILD_MARKER", str(marker))

    completed = bootstrap_run([sys.executable, "-c", "print('isolated child')"])

    assert completed.stdout.strip() == "isolated child"
    assert not marker.exists()


def test_immutable_code_checkout_binds_head_cleanliness_and_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    directory = source / "scripts" / "easley"
    directory.mkdir(parents=True)
    root_init = source / "scripts" / "__init__.py"
    root_init.write_text("", encoding="utf-8")
    module = directory / "worker.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "scripts"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for path in (root_init, module):
        path.chmod(0o444)
    for path in (directory, source / "scripts", source):
        path.chmod(0o555)
    digest = launcher_sha256(source)
    real_run = subprocess.run

    def legacy_git_compatible_run(
        command: list[str],
        *,
        cwd: Path | None = None,
        text: bool = False,
        capture_output: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert "-C" not in command
        assert command in (
            ["git", "rev-parse", "--verify", "HEAD"],
            ["git", "status", "--porcelain", "--untracked-files=all"],
        )
        assert cwd == source
        assert text
        return real_run(
            command,
            cwd=cwd,
            text=True,
            capture_output=capture_output,
            check=check,
        )

    monkeypatch.setattr("scripts.easley.submit.subprocess.run", legacy_git_compatible_run)

    _require_immutable_code_checkout(source, commit, digest)

    module.chmod(0o644)
    with pytest.raises(CampaignError, match="read-only"):
        _require_immutable_code_checkout(source, commit, digest)
    module.write_text("VALUE = 2\n", encoding="utf-8")
    module.chmod(0o444)
    with pytest.raises(CampaignError, match="clean"):
        _require_immutable_code_checkout(source, commit, digest)


def test_submitter_dry_run_is_exact_and_creates_no_campaign_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[2]
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"wheel candidate")
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    nauty = tmp_path / "nauty.tar.gz"
    nauty.write_bytes(b"source candidate")
    scratch = tmp_path / "campaign-scratch"

    exit_code = main(
        [
            "--profile",
            "order8-smoke",
            "--code-root",
            str(root),
            "--code-commit",
            "a" * 40,
            "--scratch",
            str(scratch),
            "--runtime",
            str(tmp_path / "runtime"),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            wheel_sha256,
            "--toolkit-version",
            "0.2.0",
            "--nauty-tar",
            str(nauty),
            "--bootstrap-only",
        ]
    )

    assert exit_code == 0
    assert not scratch.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["environment"]["TC_EXPECTED_RECORDS"] == "12346"
    assert payload["environment"]["TC_CODE_COMMIT"] == "a" * 40
    assert payload["environment"]["TC_CAMPAIGN_MODE"] == "bootstrap_only"
    assert payload["environment"]["TC_LAUNCHER_SHA256"] == launcher_sha256(root)
    assert (
        payload["environment"]["TC_LAUNCHER_ARCHIVE_SHA256"]
        == hashlib.sha256(launcher_archive_bytes(root)).hexdigest()
    )
    bootstrap = payload["jobs"][0]["command"]
    assert "set -eu; . /etc/profile.d/modules.sh; module purge" in bootstrap[-1]
    assert " -I -B -S -c " in bootstrap[-1]
    assert "runpy.run_module" in bootstrap[-1]
    assert [job["name"] for job in payload["jobs"]] == ["bootstrap"]


def test_submitter_rejects_noncanonical_commit(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"x")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    with pytest.raises(CampaignError, match="code commit"):
        main(
            [
                "--profile",
                "order9-production",
                "--code-root",
                str(root),
                "--code-commit",
                "not-a-commit",
                "--scratch",
                str(tmp_path / "scratch"),
                "--runtime",
                str(tmp_path / "runtime"),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
            ]
        )


def test_order8_science_requires_a_separately_pinned_runtime(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"candidate")
    digest = sha256_file(artifact)
    common = [
        "--profile",
        "order8-smoke",
        "--code-root",
        str(root),
        "--code-commit",
        "2" * 40,
        "--scratch",
        str(tmp_path / "scratch"),
        "--runtime",
        str(tmp_path / "runtime"),
        "--wheel",
        str(artifact),
        "--wheel-sha256",
        digest,
        "--toolkit-version",
        "0.2.0",
        "--nauty-tar",
        str(artifact),
    ]

    with pytest.raises(CampaignError, match="requires --geng-sha256"):
        main(common)
    with pytest.raises(CampaignError, match="requires --runtime-receipt-sha256"):
        main([*common, "--geng-sha256", "3" * 64])


def test_order9_dry_run_requires_matching_order8_exact_union_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"release")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    code_commit = "b" * 40
    receipt, runtime, geng_sha256 = _build_order8_chain(
        tmp_path / "sealed-order8",
        root,
        code_commit=code_commit,
        wheel_sha256=digest,
    )

    assert (
        main(
            [
                "--profile",
                "order9-production",
                "--code-root",
                str(root),
                "--code-commit",
                code_commit,
                "--scratch",
                str(tmp_path / "order9"),
                "--runtime",
                str(runtime),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--order8-receipt",
                str(receipt),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["environment"]["TC_GENG_SHA256"] == geng_sha256
    assert payload["environment"]["TC_ORDER8_RECEIPT_SHA256"] == sha256_file(receipt)
    assert payload["environment"]["TC_ORDER8_RECEIPT_PATH"] == str(receipt)
    assert payload["environment"]["TC_RUNTIME_RECEIPT_SHA256"] == sha256_file(
        runtime / "runtime-receipt.json"
    )
    assert payload["environment"]["TC_SHARDS"] == "2048"
    assert payload["environment"]["TC_SPLIT_DEPTH"] == "2"
    assert payload["environment"]["TC_ARRAY_CONCURRENCY"] == "2048"
    assert payload["jobs"][0]["name"] == "order8-prerequisite"
    assert payload["jobs"][1]["name"] == "bootstrap"
    assert payload["jobs"][1]["dependency"] == "afterok:<order8-prerequisite>"

    minimal = tmp_path / "minimal-order8.json"
    atomic_json(
        minimal,
        {
            "code_commit": code_commit,
            "geng_sha256": geng_sha256,
            "order": 8,
            "status": "exact_union_complete",
            "wheel_sha256": digest,
        },
    )
    with pytest.raises(CampaignError, match="unexpected field set"):
        main(
            [
                "--profile",
                "order9-production",
                "--code-root",
                str(root),
                "--code-commit",
                code_commit,
                "--scratch",
                str(tmp_path / "minimal-rejected"),
                "--runtime",
                str(runtime),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--order8-receipt",
                str(minimal),
            ]
        )

    reduce_receipt = receipt.parent / "reduce-complete.json"
    pristine_reduce = reduce_receipt.read_bytes()
    invalid_reduce = json.loads(pristine_reduce)
    invalid_reduce["status"] = "hand_authored"
    atomic_json(reduce_receipt, invalid_reduce)
    with pytest.raises(CampaignError, match="reduce receipt"):
        main(
            [
                "--profile",
                "order9-production",
                "--code-root",
                str(root),
                "--code-commit",
                code_commit,
                "--scratch",
                str(tmp_path / "bad-reduce"),
                "--runtime",
                str(runtime),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--order8-receipt",
                str(receipt),
            ]
        )
    reduce_receipt.write_bytes(pristine_reduce)

    invalid_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    invalid_receipt["geng_sha256"] = "g" * 64
    atomic_json(receipt, invalid_receipt)
    with pytest.raises(CampaignError, match="order-eight geng SHA-256"):
        main(
            [
                "--profile",
                "order9-production",
                "--code-root",
                str(root),
                "--code-commit",
                code_commit,
                "--scratch",
                str(tmp_path / "invalid-hash"),
                "--runtime",
                str(runtime),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--order8-receipt",
                str(receipt),
            ]
        )

    receipt.unlink()
    with pytest.raises(CampaignError, match="order8-receipt"):
        main(
            [
                "--profile",
                "order9-production",
                "--code-root",
                str(root),
                "--code-commit",
                code_commit,
                "--scratch",
                str(tmp_path / "other"),
                "--runtime",
                str(tmp_path / "runtime"),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
            ]
        )


def test_order8_actual_artifacts_form_a_strict_portable_gate(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    code_commit = "3" * 40
    wheel_sha256 = "4" * 64
    receipt, runtime, _ = _build_order8_chain(
        tmp_path / "sealed-order8",
        root,
        code_commit=code_commit,
        wheel_sha256=wheel_sha256,
        materialize_artifacts=True,
    )
    launcher_digest = launcher_sha256(root)
    launcher_archive_sha256 = hashlib.sha256(launcher_archive_bytes(root)).hexdigest()
    prerequisite = validate_order8_prerequisite(
        receipt,
        runtime=runtime,
        code_commit=code_commit,
        launcher_archive_sha256=launcher_archive_sha256,
        launcher_digest=launcher_digest,
        toolkit_version="0.2.0",
        wheel_sha256=wheel_sha256,
        verify_artifacts=True,
    )

    assert prerequisite.artifact_inventory is not None
    assert len(prerequisite.artifact_inventory.entries) == 256
    assert prerequisite.artifact_inventory.entries[0].path.startswith("runs/")
    assert len(prerequisite.order8_artifact_root_sha256 or "") == 64

    status = tmp_path / "order9" / "status"
    status.mkdir(parents=True)
    gate, gate_sha256 = write_order8_gate(
        status / ORDER8_GATE_FILENAME,
        prerequisite,
        job_id="900",
        campaign_contract_sha256="5" * 64,
        order8_replay_sha256="6" * 64,
    )
    validated, validated_sha256 = validate_order8_gate(
        status,
        runtime_receipt=load_json(runtime / "runtime-receipt.json"),
        expected_receipt_sha256=sha256_file(receipt),
        expected_campaign_contract_sha256="5" * 64,
    )

    assert validated == gate
    assert validated.order8_replay_sha256 == "6" * 64
    assert validated_sha256 == gate_sha256 == sha256_file(status / ORDER8_GATE_FILENAME)

    malformed = receipt.parent / "validation-complete-017.json"
    payload = dict(load_json(malformed))
    payload["record_count"] = True
    atomic_json(malformed, payload)
    with pytest.raises(CampaignError, match="nonnegative integer"):
        validate_order8_prerequisite(
            receipt,
            runtime=runtime,
            code_commit=code_commit,
            launcher_archive_sha256=launcher_archive_sha256,
            launcher_digest=launcher_digest,
            toolkit_version="0.2.0",
            wheel_sha256=wheel_sha256,
            verify_artifacts=True,
        )


def test_prerequisite_task_rejects_a_semantic_replay_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    code_commit = "7" * 40
    wheel_sha256 = "8" * 64
    receipt, runtime, _ = _build_order8_chain(
        tmp_path / "sealed-order8",
        root,
        code_commit=code_commit,
        wheel_sha256=wheel_sha256,
        materialize_artifacts=True,
    )
    launcher_digest = launcher_sha256(root)
    launcher_archive_sha256 = hashlib.sha256(launcher_archive_bytes(root)).hexdigest()
    prerequisite = validate_order8_prerequisite(
        receipt,
        runtime=runtime,
        code_commit=code_commit,
        launcher_archive_sha256=launcher_archive_sha256,
        launcher_digest=launcher_digest,
        toolkit_version="0.2.0",
        wheel_sha256=wheel_sha256,
        verify_artifacts=True,
    )
    runtime_receipt = load_json(runtime / "runtime-receipt.json")
    monkeypatch.setattr(
        "scripts.easley.prerequisite_task.runtime_paths",
        lambda: (runtime / "venv" / "bin" / "python", runtime / "bin" / "geng", runtime_receipt),
    )
    monkeypatch.setattr(
        "scripts.easley.prerequisite_task.validate_order8_prerequisite",
        lambda _path, **_kwargs: prerequisite,
    )
    monkeypatch.setattr(
        "scripts.easley.prerequisite_task.validate_completed_universal_shard_set",
        lambda *_args, **_kwargs: SimpleNamespace(order=7),
    )
    environment = {
        "SLURM_JOB_ID": "901",
        "TC_CAMPAIGN_CONTRACT_SHA256": "9" * 64,
        "TC_CODE_COMMIT": code_commit,
        "TC_LAUNCHER_ARCHIVE_SHA256": launcher_archive_sha256,
        "TC_LAUNCHER_SHA256": launcher_digest,
        "TC_MAX_UNION_GRAPHS": "1000000",
        "TC_ORDER": "9",
        "TC_ORDER8_RECEIPT_PATH": str(receipt),
        "TC_ORDER8_RECEIPT_SHA256": sha256_file(receipt),
        "TC_RUNTIME": str(runtime),
        "TC_SCRATCH": str(tmp_path / "order9"),
        "TC_SHARDS": "2048",
        "TC_TOOLKIT_VERSION": "0.2.0",
        "TC_WHEEL_SHA256": wheel_sha256,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(CampaignError, match="semantic replay"):
        prerequisite_main()
    assert not (tmp_path / "order9" / "status" / ORDER8_GATE_FILENAME).exists()


def test_order9_exact_union_fails_before_replay_without_prerequisite_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scripts.easley.exact_union.runtime_paths",
        lambda: (tmp_path / "python", tmp_path / "geng", {}),
    )
    environment = {
        "SLURM_JOB_ID": "902",
        "TC_CAMPAIGN_CONTRACT_SHA256": "a" * 64,
        "TC_EXPECTED_CHECKS": "79803890",
        "TC_EXPECTED_PARTITIONS": "26634630",
        "TC_EXPECTED_RECORDS": "274668",
        "TC_EXPECTED_SKIPPED": "15471",
        "TC_EXPECTED_VERIFIED": "259197",
        "TC_ORDER": "9",
        "TC_SCRATCH": str(tmp_path / "order9"),
        "TC_SHARDS": "2048",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TC_ORDER8_RECEIPT_PATH", raising=False)
    monkeypatch.delenv("TC_ORDER8_RECEIPT_SHA256", raising=False)

    with pytest.raises(CampaignError, match="TC_ORDER8_RECEIPT_PATH"):
        exact_union_main()


def test_exact_union_rejects_atomic_reduce_receipt_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    code_commit = "5" * 40
    wheel_sha256 = "6" * 64
    _, runtime, geng_sha256 = _build_order8_chain(
        tmp_path / "runtime-fixture",
        root,
        code_commit=code_commit,
        wheel_sha256=wheel_sha256,
    )
    runtime_receipt = load_json(runtime / "runtime-receipt.json")
    scratch = tmp_path / "current-order8"
    status = scratch / "status"
    status.mkdir(parents=True)
    counts_payload = {
        "candidate_unsat": 0,
        "error": 0,
        "skipped": 1,
        "unknown": 0,
        "verified_all": 1,
    }
    counts = SimpleNamespace(
        candidate_unsat=0,
        error=0,
        skipped=1,
        unknown=0,
        verified_all=1,
        to_dict=lambda: counts_payload,
    )
    result = SimpleNamespace(
        order=8,
        record_count=2,
        partition_count=2,
        check_evaluations=6,
        records_bytes=17,
        counts=counts,
        to_dict=lambda: {"order": 8, "totals": {"record_count": 2}},
    )
    campaign_sha256 = "7" * 64
    runtime_digest = sha256_file(runtime / "runtime-receipt.json")
    reduce_receipt = {
        "campaign_contract_sha256": campaign_sha256,
        "code_commit": code_commit,
        "counts": counts_payload,
        "geng_sha256": geng_sha256,
        "launcher_archive_sha256": runtime_receipt["launcher_archive_sha256"],
        "launcher_sha256": runtime_receipt["launcher_sha256"],
        "order": 8,
        "runtime_receipt_sha256": runtime_digest,
        "schema_version": REDUCE_SCHEMA,
        "shard_count": 64,
        "status": "reduce_complete",
        "totals": {
            "check_evaluations": 6,
            "partition_count": 2,
            "record_count": 2,
            "records_bytes": 17,
        },
        "wheel_sha256": wheel_sha256,
    }
    reduce_path = status / "reduce-complete.json"
    atomic_json(reduce_path, reduce_receipt)

    def replay_and_replace(*_args: object, **_kwargs: object) -> SimpleNamespace:
        atomic_json(reduce_path, reduce_receipt)
        return result

    monkeypatch.setattr(
        "scripts.easley.exact_union.runtime_paths",
        lambda: (runtime / "venv" / "bin" / "python", runtime / "bin" / "geng", runtime_receipt),
    )
    monkeypatch.setattr(
        "scripts.easley.exact_union.validate_completed_universal_shard_set",
        replay_and_replace,
    )
    monkeypatch.setattr(
        "scripts.easley.exact_union.recheck_universal_shard_artifact_inventory",
        lambda validation, _directories: validation,
    )
    environment = {
        "SLURM_JOB_ID": "905",
        "TC_CAMPAIGN_CONTRACT_SHA256": campaign_sha256,
        "TC_EXPECTED_CHECKS": "6",
        "TC_EXPECTED_PARTITIONS": "2",
        "TC_EXPECTED_RECORDS": "2",
        "TC_EXPECTED_SKIPPED": "1",
        "TC_EXPECTED_VERIFIED": "1",
        "TC_MAX_UNION_GRAPHS": "1000000",
        "TC_ORDER": "8",
        "TC_RUNTIME": str(runtime),
        "TC_SCRATCH": str(scratch),
        "TC_SHARDS": "64",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(CampaignError, match="reduce receipt changed"):
        exact_union_main()
    assert not (status / "exact-union-complete.json").exists()


def test_forged_order8_gate_cannot_bypass_actual_artifact_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    code_commit = "c" * 40
    wheel_sha256 = "d" * 64
    receipt, runtime, geng_sha256 = _build_order8_chain(
        tmp_path / "sealed-order8",
        root,
        code_commit=code_commit,
        wheel_sha256=wheel_sha256,
    )
    runtime_receipt = load_json(runtime / "runtime-receipt.json")
    status = tmp_path / "order9" / "status"
    status.mkdir(parents=True)
    campaign_sha256 = "e" * 64
    atomic_json(
        status / ORDER8_GATE_FILENAME,
        {
            "campaign_contract_sha256": campaign_sha256,
            "code_commit": code_commit,
            "geng_sha256": geng_sha256,
            "job_id": "903",
            "launcher_archive_sha256": runtime_receipt["launcher_archive_sha256"],
            "launcher_sha256": runtime_receipt["launcher_sha256"],
            "order8_artifact_root_sha256": "1" * 64,
            "order8_receipt_sha256": sha256_file(receipt),
            "order8_replay_sha256": "2" * 64,
            "runtime_receipt_sha256": sha256_file(runtime / "runtime-receipt.json"),
            "schema_version": ORDER8_GATE_SCHEMA,
            "status": "order8_prerequisite_complete",
            "wheel_sha256": wheel_sha256,
        },
    )
    monkeypatch.setattr(
        "scripts.easley.exact_union.runtime_paths",
        lambda: (runtime / "venv" / "bin" / "python", runtime / "bin" / "geng", runtime_receipt),
    )
    environment = {
        "SLURM_JOB_ID": "904",
        "TC_CAMPAIGN_CONTRACT_SHA256": campaign_sha256,
        "TC_CODE_COMMIT": code_commit,
        "TC_EXPECTED_CHECKS": "79803890",
        "TC_EXPECTED_PARTITIONS": "26634630",
        "TC_EXPECTED_RECORDS": "274668",
        "TC_EXPECTED_SKIPPED": "15471",
        "TC_EXPECTED_VERIFIED": "259197",
        "TC_LAUNCHER_ARCHIVE_SHA256": str(runtime_receipt["launcher_archive_sha256"]),
        "TC_LAUNCHER_SHA256": str(runtime_receipt["launcher_sha256"]),
        "TC_MAX_UNION_GRAPHS": "1000000",
        "TC_ORDER": "9",
        "TC_ORDER8_RECEIPT_PATH": str(receipt),
        "TC_ORDER8_RECEIPT_SHA256": sha256_file(receipt),
        "TC_RUNTIME": str(runtime),
        "TC_SCRATCH": str(tmp_path / "order9"),
        "TC_SHARDS": "2048",
        "TC_TOOLKIT_VERSION": "0.2.0",
        "TC_WHEEL_SHA256": wheel_sha256,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(CampaignError, match="runs directory"):
        exact_union_main()


def test_partial_submission_is_journaled_and_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"candidate")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    scratch = tmp_path / "scratch"
    _, runtime, geng_sha256 = _build_order8_chain(
        tmp_path / "bootstrap",
        root,
        code_commit="d" * 40,
        wheel_sha256=digest,
    )
    sbatch_calls = 0
    cancelled: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal sbatch_calls
        del kwargs
        if command[0] == "sbatch":
            sbatch_calls += 1
            if sbatch_calls == 1:
                return subprocess.CompletedProcess(command, 0, stdout="12345\n", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="synthetic failure")
        assert command[0] == "scancel"
        cancelled.append(command[1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("scripts.easley.submit.subprocess.run", fake_run)
    monkeypatch.setattr(
        "scripts.easley.submit._require_immutable_code_checkout",
        lambda _root, _commit, _launcher: None,
    )
    with pytest.raises(CampaignError, match="partial submission was cancelled"):
        main(
            [
                "--profile",
                "order8-smoke",
                "--code-root",
                str(root),
                "--code-commit",
                "d" * 40,
                "--scratch",
                str(scratch),
                "--runtime",
                str(runtime),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--geng-sha256",
                geng_sha256,
                "--runtime-receipt-sha256",
                sha256_file(runtime / "runtime-receipt.json"),
                "--submit",
            ]
        )

    journal = load_json(scratch / "status" / "submission.json")
    assert journal["status"] == "submission_failed"
    assert journal["jobs"] == {"bootstrap": "12345"}
    assert cancelled == ["12345"]
    contract = load_json(scratch / "sealed" / "campaign-contract.json")
    assert contract["profile"] == "order8-smoke"
    assert contract["environment"]["TC_PROFILE"] == "order8-smoke"
    assert journal["environment"]["TC_CAMPAIGN_CONTRACT_SHA256"] == sha256_file(
        scratch / "sealed" / "campaign-contract.json"
    )


def test_order9_submission_uses_full_high_throughput_arrays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"candidate")
    digest = sha256_file(artifact)
    code_commit = "8" * 40
    receipt, runtime, _ = _build_order8_chain(
        tmp_path / "prerequisite",
        root,
        code_commit=code_commit,
        wheel_sha256=digest,
    )
    submitted: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert command[0] == "sbatch"
        submitted.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{9000 + len(submitted)}\n",
            stderr="",
        )

    monkeypatch.setattr("scripts.easley.submit.subprocess.run", fake_run)
    monkeypatch.setattr(
        "scripts.easley.submit._require_immutable_code_checkout",
        lambda _root, _commit, _launcher: None,
    )

    assert (
        main(
            [
                "--profile",
                "order9-production",
                "--code-root",
                str(root),
                "--code-commit",
                code_commit,
                "--scratch",
                str(tmp_path / "order9"),
                "--runtime",
                str(runtime),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--order8-receipt",
                str(receipt),
                "--submit",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs"] == {
        "bootstrap": "9002",
        "census_array": "9003",
        "exact_union": "9006",
        "order8_prerequisite": "9001",
        "reduce": "9005",
        "validation_array": "9004",
    }
    assert len(submitted) == 6
    census, validation = submitted[2], submitted[3]
    assert "--partition=nova_short" in census
    assert "--partition=nova_short" in validation
    assert "--array=0-2047%2048" in census
    assert "--array=0-2047%2048" in validation
    assert "--partition=nova_long" in submitted[5]


def test_submitter_atomically_rejects_an_existing_scratch_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"candidate")
    digest = sha256_file(artifact)
    scratch = tmp_path / "reserved"
    scratch.mkdir()
    monkeypatch.setattr(
        "scripts.easley.submit._require_immutable_code_checkout",
        lambda _root, _commit, _launcher: None,
    )

    with pytest.raises(CampaignError, match="nonexistent scratch"):
        main(
            [
                "--profile",
                "order8-smoke",
                "--code-root",
                str(root),
                "--code-commit",
                "f" * 40,
                "--scratch",
                str(scratch),
                "--runtime",
                str(tmp_path / "runtime"),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--bootstrap-only",
                "--submit",
            ]
        )


def test_controlled_submission_interruption_cancels_recorded_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"candidate")
    digest = sha256_file(artifact)
    scratch = tmp_path / "interrupted"
    _, runtime, geng_sha256 = _build_order8_chain(
        tmp_path / "bootstrap",
        root,
        code_commit="1" * 40,
        wheel_sha256=digest,
    )
    sbatch_calls = 0
    cancelled: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal sbatch_calls
        del kwargs
        if command[0] == "sbatch":
            sbatch_calls += 1
            if sbatch_calls == 1:
                return subprocess.CompletedProcess(command, 0, stdout="777\n", stderr="")
            raise SubmissionInterrupted("synthetic SIGTERM")
        assert command[0] == "scancel"
        cancelled.append(command[1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("scripts.easley.submit.subprocess.run", fake_run)
    monkeypatch.setattr(
        "scripts.easley.submit._require_immutable_code_checkout",
        lambda _root, _commit, _launcher: None,
    )
    with pytest.raises(CampaignError, match="partial submission was cancelled"):
        main(
            [
                "--profile",
                "order8-smoke",
                "--code-root",
                str(root),
                "--code-commit",
                "1" * 40,
                "--scratch",
                str(scratch),
                "--runtime",
                str(runtime),
                "--wheel",
                str(artifact),
                "--wheel-sha256",
                digest,
                "--toolkit-version",
                "0.2.0",
                "--nauty-tar",
                str(artifact),
                "--geng-sha256",
                geng_sha256,
                "--runtime-receipt-sha256",
                sha256_file(runtime / "runtime-receipt.json"),
                "--submit",
            ]
        )

    journal = load_json(scratch / "status" / "submission.json")
    assert journal["status"] == "submission_failed"
    assert journal["jobs"] == {"bootstrap": "777"}
    assert cancelled == ["777"]
