#!/usr/bin/env python3
"""Run or resume one universal-census shard inside a Slurm array."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from scripts.easley.common import (
    CampaignError,
    atomic_json,
    easley_shard_count_env,
    nonnegative_env,
    positive_env,
    require_env,
    runtime_paths,
    runtime_receipt_sha256,
    sha256_file,
    shard_directory,
    slurm_command,
)
from total_coloring.census import CensusError
from total_coloring.geng import GengSpec
from total_coloring.universal_census import UniversalCensusConfig, run_universal_census


class RequeueRequested(RuntimeError):
    """Slurm requested a checkpoint-safe task requeue."""


def _request_requeue(_signum: int, _frame: object) -> None:
    raise RequeueRequested("Slurm USR1 checkpoint signal received")


def _requeue_target() -> str:
    array_job = os.environ.get("SLURM_ARRAY_JOB_ID")
    task = os.environ.get("SLURM_ARRAY_TASK_ID")
    if array_job and task:
        return f"{array_job}_{task}"
    return require_env("SLURM_JOB_ID")


def main() -> int:
    if "SLURM_JOB_ID" not in os.environ:
        raise CampaignError("census task must run under Slurm")
    index = nonnegative_env("SLURM_ARRAY_TASK_ID")
    order = positive_env("TC_ORDER")
    shard_count = easley_shard_count_env()
    split_depth = nonnegative_env("TC_SPLIT_DEPTH")
    checkpoint_interval = positive_env("TC_CHECKPOINT_INTERVAL")
    if index >= shard_count:
        raise CampaignError("array task index lies outside the configured shard set")
    _, geng, runtime_receipt = runtime_paths()
    output = shard_directory(index, shard_count)
    status = Path(require_env("TC_SCRATCH")).resolve() / "status"
    atomic_json(
        status / f"census-running-{index:03d}.json",
        {
            "job_id": os.environ["SLURM_JOB_ID"],
            "order": order,
            "shard_count": shard_count,
            "shard_index": index,
            "split_depth": split_depth,
            "status": "running",
        },
    )

    signal.signal(signal.SIGUSR1, _request_requeue)
    try:
        result = run_universal_census(
            UniversalCensusConfig(
                GengSpec(
                    order,
                    shard_index=index,
                    shard_count=shard_count,
                    split_depth=split_depth,
                ),
                checkpoint_interval=checkpoint_interval,
            ),
            output,
            executable=str(geng),
        )
    except RequeueRequested:
        completed = subprocess.run(
            [slurm_command("scontrol"), "requeue", _requeue_target()],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode != 0:
            raise CampaignError(f"Slurm requeue failed: {completed.stdout}") from None
        return 0
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)

    receipt = {
        "campaign_contract_sha256": require_env("TC_CAMPAIGN_CONTRACT_SHA256"),
        "code_commit": runtime_receipt["code_commit"],
        "completion_sha256": sha256_file(result.completion_path),
        "counts": result.counts.to_dict(),
        "geng_sha256": runtime_receipt["geng_sha256"],
        "launcher_archive_sha256": runtime_receipt["launcher_archive_sha256"],
        "launcher_sha256": runtime_receipt["launcher_sha256"],
        "manifest_sha256": sha256_file(result.manifest_path),
        "order": order,
        "partition_count": result.partition_count,
        "record_count": result.record_count,
        "records_bytes": result.records_path.stat().st_size,
        "records_sha256": sha256_file(result.records_path),
        "resumed_records": result.resumed_records,
        "run_fingerprint": result.run_fingerprint,
        "runtime_receipt_sha256": runtime_receipt_sha256(),
        "shard_count": shard_count,
        "shard_index": index,
        "split_depth": split_depth,
        "status": "census_complete",
        "toolkit_version": runtime_receipt["toolkit_version"],
        "wheel_sha256": runtime_receipt["wheel_sha256"],
    }
    atomic_json(status / f"census-complete-{index:03d}.json", receipt)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CampaignError, CensusError, OSError, ValueError) as exc:
        print(f"census task error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
