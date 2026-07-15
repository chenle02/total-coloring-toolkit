#!/usr/bin/env python3
"""Fail-closed reduction of distributed validation receipts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts.easley.common import (
    CampaignError,
    atomic_json,
    easley_shard_count_env,
    load_json,
    positive_env,
    require_env,
    runtime_paths,
    runtime_receipt_sha256,
    sha256_file,
    shard_directory,
)
from scripts.easley.prerequisite import REDUCE_SCHEMA, VALIDATION_SCHEMA

COUNT_NAMES = ("verified_all", "candidate_unsat", "unknown", "error", "skipped")


def _expected(name: str) -> int:
    return positive_env(name) if name != "TC_EXPECTED_ADVERSE" else 0


def main() -> int:
    if "SLURM_JOB_ID" not in os.environ:
        raise CampaignError("reducer must run under Slurm")
    _, _, runtime_receipt = runtime_paths()
    order = positive_env("TC_ORDER")
    shard_count = easley_shard_count_env()
    scratch = Path(require_env("TC_SCRATCH")).resolve()
    status = scratch / "status"
    runtime_receipt_digest = runtime_receipt_sha256()
    campaign_contract_sha256 = require_env("TC_CAMPAIGN_CONTRACT_SHA256")
    totals = {
        "record_count": 0,
        "partition_count": 0,
        "check_evaluations": 0,
        "records_bytes": 0,
    }
    counts = dict.fromkeys(COUNT_NAMES, 0)
    receipts: list[dict[str, object]] = []
    toolkit: object | None = None
    for index in range(shard_count):
        receipt_path = status / f"validation-complete-{index:03d}.json"
        receipt = load_json(receipt_path)
        if (
            receipt.get("status") != "validation_complete"
            or receipt.get("schema_version") != VALIDATION_SCHEMA
            or receipt.get("order") != order
            or receipt.get("shard_index") != index
            or receipt.get("shard_count") != shard_count
            or receipt.get("code_commit") != runtime_receipt["code_commit"]
            or receipt.get("geng_sha256") != runtime_receipt["geng_sha256"]
            or receipt.get("launcher_archive_sha256") != runtime_receipt["launcher_archive_sha256"]
            or receipt.get("launcher_sha256") != runtime_receipt["launcher_sha256"]
            or receipt.get("runtime_receipt_sha256") != runtime_receipt_digest
            or receipt.get("campaign_contract_sha256") != campaign_contract_sha256
            or receipt.get("wheel_sha256") != runtime_receipt["wheel_sha256"]
        ):
            raise CampaignError(f"validation receipt {index} violates the campaign contract")
        if toolkit is None:
            toolkit = receipt.get("toolkit")
        elif receipt.get("toolkit") != toolkit:
            raise CampaignError("validation receipts contain mixed toolkit identities")
        directory = shard_directory(index, shard_count)
        if sha256_file(directory / "manifest.json") != receipt.get("manifest_sha256"):
            raise CampaignError(f"shard {index} manifest changed after validation")
        if sha256_file(directory / "completion.json") != receipt.get("completion_sha256"):
            raise CampaignError(f"shard {index} completion marker changed after validation")
        raw_counts = receipt.get("counts")
        if not isinstance(raw_counts, dict) or set(raw_counts) != set(COUNT_NAMES):
            raise CampaignError(f"shard {index} has malformed status counts")
        for name in COUNT_NAMES:
            value = raw_counts[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CampaignError(f"shard {index} has invalid count {name}")
            counts[name] += value
        for name in totals:
            value = receipt.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CampaignError(f"shard {index} has invalid total {name}")
            totals[name] += value
        receipts.append(
            {
                "manifest_sha256": receipt["manifest_sha256"],
                "record_count": receipt["record_count"],
                "run_fingerprint": receipt["run_fingerprint"],
                "shard_index": index,
            }
        )

    expected = {
        "record_count": _expected("TC_EXPECTED_RECORDS"),
        "partition_count": _expected("TC_EXPECTED_PARTITIONS"),
        "check_evaluations": _expected("TC_EXPECTED_CHECKS"),
        "verified_all": _expected("TC_EXPECTED_VERIFIED"),
        "skipped": _expected("TC_EXPECTED_SKIPPED"),
    }
    for name in ("record_count", "partition_count", "check_evaluations"):
        if totals[name] != expected[name]:
            raise CampaignError(f"aggregate {name}: expected {expected[name]}, got {totals[name]}")
    for name in ("verified_all", "skipped"):
        if counts[name] != expected[name]:
            raise CampaignError(f"aggregate {name}: expected {expected[name]}, got {counts[name]}")
    adverse = counts["candidate_unsat"] + counts["unknown"] + counts["error"]
    if adverse != 0:
        raise CampaignError(f"array completed with {adverse} adverse scientific status(es)")
    if sum(counts.values()) != totals["record_count"]:
        raise CampaignError("aggregate status counts do not equal the graph total")
    if totals["check_evaluations"] != 3 * totals["partition_count"]:
        raise CampaignError("aggregate check count does not equal three times partitions")

    atomic_json(
        status / "reduce-complete.json",
        {
            "campaign_contract_sha256": campaign_contract_sha256,
            "code_commit": runtime_receipt["code_commit"],
            "counts": counts,
            "geng_sha256": runtime_receipt["geng_sha256"],
            "launcher_archive_sha256": runtime_receipt["launcher_archive_sha256"],
            "launcher_sha256": runtime_receipt["launcher_sha256"],
            "order": order,
            "receipts": receipts,
            "runtime_receipt_sha256": runtime_receipt_digest,
            "schema_version": REDUCE_SCHEMA,
            "shard_count": shard_count,
            "status": "reduce_complete",
            "toolkit": toolkit,
            "totals": totals,
            "wheel_sha256": runtime_receipt["wheel_sha256"],
        },
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CampaignError, OSError, ValueError) as exc:
        print(f"reducer error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
