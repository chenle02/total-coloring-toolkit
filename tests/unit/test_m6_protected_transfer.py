from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from scripts.easley.common import CampaignError
from scripts.m6 import check_protected_transfer as independent
from scripts.m6 import kernel
from scripts.m6 import protected_transfer as campaign
from total_coloring.external_tools import PinnedFile, sha256_file

REFERENCE_BASELINES = {
    1: "10d430f18005abb27648f0885381687a68479622a332d09513030d7f8576e386",
    3: "65a1031f2248af17755c91984f3d9d389c6d79dbe58d6a288cb34b1fa39f8890",
}


def _config(q: int) -> dict[str, object]:
    return {
        "schema": "total-coloring.m6-protected-transfer-config.v2",
        "finite_scope": "m=6, D=26, Def=1, row sizes 3,4,4,4,4,4",
        "s_label": 0,
        "a_vertex": 0,
        "r_label": 1,
        "q_vertex": q,
        "q_shore_relation": "same" if q == 1 else "opposite",
        "row_sizes": [3, 4, 4, 4, 4, 4],
        "rx_mask": 1,
        "safe_transfer_pins": [
            "not_Rs_q",
            "not_Rr_a",
            "Rr_q",
            "no_q_owner_s",
            "no_a_owner_r",
        ],
        "anchor_scope": "retained labels, excluding selector (r,q)",
        "endpoint_scope": ("retained-label canonical Q0 + old-outside Q1 + new-outside-only Q1"),
        "anchor_encoding": "compact",
        "inactive_first_use_break": False,
        "solver_seed": 0,
    }


@pytest.mark.parametrize("q", [1, 3])
def test_protected_formula_matches_reference_and_independent_rebuild(q: int) -> None:
    producer, row, xrow, selected, selectors, _metadata, unpinned = campaign.build_formula(q)
    rebuilt, irow, ixrow, iselected, iselectors, iunpinned = independent.build_formula(_config(q))

    assert len(producer.key_of) - 1 == 1032
    assert unpinned == iunpinned == 19_931
    assert len(producer.clauses) == 19_944
    assert hashlib.sha256(producer.dimacs_bytes()).hexdigest() == REFERENCE_BASELINES[q]
    assert producer.var_of == rebuilt.var_of
    assert producer.clauses == rebuilt.clauses
    assert row == irow and xrow == ixrow
    assert selected == iselected and selectors == iselectors


def _empty_rows_and_factors() -> tuple[kernel.Rows, kernel.Factors]:
    return (
        {label: frozenset() for label in kernel.FACTORS},
        {label: frozenset() for label in kernel.FACTORS},
    )


@pytest.mark.parametrize(
    ("rows_by_label", "edges_by_label", "expected_kind"),
    [
        (
            {1: {0}, 2: {1}, 3: {3}},
            {1: kernel.edge(0, 13), 2: kernel.edge(1, 14), 3: kernel.edge(3, 15)},
            "Q0",
        ),
        (
            {1: {4}, 2: {1}, 3: {3}},
            {1: kernel.edge(0, 13), 2: kernel.edge(1, 14), 3: kernel.edge(3, 15)},
            "Q1_old_outside",
        ),
        (
            {1: {4}, 2: {1}, 3: {3}},
            {1: kernel.edge(0, 13), 2: kernel.edge(1, 2), 3: kernel.edge(3, 5)},
            "Q1_new_outside_only",
        ),
    ],
)
def test_endpoint_kinds_match_independent_semantics(
    rows_by_label: dict[int, set[int]],
    edges_by_label: dict[int, kernel.Edge],
    expected_kind: str,
) -> None:
    cnf, row_vars, _xrow, selected, _selectors, _metadata, _unpinned = campaign.build_formula(1)
    rows, factors = _empty_rows_and_factors()
    rows.update({label: frozenset(values) for label, values in rows_by_label.items()})
    factors.update({label: frozenset((current,)) for label, current in edges_by_label.items()})

    witnesses = campaign.retained_witnesses(rows, factors, cnf, row_vars, selected)[1]
    producer_counts = dict(Counter(witness["kind"] for witness in witnesses))
    independent_counts = independent.endpoint_counts(rows, factors, campaign.RETAINED)

    assert expected_kind in producer_counts
    assert producer_counts == independent_counts
    assert [tuple(witness["cut"]) for witness in witnesses] == sorted(
        tuple(witness["cut"]) for witness in witnesses
    )


