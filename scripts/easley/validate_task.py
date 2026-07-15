#!/usr/bin/env python3
"""Independently replay one completed shard inside a Slurm array."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts.easley.common import (
    CampaignError,
    atomic_json,
    easley_shard_count_env,
    load_json,
    nonnegative_env,
    positive_env,
    require_env,
    runtime_paths,
    runtime_receipt_sha256,
    sha256_file,
    shard_directory,
)
from scripts.easley.prerequisite import VALIDATION_SCHEMA
from total_coloring.census import CensusError
from total_coloring.universal_census import validate_completed_universal_census


def main() -> int:
    if "SLURM_JOB_ID" not in os.environ:
        raise CampaignError("validation task must run under Slurm")
    index = nonnegative_env("SLURM_ARRAY_TASK_ID")
    order = positive_env("TC_ORDER")
    shard_count = easley_shard_count_env()
    split_depth = nonnegative_env("TC_SPLIT_DEPTH")
    if index >= shard_count:
        raise CampaignError("array task index lies outside the configured shard set")
    _, geng, runtime_receipt = runtime_paths()
    directory = shard_directory(index, shard_count)
    validation = validate_completed_universal_census(directory, executable=str(geng))
    spec = validation.config.geng
    if (
        spec.order != order
        or spec.shard_index != index
        or spec.shard_count != shard_count
        or spec.split_depth != split_depth
    ):
        raise CampaignError("validated shard provenance does not match the Slurm contract")
    manifest = load_json(validation.result.manifest_path)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CampaignError("validated manifest has no artifact receipt")
    receipt = {
        "check_evaluations": validation.result.partition_count * len(validation.config.checks),
        "campaign_contract_sha256": require_env("TC_CAMPAIGN_CONTRACT_SHA256"),
        "code_commit": runtime_receipt["code_commit"],
        "completion_sha256": sha256_file(validation.result.completion_path),
        "counts": validation.result.counts.to_dict(),
        "geng_sha256": validation.generator.sha256,
        "launcher_archive_sha256": runtime_receipt["launcher_archive_sha256"],
        "launcher_sha256": runtime_receipt["launcher_sha256"],
        "manifest_sha256": sha256_file(validation.result.manifest_path),
        "order": order,
        "partition_count": validation.result.partition_count,
        "record_count": validation.result.record_count,
        "records_bytes": artifacts.get("records_bytes"),
        "records_sha256": artifacts.get("records_sha256"),
        "run_fingerprint": validation.result.run_fingerprint,
        "runtime_receipt_sha256": runtime_receipt_sha256(),
        "schema_version": VALIDATION_SCHEMA,
        "shard_count": shard_count,
        "shard_index": index,
        "split_depth": split_depth,
        "status": "validation_complete",
        "toolkit": validation.toolkit.to_dict(),
        "wheel_sha256": runtime_receipt["wheel_sha256"],
    }
    status = Path(require_env("TC_SCRATCH")).resolve() / "status"
    atomic_json(status / f"validation-complete-{index:03d}.json", receipt)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CampaignError, CensusError, OSError, ValueError) as exc:
        print(f"validation task error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
