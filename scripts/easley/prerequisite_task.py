#!/usr/bin/env python3
"""Compute-node semantic replay and actual-artifact gate for order nine."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from scripts.easley.common import (
    CampaignError,
    canonical_json_bytes,
    positive_env,
    require_env,
    runtime_paths,
)
from scripts.easley.prerequisite import (
    EXPECTED_CHECKS,
    ORDER8_GATE_FILENAME,
    Order8Prerequisite,
    validate_order8_prerequisite,
    write_order8_gate,
)
from total_coloring.census import CensusError
from total_coloring.universal_shards import (
    UniversalShardSetValidation,
    validate_completed_universal_shard_set,
)


def require_golden_order8_replay(result: UniversalShardSetValidation, *, geng_sha256: str) -> None:
    """Fail closed unless the independent replay equals the golden order-eight census."""

    expected_counts = {
        "candidate_unsat": 0,
        "error": 0,
        "skipped": 424,
        "unknown": 0,
        "verified_all": 11_922,
    }
    if (
        result.order != 8
        or result.shard_count != 64
        or result.split_depth != 2
        or tuple(check.to_dict() for check in result.checks) != EXPECTED_CHECKS
        or result.generator_executable != "geng"
        or result.generator_sha256 != geng_sha256
        or result.record_count != 12_346
        or result.partition_count != 514_050
        or result.check_evaluations != 1_542_150
        or result.counts.to_dict() != expected_counts
        or len(result.receipts) != 64
        or tuple(receipt.shard_index for receipt in result.receipts) != tuple(range(64))
    ):
        raise CampaignError("order-eight semantic replay does not equal the golden census")


def replay_order8_prerequisite(
    receipt_path: Path,
    *,
    geng: Path,
    geng_sha256: str,
    max_union_graphs: int,
) -> str:
    """Replay all retained order-eight shards and return the canonical result digest."""

    order8_scratch = receipt_path.parent.parent
    replay = validate_completed_universal_shard_set(
        [order8_scratch / "runs" / f"shard-{index:03d}-of-064" for index in range(64)],
        executable=str(geng),
        max_union_graphs=max_union_graphs,
    )
    require_golden_order8_replay(replay, geng_sha256=geng_sha256)
    return hashlib.sha256(canonical_json_bytes(replay.to_dict())).hexdigest()


def main() -> int:
    """Replay order eight, re-hash every artifact, then atomically publish the gate."""

    job_id = os.environ.get("SLURM_JOB_ID")
    if job_id is None or not job_id.isdigit():
        raise CampaignError("prerequisite task must run under Slurm with a numeric job id")
    if positive_env("TC_ORDER") != 9:
        raise CampaignError("the order-eight prerequisite task is only valid for order nine")
    if positive_env("TC_SHARDS") != 64:
        raise CampaignError("the order-nine campaign must use exactly 64 shards")

    runtime = Path(require_env("TC_RUNTIME")).resolve(strict=True)
    _, geng, runtime_receipt = runtime_paths()
    receipt_path = Path(require_env("TC_ORDER8_RECEIPT_PATH"))
    expected_receipt_sha256 = require_env("TC_ORDER8_RECEIPT_SHA256")
    code_commit = require_env("TC_CODE_COMMIT")
    launcher_archive_sha256 = require_env("TC_LAUNCHER_ARCHIVE_SHA256")
    launcher_digest = require_env("TC_LAUNCHER_SHA256")
    toolkit_version = require_env("TC_TOOLKIT_VERSION")
    wheel_sha256 = require_env("TC_WHEEL_SHA256")

    def validate_actual_artifacts() -> Order8Prerequisite:
        return validate_order8_prerequisite(
            receipt_path,
            runtime=runtime,
            code_commit=code_commit,
            launcher_archive_sha256=launcher_archive_sha256,
            launcher_digest=launcher_digest,
            toolkit_version=toolkit_version,
            wheel_sha256=wheel_sha256,
            verify_artifacts=True,
        )

    prerequisite = validate_actual_artifacts()
    if prerequisite.receipt_sha256 != expected_receipt_sha256:
        raise CampaignError("order-eight exact-union receipt changed after submission")
    if (
        prerequisite.runtime_receipt_sha256
        != hashlib.sha256(canonical_json_bytes(runtime_receipt)).hexdigest()
    ):
        raise CampaignError("order-eight prerequisite and active runtime receipts disagree")

    replay_sha256 = replay_order8_prerequisite(
        receipt_path,
        geng=geng,
        geng_sha256=prerequisite.geng_sha256,
        max_union_graphs=positive_env("TC_MAX_UNION_GRAPHS"),
    )

    # The replay brackets run artifacts, while this second full pass also rechecks
    # the 64 validation receipts and the entire exact/reduce/runtime chain after it.
    stable_prerequisite = validate_actual_artifacts()
    if (
        stable_prerequisite != prerequisite
        or stable_prerequisite.receipt_sha256 != expected_receipt_sha256
    ):
        raise CampaignError("order-eight prerequisite changed during semantic replay")

    status = Path(require_env("TC_SCRATCH")).resolve(strict=True) / "status"
    gate, gate_sha256 = write_order8_gate(
        status / ORDER8_GATE_FILENAME,
        stable_prerequisite,
        job_id=job_id,
        campaign_contract_sha256=require_env("TC_CAMPAIGN_CONTRACT_SHA256"),
        order8_replay_sha256=replay_sha256,
    )
    sys.stdout.buffer.write(canonical_json_bytes({**gate.to_dict(), "gate_sha256": gate_sha256}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CampaignError, CensusError, OSError, ValueError) as exc:
        print(f"prerequisite task error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