def test_round_payload_rebuild_is_deterministic_and_rejects_wrong_identity() -> None:
    cnf, row_vars, _xrow, selected, _selectors, _metadata, _unpinned = campaign.build_formula(1)
    rows, factors = _empty_rows_and_factors()
    rows.update({1: frozenset((0,)), 2: frozenset((1,)), 3: frozenset((3,))})
    factors.update(
        {
            1: frozenset((kernel.edge(0, 13),)),
            2: frozenset((kernel.edge(1, 14),)),
            3: frozenset((kernel.edge(3, 15),)),
        }
    )
    witness = campaign.retained_witnesses(rows, factors, cnf, row_vars, selected)[1][0]
    before = len(cnf.clauses)
    record = campaign.compact_witness(witness)
    payload = {
        "input_digest": "a" * 64,
        "summary": {
            "clauses_before_cuts": before,
            "new_cuts": 1,
            "cut_types": {"Q0": 1},
            "clauses_after_cuts": before + 1,
        },
        "records": [record],
    }

    summary = campaign.apply_round_payload(cnf, row_vars, selected, payload, "a" * 64)
    assert summary["clauses_after_cuts"] == before + 1

    fresh, fresh_rows, _fresh_xrow, fresh_selected, *_ = campaign.build_formula(1)
    with pytest.raises(AssertionError):
        campaign.apply_round_payload(fresh, fresh_rows, fresh_selected, payload, "b" * 64)


def test_payload_first_interruption_reconciles_exactly_one_orphan(tmp_path: Path) -> None:
    cnf, row_vars, _xrow, selected, _selectors, _metadata, _unpinned = campaign.build_formula(1)
    rows, factors = _empty_rows_and_factors()
    rows.update({1: frozenset((0,)), 2: frozenset((1,)), 3: frozenset((3,))})
    factors.update(
        {
            1: frozenset((kernel.edge(0, 13),)),
            2: frozenset((kernel.edge(1, 14),)),
            3: frozenset((kernel.edge(3, 15),)),
        }
    )
    witness = campaign.retained_witnesses(rows, factors, cnf, row_vars, selected)[1][0]
    input_digest = "d" * 64
    before = len(cnf.clauses)
    summary = {
        "round": 0,
        "solver_status": "SAT",
        "solver_cnf_sha256": campaign.transient_cnf_digest(cnf),
        "clauses_before_cuts": before,
        "new_cuts": 1,
        "cut_types": {"Q0": 1},
        "clauses_after_cuts": before + 1,
    }
    payload = {
        "schema": "total-coloring.m6-protected-transfer-cut-round.v2",
        "input_digest": input_digest,
        "round": 0,
        "summary": summary,
        "records": [campaign.compact_witness(witness)],
    }
    run_dir = tmp_path / "run"
    cuts = run_dir / "cuts"
    cuts.mkdir(parents=True)
    campaign.atomic_gzip_json(cuts / "round-0000000.json.gz", payload)
    checkpoint_path = run_dir / "checkpoint.json"
    checkpoint: dict[str, object] = {
        "rounds": [],
        "completed_sat_rounds": 0,
        "cut_count": 0,
        "cut_types": {},
        "clauses_after_cuts": before,
        "status": "ready",
    }

    assert campaign.replay_checkpoint_cuts(
        cnf,
        row_vars,
        selected,
        checkpoint,
        run_dir=run_dir,
        checkpoint_path=checkpoint_path,
        input_digest=input_digest,
    )
    assert checkpoint["completed_sat_rounds"] == 1
    assert checkpoint["status"] == "running"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == checkpoint

    fresh, fresh_rows, _fresh_xrow, fresh_selected, *_ = campaign.build_formula(1)
    assert not campaign.replay_checkpoint_cuts(
        fresh,
        fresh_rows,
        fresh_selected,
        checkpoint,
        run_dir=run_dir,
        checkpoint_path=checkpoint_path,
        input_digest=input_digest,
    )

    (cuts / "round-0000002.json.gz").write_bytes((cuts / "round-0000000.json.gz").read_bytes())
    another, another_rows, _another_xrow, another_selected, *_ = campaign.build_formula(1)
    with pytest.raises(CampaignError, match="resumable orphan"):
        campaign.replay_checkpoint_cuts(
            another,
            another_rows,
            another_selected,
            checkpoint,
            run_dir=run_dir,
            checkpoint_path=checkpoint_path,
            input_digest=input_digest,
        )


