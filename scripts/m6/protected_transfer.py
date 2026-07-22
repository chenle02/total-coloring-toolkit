"""Deterministic resumable CEGAR campaign for protected row transfer.

The original Def=1 rows are retained in the SAT formula.  Only canonical Q0,
old-outside Q1, and new-outside-only Q1 endpoints using labels other than the
deleted label s are cut.  SAT with no such witness is a positive weak-core
model; raw UNSAT is frozen but is not promoted until independent reconstruction
and dual LRAT checking succeed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.easley.common import CampaignError, atomic_bytes, canonical_json_bytes
from scripts.m6 import kernel
from total_coloring.external_tools import PinnedExecutable, PinnedFile, sha256_file

S_LABEL = 0
A = 0
R_LABEL = 1
RETAINED = tuple(label for label in kernel.FACTORS if label != S_LABEL)


def sha256(path: Path) -> str:
    return sha256_file(path)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def atomic_json(path: Path, value: object) -> None:
    atomic_bytes(path, canonical_json_bytes(value), mode=0o644)


def atomic_gzip_json(path: Path, value: object) -> str:
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    atomic_bytes(path, compressed, mode=0o644)
    return hashlib.sha256(compressed).hexdigest()


def load_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise CampaignError(f"cut payload is not a JSON object: {path}")
    return value


def atomic_cnf(cnf: kernel.CNF, path: Path) -> str:
    data = cnf.dimacs_bytes()
    atomic_bytes(path, data, mode=0o644)
    return hashlib.sha256(data).hexdigest()


def transient_cnf_digest(cnf: kernel.CNF) -> str:
    return hashlib.sha256(cnf.dimacs_bytes()).hexdigest()


def run_command(
    command: Sequence[str | Path],
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    proc = subprocess.run(
        tuple(map(str, command)),
        text=True,
        capture_output=True,
        check=False,
    )
    return proc, time.monotonic() - started


def build_formula(
    q: int,
) -> tuple[
    kernel.CNF,
    kernel.RowVariables,
    dict[int, int],
    kernel.SelectedVariables,
    dict[object, int],
    dict[str, object],
    int,
]:
    if q not in {1, 3}:
        raise ValueError("protected transfer q must be 1 or 3")
    cnf, row, xrow, selected, selectors, metadata = kernel.build_base(
        noncage_mode="free",
        min_deficiency=1,
        max_deficiency=1,
        exact_row_sizes=(3, 4, 4, 4, 4, 4),
        exact_rx_mask=1 << A,
        anchor_encoding="compact",
        inactive_first_use_break=False,
    )
    unpinned = len(cnf.clauses)
    cnf.add((-row[S_LABEL, q],))
    cnf.add((-row[R_LABEL, A],))
    cnf.add((row[R_LABEL, q],))
    for label in RETAINED:
        current = kernel.edge(q, kernel.owner(S_LABEL))
        if (label, current) in selected:
            cnf.add((-selected[label, current],))
    for label in RETAINED:
        current = kernel.edge(A, kernel.owner(R_LABEL))
        if (label, current) in selected:
            cnf.add((-selected[label, current],))
    allowed = [
        variable
        for key, variable in selectors.items()
        if isinstance(key, tuple) and len(key) == 2
        for label, head in (key,)
        if label in RETAINED and not (label == R_LABEL and head == q)
    ]
    cnf.add(allowed)
    assert len(cnf.clauses) - unpinned == 13
    return cnf, row, xrow, selected, selectors, metadata, unpinned


def compact_witness(witness: kernel.EndpointWitness) -> dict[str, object]:
    return {
        "kind": witness["kind"],
        "donors": witness["donors"],
        "spare": witness["spare"],
        "cross_pair": witness["cross_pair"],
        "activated": witness["activated"],
    }


def rebuild_cut(
    cnf: kernel.CNF,
    row: Mapping[tuple[int, int], int],
    selected: Mapping[tuple[int, kernel.Edge], int],
    record: Mapping[str, Any],
) -> kernel.Clause:
    expected_fields = {"kind", "donors", "spare", "cross_pair", "activated"}
    if set(record) != expected_fields:
        raise CampaignError("cut record has an unexpected field set")
    if record["kind"] not in {"Q0", "Q1_old_outside", "Q1_new_outside_only"}:
        raise CampaignError("cut record has an unsupported endpoint kind")
    donors = record["donors"]
    if not isinstance(donors, list) or len(donors) != 3:
        raise CampaignError("cut donors must be a three-item list")
    selected_atoms = tuple(
        (int(item[0]), kernel.edge(int(item[1][0]), int(item[1][1]))) for item in donors
    )
    labels = tuple(int(item[0]) for item in donors)
    heads = tuple(int(item[2]) for item in donors)
    if record["kind"] == "Q0":
        row_atoms = tuple((True, labels[index], heads[index]) for index in range(3))
    else:
        activated = record["activated"]
        positions = [index for index, label in enumerate(labels) if label == activated]
        assert len(positions) == 1
        activated_index = positions[0]
        row_atoms = (
            *tuple((index != activated_index, labels[index], heads[index]) for index in range(3)),
            (True, int(activated), int(record["spare"])),
        )
    return kernel.witness_cut(cnf, row, selected, selected_atoms, row_atoms)


def apply_round_payload(
    cnf: kernel.CNF,
    row: Mapping[tuple[int, int], int],
    selected: Mapping[tuple[int, kernel.Edge], int],
    payload: Mapping[str, Any],
    input_digest: str,
) -> dict[str, Any]:
    assert payload["input_digest"] == input_digest
    summary = payload["summary"]
    assert summary["clauses_before_cuts"] == len(cnf.clauses)
    kinds: Counter[str] = Counter()
    for record in payload["records"]:
        clause = rebuild_cut(cnf, row, selected, record)
        assert cnf.add(clause)
        kinds[record["kind"]] += 1
    assert len(payload["records"]) == summary["new_cuts"]
    assert dict(sorted(kinds.items())) == summary["cut_types"]
    assert len(cnf.clauses) == summary["clauses_after_cuts"]
    return dict(summary)


def replay_checkpoint_cuts(
    cnf: kernel.CNF,
    row: Mapping[tuple[int, int], int],
    selected: Mapping[tuple[int, kernel.Edge], int],
    checkpoint: dict[str, Any],
    *,
    run_dir: Path,
    checkpoint_path: Path,
    input_digest: str,
) -> bool:
    """Replay committed cuts and reconcile one payload-first interruption."""

    if checkpoint["completed_sat_rounds"] != len(checkpoint["rounds"]):
        raise CampaignError("checkpoint completed-round count disagrees with its summaries")
    for expected_round, summary in enumerate(checkpoint["rounds"]):
        expected_name = f"cuts/round-{expected_round:07d}.json.gz"
        if summary.get("round") != expected_round or summary.get("cut_file") != expected_name:
            raise CampaignError("checkpoint rounds are not in canonical contiguous order")
        cut_path = run_dir / summary["cut_file"]
        if sha256(cut_path) != summary["cut_file_sha256"]:
            raise CampaignError(f"cut payload hash mismatch: {cut_path}")
        payload = load_gzip_json(cut_path)
        if (
            payload.get("schema") != "total-coloring.m6-protected-transfer-cut-round.v2"
            or payload.get("round") != expected_round
        ):
            raise CampaignError("cut payload has the wrong schema or round")
        rebuilt = apply_round_payload(cnf, row, selected, payload, input_digest)
        if rebuilt != {key: summary[key] for key in rebuilt}:
            raise CampaignError("checkpoint round summary differs from its cut payload")
    if len(cnf.clauses) != checkpoint["clauses_after_cuts"]:
        raise CampaignError("checkpoint clause count does not match replayed cuts")

    referenced = {summary["cut_file"] for summary in checkpoint["rounds"]}
    available = {
        str(path.relative_to(run_dir)) for path in (run_dir / "cuts").glob("round-*.json.gz")
    }
    missing = referenced - available
    extras = sorted(available - referenced)
    if missing:
        raise CampaignError(f"checkpoint references missing cut payloads: {sorted(missing)}")
    if not extras:
        return False
    expected = f"cuts/round-{checkpoint['completed_sat_rounds']:07d}.json.gz"
    if extras != [expected]:
        raise CampaignError("cut inventory has more than one exact resumable orphan")
    cut_path = run_dir / expected
    payload = load_gzip_json(cut_path)
    if (
        payload.get("schema") != "total-coloring.m6-protected-transfer-cut-round.v2"
        or payload.get("round") != checkpoint["completed_sat_rounds"]
    ):
        raise CampaignError("orphan cut payload has the wrong schema or round")
    payload_summary = payload.get("summary")
    if not isinstance(payload_summary, dict):
        raise CampaignError("orphan cut payload has no summary")
    if transient_cnf_digest(cnf) != payload_summary.get("solver_cnf_sha256"):
        raise CampaignError("orphan cut payload does not continue the checkpoint CNF")
    summary = apply_round_payload(cnf, row, selected, payload, input_digest)
    summary = {**summary, "cut_file": expected, "cut_file_sha256": sha256(cut_path)}
    checkpoint["rounds"].append(summary)
    checkpoint["completed_sat_rounds"] += 1
    checkpoint["cut_count"] += summary["new_cuts"]
    checkpoint["cut_types"] = dict(
        sorted((Counter(checkpoint["cut_types"]) + Counter(summary["cut_types"])).items())
    )
    checkpoint["clauses_after_cuts"] = len(cnf.clauses)
    checkpoint["status"] = "running"
    atomic_json(checkpoint_path, checkpoint)
    return True


def retained_witnesses(
    rows: Mapping[int, frozenset[int]],
    factors: Mapping[int, frozenset[kernel.Edge]],
    cnf: kernel.CNF,
    row: Mapping[tuple[int, int], int],
    selected: Mapping[tuple[int, kernel.Edge], int],
) -> tuple[list[kernel.EndpointWitness], list[kernel.EndpointWitness]]:
    all_witnesses = kernel.endpoint_witnesses(rows, factors, cnf, row, selected)
    retained = [
        witness
        for witness in all_witnesses
        if all(item[0] != S_LABEL for item in witness["selected_atoms"])
    ]
    retained.sort(key=lambda witness: tuple(witness["cut"]))
    return all_witnesses, retained


def validate_pins(
    rows: Mapping[int, frozenset[int]],
    rx: frozenset[int],
    factors: Mapping[int, frozenset[kernel.Edge]],
    q: int,
) -> dict[str, object]:
    assert rx == {A}
    assert [len(rows[label]) for label in kernel.FACTORS] == [3, 4, 4, 4, 4, 4]
    assert q not in rows[S_LABEL]
    assert A not in rows[R_LABEL] and q in rows[R_LABEL]
    assert all(kernel.edge(q, kernel.owner(S_LABEL)) not in factors[label] for label in RETAINED)
    assert all(kernel.edge(A, kernel.owner(R_LABEL)) not in factors[label] for label in RETAINED)
    anchors = [
        anchor
        for anchor in kernel.selected_anchors(rows, factors)
        if anchor[0] in RETAINED and not (anchor[0] == R_LABEL and anchor[2] == q)
    ]
    assert anchors

    changed = dict(rows)
    changed[R_LABEL] = frozenset((set(rows[R_LABEL]) - {q}) | {A})
    exposed = frozenset(set(rows[S_LABEL]) | {q})
    assert len(exposed) == 4
    assert all(len(changed[label]) == 4 for label in RETAINED)
    assert all(
        sum(head in changed[label] for label in RETAINED) + (head in exposed) == 4
        for head in kernel.C
    )
    for label in RETAINED:
        for current in factors[label]:
            core = set(current) & kernel.C
            outside = set(current) & kernel.OUTSIDE
            if len(core) != 1 or len(outside) != 1:
                continue
            head = next(iter(core))
            tail = next(iter(outside))
            if tail == kernel.owner(S_LABEL):
                assert head not in exposed
            elif tail in tuple(kernel.owner(other) for other in RETAINED):
                assert head not in changed[kernel.OWNERS.index(tail)]
    return {
        "allowed_retained_anchor_count": len(anchors),
        "first_allowed_retained_anchor": [
            anchors[0][0],
            list(anchors[0][1]),
            anchors[0][2],
            anchors[0][3],
        ],
        "new_r_row": sorted(changed[R_LABEL]),
        "new_exposed_row": sorted(exposed),
        "transformed_column_totals": [4] * 6,
        "safe_transfer_graph_law_verified": True,
    }


def independent_check(checker: PinnedFile, run_dir: Path) -> tuple[dict[str, Any], float]:
    output = run_dir / "independent-check.json"
    proc, elapsed = run_command(
        (
            sys.executable,
            checker.verify(),
            "--run-dir",
            run_dir,
            "--output",
            output,
        )
    )
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-3000:])
    result = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise CampaignError("independent checker output must be a JSON object")
    assert result["status"] == "PASS"
    return result, elapsed


def prove_unsat(
    run_dir: Path,
    checkpoint: Mapping[str, Any],
    *,
    checker: PinnedFile,
    solver: PinnedExecutable,
    lrat_trim: PinnedExecutable,
    lrat_check: PinnedExecutable,
) -> dict[str, object]:
    independent, independent_seconds = independent_check(checker, run_dir)
    assert independent["run_status"] == "candidate_unsat"
    final_cnf = run_dir / "terminal.cnf"
    final_lrat = run_dir / "terminal.lrat"
    proc, solver_seconds = run_command(
        (
            solver.verify(),
            "--quiet",
            "--seed=0",
            "--lrat",
            "--no-binary",
            final_cnf,
            final_lrat,
        )
    )
    assert proc.returncode == 20, (proc.stdout[-3000:], proc.stderr[-3000:])
    strict, strict_seconds = run_command((lrat_trim.verify(), "--strict", final_cnf, final_lrat))
    assert strict.returncode == 20 and "s VERIFIED" in strict.stdout
    separate, separate_seconds = run_command((lrat_check.verify(), final_cnf, final_lrat))
    assert separate.returncode == 0 and "c VERIFIED" in separate.stdout
    receipt = {
        "schema": "total-coloring.m6-protected-transfer-proof.v2",
        "status": "verified_unsat",
        "input_digest": checkpoint["input_digest"],
        "q_vertex": checkpoint["config"]["q_vertex"],
        "variables": checkpoint["variables"],
        "clauses": checkpoint["clauses_after_cuts"],
        "cuts": checkpoint["cut_count"],
        "terminal_cnf_sha256": sha256(final_cnf),
        "terminal_lrat_sha256": sha256(final_lrat),
        "independent_check_sha256": sha256(run_dir / "independent-check.json"),
        "strict_lrat_trim_verified": True,
        "separate_lrat_check_verified": True,
        "tools": {
            "solver": solver.identity(),
            "strict_lrat_checker": lrat_trim.identity(),
            "independent_lrat_checker": lrat_check.identity(),
        },
        "seconds": {
            "independent_reconstruction": round(independent_seconds, 6),
            "cadical_lrat": round(solver_seconds, 6),
            "strict_lrat_trim": round(strict_seconds, 6),
            "separate_lrat_check": round(separate_seconds, 6),
        },
    }
    atomic_json(run_dir / "proof-receipt.json", receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--q", type=int, choices=(1, 3), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--max-total-rounds", type=int, default=1_000_000)
    parser.add_argument("--max-invocation-seconds", type=float, default=600.0)
    parser.add_argument("--solver", type=Path, required=True)
    parser.add_argument("--solver-sha256", required=True)
    parser.add_argument("--independent-checker", type=Path, required=True)
    parser.add_argument("--independent-checker-sha256", required=True)
    parser.add_argument("--independent-helper", type=Path, required=True)
    parser.add_argument("--independent-helper-sha256", required=True)
    parser.add_argument("--lrat-trim", type=Path, required=True)
    parser.add_argument("--lrat-trim-sha256", required=True)
    parser.add_argument("--lrat-check", type=Path, required=True)
    parser.add_argument("--lrat-check-sha256", required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--runtime-config-sha256", required=True)
    parser.add_argument("--prove-on-candidate-unsat", action="store_true")
    return parser


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot load JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignError(f"JSON artifact is not an object: {path}")
    return value


def _promote_sat_candidate(
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    *,
    checker: PinnedFile,
    run_dir: Path,
) -> dict[str, Any]:
    independent, _elapsed = independent_check(checker, run_dir)
    terminal = independent.get("terminal_model_check")
    if not isinstance(terminal, dict) or terminal.get("retained_endpoint_types") != {}:
        raise CampaignError("independent checker did not confirm the endpoint-negative model")
    checkpoint["status"] = "verified_sat_no_retained_endpoint"
    checkpoint["independent_check_sha256"] = sha256(run_dir / "independent-check.json")
    atomic_json(checkpoint_path, checkpoint)
    return independent


def main() -> int:
    if not __debug__:
        raise CampaignError("the m=6 campaign must not run with Python optimization enabled")
    args = _parser().parse_args()
    if args.max_total_rounds < 0 or args.max_invocation_seconds < 0:
        raise CampaignError("round and wall-time limits must be nonnegative")

    solver = PinnedExecutable(args.solver, args.solver_sha256)
    checker = PinnedFile(args.independent_checker, args.independent_checker_sha256)
    helper = PinnedFile(args.independent_helper, args.independent_helper_sha256)
    if helper.path != checker.path.with_name("independent_static.py"):
        raise CampaignError("the independent helper must be adjacent to the independent checker")
    lrat_trim = PinnedExecutable(args.lrat_trim, args.lrat_trim_sha256)
    lrat_check = PinnedExecutable(args.lrat_check, args.lrat_check_sha256)
    runtime_config = PinnedFile(args.runtime_config, args.runtime_config_sha256)
    python_runtime = PinnedFile(Path(sys.executable), sha256(Path(sys.executable)))
    kernel_source = PinnedFile(Path(kernel.__file__), sha256(Path(kernel.__file__)))
    campaign_source = PinnedFile(Path(__file__), sha256(Path(__file__)))
    config = {
        "schema": "total-coloring.m6-protected-transfer-config.v2",
        "finite_scope": "m=6, D=26, Def=1, row sizes 3,4,4,4,4,4",
        "s_label": S_LABEL,
        "a_vertex": A,
        "r_label": R_LABEL,
        "q_vertex": args.q,
        "q_shore_relation": "same" if args.q in kernel.T0 else "opposite",
        "row_sizes": [3, 4, 4, 4, 4, 4],
        "rx_mask": 1 << A,
        "safe_transfer_pins": ["not_Rs_q", "not_Rr_a", "Rr_q", "no_q_owner_s", "no_a_owner_r"],
        "anchor_scope": "retained labels, excluding selector (r,q)",
        "endpoint_scope": "retained-label canonical Q0 + old-outside Q1 + new-outside-only Q1",
        "anchor_encoding": "compact",
        "inactive_first_use_break": False,
        "solver_seed": 0,
    }
    provenance = {
        "kernel": kernel_source.identity(),
        "campaign": campaign_source.identity(),
        "independent_checker": checker.identity(),
        "independent_helper": helper.identity(),
        "solver": solver.identity(),
        "strict_lrat_checker": lrat_trim.identity(),
        "independent_lrat_checker": lrat_check.identity(),
        "runtime_config": runtime_config.identity(),
        "python_runtime": python_runtime.identity(),
    }
    input_digest = hashlib.sha256(
        canonical_json({"config": config, "provenance": provenance}).encode()
    ).hexdigest()

    run_dir = args.run_dir.resolve()
    checkpoint_path = run_dir / "checkpoint.json"
    checkpoint: dict[str, Any] | None = None
    if checkpoint_path.exists():
        checkpoint = _json_object(checkpoint_path)
        if (
            checkpoint.get("input_digest") != input_digest
            or checkpoint.get("config") != config
            or checkpoint.get("provenance") != provenance
        ):
            raise CampaignError("existing run identity differs from the requested campaign")
    elif run_dir.exists():
        if any(run_dir.iterdir()):
            raise CampaignError("nonempty run directory lacks a checkpoint")
    else:
        run_dir.mkdir(parents=True)
    cuts_dir = run_dir / "cuts"
    cuts_dir.mkdir(exist_ok=True)

    cnf, row, xrow, selected, _selectors, _metadata, unpinned = build_formula(args.q)
    baseline_path = run_dir / "baseline.cnf"
    if checkpoint is None:
        baseline_sha = atomic_cnf(cnf, baseline_path)
        checkpoint = {
            "schema": "total-coloring.m6-protected-transfer-checkpoint.v2",
            "input_digest": input_digest,
            "config": config,
            "provenance": provenance,
            "variables": len(cnf.key_of) - 1,
            "unpinned_base_clauses": unpinned,
            "pinned_baseline_clauses": len(cnf.clauses),
            "baseline_cnf_sha256": baseline_sha,
            "completed_sat_rounds": 0,
            "cut_count": 0,
            "cut_types": {},
            "clauses_after_cuts": len(cnf.clauses),
            "rounds": [],
            "status": "ready",
        }
        atomic_json(checkpoint_path, checkpoint)
    else:
        assert baseline_path.is_file()
        baseline_sha = transient_cnf_digest(cnf)
        assert sha256(baseline_path) == baseline_sha == checkpoint["baseline_cnf_sha256"]
        assert len(cnf.key_of) - 1 == checkpoint["variables"]
        assert unpinned == checkpoint["unpinned_base_clauses"]
        assert len(cnf.clauses) == checkpoint["pinned_baseline_clauses"]

    replay_checkpoint_cuts(
        cnf,
        row,
        selected,
        checkpoint,
        run_dir=run_dir,
        checkpoint_path=checkpoint_path,
        input_digest=input_digest,
    )

    if checkpoint["status"] == "candidate_sat_no_retained_endpoint":
        _promote_sat_candidate(checkpoint, checkpoint_path, checker=checker, run_dir=run_dir)
    if checkpoint["status"] == "verified_sat_no_retained_endpoint":
        independent_check(checker, run_dir)
        print(
            json.dumps({"status": checkpoint["status"], "resumed_terminal": True}, sort_keys=True)
        )
        return 0
    if checkpoint["status"] == "candidate_unsat":
        proof = (
            prove_unsat(
                run_dir,
                checkpoint,
                checker=checker,
                solver=solver,
                lrat_trim=lrat_trim,
                lrat_check=lrat_check,
            )
            if args.prove_on_candidate_unsat
            else None
        )
        print(
            json.dumps(
                {
                    "status": checkpoint["status"],
                    "proof": proof and proof["status"],
                    "resumed_terminal": True,
                },
                sort_keys=True,
            )
        )
        return 0

    completed = checkpoint["completed_sat_rounds"]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"tc-protected-q{args.q}-") as directory:
        temporary = Path(directory)
        while completed < args.max_total_rounds:
            if time.monotonic() - started >= args.max_invocation_seconds:
                break
            cnf_path = temporary / f"round-{completed:07d}.cnf"
            status, truth, _elapsed, digest = kernel.solve(cnf, solver, cnf_path)
            cnf_path.unlink()
            if status == "UNSAT":
                terminal_sha = atomic_cnf(cnf, run_dir / "terminal.cnf")
                checkpoint.update(
                    {
                        "status": "candidate_unsat",
                        "clauses_after_cuts": len(cnf.clauses),
                        "terminal_cnf_sha256": terminal_sha,
                        "terminal_raw_solver_cnf_sha256": digest,
                    }
                )
                atomic_json(checkpoint_path, checkpoint)
                break

            rows, rx, factors = kernel.decode(truth, row, xrow, selected)
            semantic = kernel.semantic_check(rows, rx, factors, require_noncage=True)
            pin_semantic = validate_pins(rows, rx, factors, args.q)
            all_witnesses, witnesses = retained_witnesses(rows, factors, cnf, row, selected)
            if not witnesses:
                terminal_sha = atomic_cnf(cnf, run_dir / "terminal.cnf")
                model_path = run_dir / "endpoint-negative-model.json"
                atomic_json(
                    model_path,
                    {
                        "schema": "total-coloring.m6-protected-transfer-sat-model.v2",
                        "status": "candidate_sat_no_retained_endpoint",
                        "input_digest": input_digest,
                        "q_vertex": args.q,
                        "solver_cnf_sha256": digest,
                        "semantic_check": semantic,
                        "pin_semantic_check": pin_semantic,
                        "retained_endpoint_types": {},
                        "all_label_endpoint_types": dict(
                            sorted(Counter(item["kind"] for item in all_witnesses).items())
                        ),
                        "model": kernel.serialize_model(rows, rx, factors),
                    },
                )
                checkpoint.update(
                    {
                        "status": "candidate_sat_no_retained_endpoint",
                        "terminal_cnf_sha256": terminal_sha,
                        "terminal_sat_solver_cnf_sha256": digest,
                        "endpoint_negative_model_sha256": sha256(model_path),
                    }
                )
                atomic_json(checkpoint_path, checkpoint)
                _promote_sat_candidate(
                    checkpoint, checkpoint_path, checker=checker, run_dir=run_dir
                )
                break

            records = [compact_witness(witness) for witness in witnesses]
            cut_count_before = len(cnf.clauses)
            for witness in witnesses:
                assert cnf.add(witness["cut"])
            kinds = dict(sorted(Counter(record["kind"] for record in records).items()))
            summary = {
                "round": completed,
                "solver_status": "SAT",
                "solver_cnf_sha256": digest,
                "clauses_before_cuts": cut_count_before,
                "new_cuts": len(records),
                "cut_types": kinds,
                "clauses_after_cuts": len(cnf.clauses),
            }
            payload = {
                "schema": "total-coloring.m6-protected-transfer-cut-round.v2",
                "input_digest": input_digest,
                "round": completed,
                "summary": summary,
                "records": records,
            }
            relative = f"cuts/round-{completed:07d}.json.gz"
            cut_path = run_dir / relative
            cut_sha = atomic_gzip_json(cut_path, payload)
            checkpoint["rounds"].append(
                {**summary, "cut_file": relative, "cut_file_sha256": cut_sha}
            )
            checkpoint["completed_sat_rounds"] += 1
            checkpoint["cut_count"] += len(records)
            checkpoint["cut_types"] = dict(
                sorted((Counter(checkpoint["cut_types"]) + Counter(kinds)).items())
            )
            checkpoint["clauses_after_cuts"] = len(cnf.clauses)
            checkpoint["status"] = "running"
            atomic_json(checkpoint_path, checkpoint)
            completed += 1

    if checkpoint["status"] not in {
        "verified_sat_no_retained_endpoint",
        "candidate_unsat",
    }:
        checkpoint["status"] = "limit"
        checkpoint["clauses_after_cuts"] = len(cnf.clauses)
        atomic_json(checkpoint_path, checkpoint)
    proof = None
    if checkpoint["status"] == "candidate_unsat" and args.prove_on_candidate_unsat:
        proof = prove_unsat(
            run_dir,
            checkpoint,
            checker=checker,
            solver=solver,
            lrat_trim=lrat_trim,
            lrat_check=lrat_check,
        )
    print(
        json.dumps(
            {
                "status": checkpoint["status"],
                "q": args.q,
                "rounds": checkpoint["completed_sat_rounds"],
                "cuts": checkpoint["cut_count"],
                "clauses": checkpoint["clauses_after_cuts"],
                "proof": proof and proof["status"],
                "wall_seconds": round(time.monotonic() - started, 6),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
