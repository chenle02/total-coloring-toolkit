"""Detached launch authority and explicit Slurm allocation reconciliation.

Slurm identity must be expanded by the host wrapper and passed as ordinary
arguments into a ``--cleanenv`` container. Code in the container must never
assume that ``SLURM_JOB_ID`` or ``SLURMD_NODENAME`` survived environment
cleaning. An in-allocation receipt is provisional until a hash-pinned
``sacct`` query confirms the root job's terminal state, exit code, and node.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.easley.common import CampaignError, load_json_with_snapshot
from total_coloring.external_tools import PinnedExecutable

_NODE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


@dataclass(frozen=True, slots=True)
class SlurmAllocationIdentity:
    """Explicit root-job and compute-node identity."""

    job_id: str
    node: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[1-9][0-9]*", self.job_id) is None:
            raise CampaignError("job id must be a canonical positive Slurm decimal identity")
        if _NODE_PATTERN.fullmatch(self.node) is None:
            raise CampaignError("node must be one explicit safe Slurm node name")

    def cleanenv_arguments(self) -> tuple[str, str, str, str]:
        """Return arguments to append outside a clean-environment boundary."""

        return ("--job-id", self.job_id, "--node", self.node)


@dataclass(frozen=True, slots=True)
class SacctRow:
    job_id_raw: str
    state: str
    exit_code: str
    node_list: str


def validate_detached_authority(
    path: Path,
    *,
    expected_sha256: str,
    required_bindings: Mapping[str, object],
) -> Mapping[str, Any]:
    """Validate a canonical detached authority before any campaign mutation."""

    document, snapshot = load_json_with_snapshot(path)
    if snapshot.sha256 != expected_sha256:
        raise CampaignError("detached launch authority has the wrong SHA-256")
    for key, expected in required_bindings.items():
        if key not in document or document[key] != expected:
            raise CampaignError(f"detached launch authority binding mismatch: {key}")
    return document


def parse_sacct_rows(output: str) -> tuple[SacctRow, ...]:
    """Parse ``sacct -n -P --format=JobIDRaw,State,ExitCode,NodeList`` output."""

    rows: list[SacctRow] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) < 4 or any(not field for field in fields[:4]):
            raise CampaignError("sacct returned a malformed accounting row")
        rows.append(SacctRow(*fields[:4]))
    if not rows:
        raise CampaignError("sacct returned no accounting rows")
    return tuple(rows)


def reconcile_sacct_rows(
    identity: SlurmAllocationIdentity, rows: tuple[SacctRow, ...]
) -> dict[str, object]:
    """Require one successful terminal root row bound to the recorded node."""

    roots = [row for row in rows if row.job_id_raw == identity.job_id]
    if len(roots) != 1:
        raise CampaignError("sacct must return exactly one root-job row")
    root = roots[0]
    if root.state.split()[0] != "COMPLETED":
        raise CampaignError(f"root Slurm job did not complete successfully: {root.state}")
    if root.exit_code != "0:0":
        raise CampaignError(f"root Slurm job has nonzero ExitCode: {root.exit_code}")
    if root.node_list != identity.node:
        raise CampaignError("sacct root node does not match the in-allocation receipt")
    return {
        "status": "PASS",
        "job_id": identity.job_id,
        "node": identity.node,
        "root_state": root.state,
        "root_exit_code": root.exit_code,
        "sacct_rows": [[row.job_id_raw, row.state, row.exit_code, row.node_list] for row in rows],
        "squeue_absence_not_used": True,
    }


def query_sacct(identity: SlurmAllocationIdentity, sacct: PinnedExecutable) -> dict[str, object]:
    """Query a hash-pinned ``sacct`` and reconcile the terminal root job."""

    process = subprocess.run(
        [
            str(sacct.verify()),
            "-n",
            "-P",
            "-j",
            identity.job_id,
            "--format=JobIDRaw,State,ExitCode,NodeList",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise CampaignError(f"sacct failed with {process.returncode}: {process.stderr[-1000:]}")
    return reconcile_sacct_rows(identity, parse_sacct_rows(process.stdout))


def validate_allocation_receipt(
    receipt: Mapping[str, Any],
    *,
    identity: SlurmAllocationIdentity,
    authority_sha256: str,
) -> None:
    """Bind a provisional in-allocation receipt to explicit host arguments."""

    if receipt.get("status") != "PASS_PENDING_SACCT_RECONCILIATION":
        raise CampaignError("allocation receipt is not pending terminal reconciliation")
    if receipt.get("job_id") != identity.job_id or receipt.get("node") != identity.node:
        raise CampaignError("allocation receipt does not match explicit Slurm identity")
    if receipt.get("authority_sha256") != authority_sha256:
        raise CampaignError("allocation receipt does not match detached authority")