def test_independent_checker_inherits_current_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker_path = Path(independent.__file__).resolve()
    checker = PinnedFile(checker_path, sha256_file(checker_path))
    observed: list[str | Path] = []

    def fake_run(
        command: tuple[str | Path, ...],
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        observed.extend(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps({"status": "PASS"}) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", ""), 0.0

    monkeypatch.setattr(campaign, "run_command", fake_run)
    result, _elapsed = campaign.independent_check(checker, tmp_path)

    assert result["status"] == "PASS"
    assert observed[0] == sys.executable


def _make_executable(path: Path, body: str) -> tuple[Path, str]:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path, sha256_file(path)


def _campaign_argv(tmp_path: Path, run_dir: Path, *, q: int = 1) -> list[str]:
    solver, solver_sha = _make_executable(
        tmp_path / "solver",
        'case " $* " in\n'
        '  *" --lrat "*) for last in "$@"; do :; done; printf \'proof\\n\' > "$last";;\n'
        "esac\n"
        "exit 20",
    )
    trim, trim_sha = _make_executable(tmp_path / "lrat-trim", "printf 's VERIFIED\\n'\nexit 20")
    check, check_sha = _make_executable(tmp_path / "lrat-check", "printf 'c VERIFIED\\n'\nexit 0")
    checker = Path(independent.__file__).resolve()
    helper = checker.with_name("independent_static.py")
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"runtime":"test"}\n', encoding="utf-8")
    return [
        "protected-transfer",
        "--q",
        str(q),
        "--run-dir",
        str(run_dir),
        "--max-total-rounds",
        "1",
        "--solver",
        str(solver),
        "--solver-sha256",
        solver_sha,
        "--independent-checker",
        str(checker),
        "--independent-checker-sha256",
        sha256_file(checker),
        "--independent-helper",
        str(helper),
        "--independent-helper-sha256",
        sha256_file(helper),
        "--lrat-trim",
        str(trim),
        "--lrat-trim-sha256",
        trim_sha,
        "--lrat-check",
        str(check),
        "--lrat-check-sha256",
        check_sha,
        "--runtime-config",
        str(runtime),
        "--runtime-config-sha256",
        sha256_file(runtime),
    ]


def test_sat_round_checkpoint_excludes_nondeterministic_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    witness_cnf, witness_rows, _xrow, witness_selected, *_ = campaign.build_formula(1)
    rows, factors = _empty_rows_and_factors()
    rows.update({1: frozenset((0,)), 2: frozenset((1,)), 3: frozenset((3,))})
    factors.update(
        {
            1: frozenset((kernel.edge(0, 13),)),
            2: frozenset((kernel.edge(1, 14),)),
            3: frozenset((kernel.edge(3, 15),)),
        }
    )
    witness = campaign.retained_witnesses(
        rows, factors, witness_cnf, witness_rows, witness_selected
    )[1][0]

    def fake_solve(
        cnf: kernel.CNF,
        _solver: object,
        cnf_path: Path,
    ) -> tuple[str, set[int], float, str]:
        data = cnf.dimacs_bytes()
        cnf_path.write_bytes(data)
        return "SAT", set(), 987.654321, hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(kernel, "solve", fake_solve)
    monkeypatch.setattr(kernel, "decode", lambda *_args: (rows, frozenset((0,)), factors))
    monkeypatch.setattr(kernel, "semantic_check", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(campaign, "validate_pins", lambda *_args: {})
    monkeypatch.setattr(
        campaign,
        "retained_witnesses",
        lambda *_args: ([witness], [witness]),
    )
    monkeypatch.setattr(sys, "argv", _campaign_argv(tmp_path, run_dir))

    assert campaign.main() == 0
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "limit"
    assert len(checkpoint["rounds"]) == 1
    assert "solver_seconds" not in checkpoint["rounds"][0]


@pytest.mark.parametrize("q", [1, 3])
def test_candidate_unsat_resume_and_independent_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, q: int
) -> None:
    run_dir = tmp_path / "run"
    argv = _campaign_argv(tmp_path, run_dir, q=q)
    monkeypatch.setattr(sys, "argv", argv)
    assert campaign.main() == 0
    checkpoint_path = run_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "candidate_unsat"
    assert not (run_dir / "proof-receipt.json").exists()

    first_checkpoint = checkpoint_path.read_bytes()
    monkeypatch.setattr(sys, "argv", argv)
    assert campaign.main() == 0
    assert checkpoint_path.read_bytes() == first_checkpoint

    output = tmp_path / "independent.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["check-protected-transfer", "--run-dir", str(run_dir), "--output", str(output)],
    )
    assert independent.main() == 0
    audit = json.loads(output.read_text(encoding="utf-8"))
    assert audit["status"] == "PASS"
    assert audit["run_status"] == "candidate_unsat"
    assert audit["terminal_exact_clause_set_equality"] is True

    monkeypatch.setattr(sys, "argv", [*argv, "--prove-on-candidate-unsat"])
    assert campaign.main() == 0
    proof = json.loads((run_dir / "proof-receipt.json").read_text(encoding="utf-8"))
    assert proof["status"] == "verified_unsat"
    assert proof["strict_lrat_trim_verified"] is True
    assert proof["separate_lrat_check_verified"] is True

    monkeypatch.setattr(sys, "argv", _campaign_argv(tmp_path, run_dir, q=3 if q == 1 else 1))
    with pytest.raises(CampaignError, match="run identity"):
        campaign.main()
    assert checkpoint_path.read_bytes() == first_checkpoint
