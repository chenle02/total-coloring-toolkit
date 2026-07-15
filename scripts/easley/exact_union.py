#!/usr/bin/env python3
"""Final exact shard-union validation and completion marker."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts.easley.common import (
    CampaignError,
    atomic_json,
    easley_shard_count_env,
    load_json_with_snapshot,
    positive_env,
    require_env,
    runtime_paths,
    runtime_receipt_sha256,
    shard_directory,
)
from scripts.easley.prerequisite import (
    EXACT_UNION_SCHEMA,
    REDUCE_SCHEMA,
    Order8Gate,
    Order8Prerequisite,
    validate_order8_gate,
    validate_order8_prerequisite,
)
from scripts.easley.prerequisite_task import replay_order8_prerequisite
from total_coloring.census import CensusError
from total_coloring.universal_shards import (
    recheck_universal_shard_artifact_inventory,
    validate_completed_universal_shard_set,
)


def _validate_order8_evidence(
    gate: Order8Gate,
    *,
    geng: Path,
) -> tuple[Order8Prerequisite, str]:
    """Independently revalidate and replay the evidence named by an order-nine gate."""

    receipt_path = Path(require_env("TC_ORDER8_RECEIPT_PATH"))
    prerequisite = validate_order8_prerequisite(
        receipt_path,
        runtime=Path(require_env("TC_RUNTIME")).resolve(strict=True),
        code_commit=require_env("TC_CODE_COMMIT"),
        launcher_archive_sha256=require_env("TC_LAUNCHER_ARCHIVE_SHA256"),
        launcher_digest=require_env("TC_LAUNCHER_SHA256"),
        toolkit_version=require_env("TC_TOOLKIT_VERSION"),
        wheel_sha256=require_env("TC_WHEEL_SHA256"),
        verify_artifacts=True,
    )
    if (
        prerequisite.receipt_sha256 != require_env("TC_ORDER8_RECEIPT_SHA256")
        or prerequisite.receipt_sha256 != gate.order8_receipt_sha256
        or prerequisite.runtime_receipt_sha256 != gate.runtime_receipt_sha256
        or prerequisite.code_commit != gate.code_commit
        or prerequisite.geng_sha256 != gate.geng_sha256
        or prerequisite.wheel_sha256 != gate.wheel_sha256
        or prerequisite.launcher_sha256 != gate.launcher_sha256
        or prerequisite.launcher_archive_sha256 != gate.launcher_archive_sha256
        or prerequisite.order8_artifact_root_sha256 != gate.order8_artifact_root_sha256
    ):
        raise CampaignError("order-eight gate does not match the retained actual artifacts")
    replay_sha256 = replay_order8_prerequisite(
        receipt_path,
        geng=geng,
        geng_sha256=prerequisite.geng_sha256,
        max_union_graphs=positive_env("TC_MAX_UNION_GRAPHS"),
    )
    if replay_sha256 != gate.order8_replay_sha256:
        raise CampaignError("order-eight gate does not match the independent semantic replay")
    stable = validate_order8_prerequisite(
        receipt_path,
        runtime=Path(require_env("TC_RUNTIME")).resolve(strict=True),
        code_commit=require_env("TC_CODE_COMMIT"),
        launcher_archive_sha256=require_env("TC_LAUNCHER_ARCHIVE_SHA256"),
        launcher_digest=require_env("TC_LAUNCHER_SHA256"),
        toolkit_version=require_env("TC_TOOLKIT_VERSION"),
        wheel_sha256=require_env("TC_WHEEL_SHA256"),
        verify_artifacts=True,
    )
    if stable != prerequisite:
        raise CampaignError("order-eight actual artifacts changed during semantic replay")
    return stable, replay_sha256


def main() -> int:
    if "SLURM_JOB_ID" not in os.environ:
        raise CampaignError("exact-union validator must run under Slurm")
    _, geng, runtime_receipt = runtime_paths()
    shard_count = easley_shard_count_env()
    order = positive_env("TC_ORDER")
    expected_records = positive_env("TC_EXPECTED_RECORDS")
    expected_partitions = positive_env("TC_EXPECTED_PARTITIONS")
    expected_checks = positive_env("TC_EXPECTED_CHECKS")
    expected_verified = positive_env("TC_EXPECTED_VERIFIED")
    expected_skipped = positive_env("TC_EXPECTED_SKIPPED")
    status = Path(require_env("TC_SCRATCH")).resolve() / "status"
    order8_gate = None
    order8_gate_sha256 = None
    order8_evidence = None
    campaign_contract_sha256 = require_env("TC_CAMPAIGN_CONTRACT_SHA256")
    if order == 9:
        require_env("TC_ORDER8_RECEIPT_PATH")
        order8_gate, order8_gate_sha256 = validate_order8_gate(
            status,
            runtime_receipt=runtime_receipt,
            expected_receipt_sha256=require_env("TC_ORDER8_RECEIPT_SHA256"),
            expected_campaign_contract_sha256=campaign_contract_sha256,
        )
        order8_evidence = _validate_order8_evidence(order8_gate, geng=geng)
    elif any(name in os.environ for name in ("TC_ORDER8_RECEIPT_PATH", "TC_ORDER8_RECEIPT_SHA256")):
        raise CampaignError("only order nine may carry an order-eight prerequisite")
    reduce_receipt_path = status / "reduce-complete.json"
    reduce_receipt, reduce_snapshot = load_json_with_snapshot(reduce_receipt_path)
    if reduce_receipt.get("status") != "reduce_complete":
        raise CampaignError("distributed validation reducer is not complete")
    run_directories = [shard_directory(index, shard_count) for index in range(shard_count)]
    result = validate_completed_universal_shard_set(
        run_directories,
        executable=str(geng),
        max_union_graphs=positive_env("TC_MAX_UNION_GRAPHS"),
    )
    if (
        result.order != order
        or result.record_count != expected_records
        or result.partition_count != expected_partitions
        or result.check_evaluations != expected_checks
        or result.counts.verified_all != expected_verified
        or result.counts.skipped != expected_skipped
        or result.counts.candidate_unsat
        or result.counts.unknown
        or result.counts.error
    ):
        raise CampaignError("exact shard-union result violates the expected scientific contract")
    runtime_receipt_digest = runtime_receipt_sha256()
    if (
        reduce_receipt.get("schema_version") != REDUCE_SCHEMA
        or reduce_receipt.get("order") != order
        or reduce_receipt.get("shard_count") != shard_count
        or reduce_receipt.get("code_commit") != runtime_receipt["code_commit"]
        or reduce_receipt.get("launcher_archive_sha256")
        != runtime_receipt["launcher_archive_sha256"]
        or reduce_receipt.get("launcher_sha256") != runtime_receipt["launcher_sha256"]
        or reduce_receipt.get("runtime_receipt_sha256") != runtime_receipt_digest
        or reduce_receipt.get("campaign_contract_sha256") != campaign_contract_sha256
        or reduce_receipt.get("geng_sha256") != runtime_receipt["geng_sha256"]
        or reduce_receipt.get("wheel_sha256") != runtime_receipt["wheel_sha256"]
        or reduce_receipt.get("counts") != result.counts.to_dict()
        or reduce_receipt.get("totals")
        != {
            "check_evaluations": result.check_evaluations,
            "partition_count": result.partition_count,
            "record_count": result.record_count,
            "records_bytes": result.records_bytes,
        }
    ):
        raise CampaignError("reduce receipt does not match the exact shard-union replay")
    payload = result.to_dict()
    payload.update(
        {
            "campaign_contract_sha256": campaign_contract_sha256,
            "geng_sha256": runtime_receipt["geng_sha256"],
            "job_id": os.environ["SLURM_JOB_ID"],
            "code_commit": runtime_receipt["code_commit"],
            "launcher_archive_sha256": runtime_receipt["launcher_archive_sha256"],
            "launcher_sha256": runtime_receipt["launcher_sha256"],
            "reduce_receipt_sha256": reduce_snapshot.sha256,
            "runtime_receipt_sha256": runtime_receipt_digest,
            "schema_version": EXACT_UNION_SCHEMA,
            "status": "exact_union_complete",
            "wheel_sha256": runtime_receipt["wheel_sha256"],
        }
    )
    if order == 9:
        assert order8_gate is not None
        assert order8_gate_sha256 is not None
        assert order8_evidence is not None
        final_evidence = _validate_order8_evidence(order8_gate, geng=geng)
        if final_evidence != order8_evidence:
            raise CampaignError("order-eight actual evidence changed during exact validation")
        final_gate, final_gate_sha256 = validate_order8_gate(
            status,
            runtime_receipt=runtime_receipt,
            expected_receipt_sha256=require_env("TC_ORDER8_RECEIPT_SHA256"),
            expected_campaign_contract_sha256=campaign_contract_sha256,
        )
        if final_gate != order8_gate or final_gate_sha256 != order8_gate_sha256:
            raise CampaignError("order-eight prerequisite gate changed during exact validation")
        payload.update(
            {
                "order8_artifact_root_sha256": order8_gate.order8_artifact_root_sha256,
                "order8_prerequisite_gate_sha256": order8_gate_sha256,
                "order8_receipt_sha256": order8_gate.order8_receipt_sha256,
                "order8_replay_sha256": order8_gate.order8_replay_sha256,
            }
        )
    recheck_universal_shard_artifact_inventory(result, run_directories)
    final_reduce, final_reduce_snapshot = load_json_with_snapshot(reduce_receipt_path)
    if final_reduce != reduce_receipt or final_reduce_snapshot != reduce_snapshot:
        raise CampaignError("reduce receipt changed during exact shard-union validation")
    completion_path = status / "exact-union-complete.json"
    atomic_json(completion_path, payload, mode=0o444)
    written, _ = load_json_with_snapshot(completion_path)
    if written != payload:
        raise CampaignError("exact shard-union completion receipt failed its write verification")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CampaignError, CensusError, OSError, ValueError) as exc:
        print(f"exact-union error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
