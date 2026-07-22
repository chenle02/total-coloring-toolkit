from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.easley.authority import (
    SlurmAllocationIdentity,
    parse_sacct_rows,
    query_sacct,
    reconcile_sacct_rows,
    validate_allocation_receipt,
    validate_detached_authority,
)
from scripts.easley.common import CampaignError, atomic_json
from total_coloring.external_tools import PinnedExecutable, sha256_file


def test_explicit_identity_survives_clean_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("SLURMD_NODENAME", raising=False)
    identity = SlurmAllocationIdentity("12345", "compute-a")

    assert identity.cleanenv_arguments() == (
        "--job-id",
        "12345",
        "--node",
        "compute-a",
    )


@pytest.mark.parametrize(
    ("job_id", "node"),
    [("", "node1"), ("0", "node1"), ("01", "node1"), ("null", "node1"), ("1", "bad node")],
)
def test_explicit_identity_rejects_ambiguous_values(job_id: str, node: str) -> None:
    with pytest.raises(CampaignError):
        SlurmAllocationIdentity(job_id, node)


def test_detached_authority_validates_hash_and_bindings_before_mutation(tmp_path: Path) -> None:
    authority = tmp_path / "authority.json"
    atomic_json(
        authority,
        {
            "launch_id": "generic-canary",
            "package_sha256": "a" * 64,
            "status": "READY",
        },
    )
    digest = sha256_file(authority)

    result = validate_detached_authority(
        authority,
        expected_sha256=digest,
        required_bindings={"launch_id": "generic-canary", "package_sha256": "a" * 64},
    )
    assert result["status"] == "READY"
    with pytest.raises(CampaignError, match="binding mismatch"):
        validate_detached_authority(
            authority,
            expected_sha256=digest,
            required_bindings={"package_sha256": "b" * 64},
        )
    with pytest.raises(CampaignError, match="wrong SHA-256"):
        validate_detached_authority(
            authority,
            expected_sha256="0" * 64,
            required_bindings={},
        )


def test_sacct_reconciliation_requires_exact_root_terminal_binding() -> None:
    identity = SlurmAllocationIdentity("12345", "compute-a")
    rows = parse_sacct_rows("12345|COMPLETED|0:0|compute-a\n12345.batch|COMPLETED|0:0|compute-a\n")
    receipt = reconcile_sacct_rows(identity, rows)
    assert receipt["status"] == "PASS"
    assert receipt["squeue_absence_not_used"] is True

    for bad in (
        "12345|FAILED|1:0|compute-a\n",
        "12345|COMPLETED|1:0|compute-a\n",
        "12345|COMPLETED|0:0|compute-b\n",
        "12345.batch|COMPLETED|0:0|compute-a\n",
        "12345|COMPLETED|0:0|compute-a\n12345|COMPLETED|0:0|compute-a\n",
    ):
        with pytest.raises(CampaignError):
            reconcile_sacct_rows(identity, parse_sacct_rows(bad))


def test_hash_pinned_sacct_query_and_provisional_receipt(tmp_path: Path) -> None:
    sacct = tmp_path / "sacct"
    sacct.write_text(
        "#!/bin/sh\nprintf '77|COMPLETED|0:0|node7\\n77.batch|COMPLETED|0:0|node7\\n'\n",
        encoding="utf-8",
    )
    sacct.chmod(0o755)
    identity = SlurmAllocationIdentity("77", "node7")
    result = query_sacct(identity, PinnedExecutable(sacct, sha256_file(sacct)))
    assert result["root_exit_code"] == "0:0"

    provisional = {
        "status": "PASS_PENDING_SACCT_RECONCILIATION",
        "job_id": "77",
        "node": "node7",
        "authority_sha256": "c" * 64,
    }
    validate_allocation_receipt(
        provisional,
        identity=identity,
        authority_sha256="c" * 64,
    )
    changed = json.loads(json.dumps(provisional))
    changed["node"] = "node8"
    with pytest.raises(CampaignError, match="explicit Slurm identity"):
        validate_allocation_receipt(
            changed,
            identity=identity,
            authority_sha256="c" * 64,
        )
