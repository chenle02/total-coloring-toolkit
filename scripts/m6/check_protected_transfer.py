"""Independent reconstruction and semantic audit for protected-transfer runs.

The producer kernel and campaign module are never imported.  This checker
reconstructs the compact CNF, validates every retained-only endpoint cut,
requires exact DIMACS clause-set equality, and independently audits a terminal
SAT_NO_RETAINED_ENDPOINT model when present.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import itertools
import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__:
    from scripts.m6.independent_static import (
        FACTORS,
        OUTSIDE,
        OWNERS,
        PHYSICAL_EDGES,
        T0,
        T1,
        C,
        IndependentCNF,
        X,
        edge,
        orientations,
        owner,
        parse_dimacs,
        sha256,
    )
else:
    from independent_static import (  # type: ignore[import-not-found, no-redef]
        FACTORS,
        OUTSIDE,
        OWNERS,
        PHYSICAL_EDGES,
        T0,
        T1,
        C,
        IndependentCNF,
        X,
        edge,
        orientations,
        owner,
        parse_dimacs,
        sha256,
    )


S_LABEL = 0
A = 0
R_LABEL = 1
RETAINED = tuple(label for label in FACTORS if label != S_LABEL)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def atomic_json(path: Path, value: object) -> None:
    data = (canonical_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def build_formula(
    config: Mapping[str, Any],
) -> tuple[
    IndependentCNF,
    dict[tuple[int, int], int],
    dict[int, int],
    dict[tuple[int, tuple[int, int]], int],
    dict[tuple[int, int], int],
    int,
]:
    assert config["schema"] == "total-coloring.m6-protected-transfer-config.v2"
    assert config["s_label"] == S_LABEL
    assert config["a_vertex"] == A
    assert config["r_label"] == R_LABEL
    q = int(config["q_vertex"])
    row_sizes = tuple(int(value) for value in config["row_sizes"])
    assert row_sizes == (3, 4, 4, 4, 4, 4)
    assert config["rx_mask"] == 1
    assert config["anchor_encoding"] == "compact"
    assert config["inactive_first_use_break"] is False

    cnf = IndependentCNF()
    row = {(f, h): cnf.var(("R", f, h)) for f in FACTORS for h in sorted(C)}
    xrow = {h: cnf.var(("RX", h)) for h in sorted(C)}
    for f in FACTORS:
        variables = [row[f, h] for h in sorted(C)]
        cnf.at_least_k(variables, 1)
        cnf.at_most_k(variables, 4)
        cnf.at_least_k(variables, row_sizes[f])
        cnf.at_most_k(variables, row_sizes[f])
    for h in sorted(C):
        variables = [row[f, h] for f in FACTORS]
        cnf.at_least_k(variables, 3)
        cnf.at_most_k(variables, 4)
        for four_chosen in itertools.combinations(variables, 4):
            cnf.add((-xrow[h], *(-variable for variable in four_chosen)))
        for three_chosen in itertools.combinations(variables, 3):
            cnf.add((xrow[h], *three_chosen))
    cnf.at_most_k(xrow.values(), 4)
    cnf.at_least_k(xrow.values(), 1)
    cnf.at_most_k(xrow.values(), 1)
    for h in sorted(C):
        cnf.add((xrow[h] if h == A else -xrow[h],))

    selected: dict[tuple[int, tuple[int, int]], int] = {}
    by_factor_core: dict[tuple[int, int], list[int]] = {(f, h): [] for f in FACTORS for h in C}
    by_factor_outside: dict[tuple[int, int], list[int]] = {
        (f, tail): [] for f in FACTORS for tail in OUTSIDE
    }
    by_physical: dict[tuple[int, int], list[int]] = {current: [] for current in PHYSICAL_EDGES}
    for f in FACTORS:
        for current in PHYSICAL_EDGES:
            if owner(f) in current:
                continue
            variable = cnf.var(("E", f, current))
            selected[f, current] = variable
            by_physical[current].append(variable)
            for vertex in current:
                if vertex in C:
                    by_factor_core[f, vertex].append(variable)
                else:
                    by_factor_outside[f, vertex].append(variable)
            core = set(current) & C
            outside = set(current) & OUTSIDE
            if outside:
                assert len(core) == len(outside) == 1
                head = next(iter(core))
                tail = next(iter(outside))
                if tail in OWNERS:
                    cnf.add((-variable, -row[OWNERS.index(tail), head]))
                elif tail == X:
                    cnf.add((-variable, -xrow[head]))
    for f in FACTORS:
        for h in sorted(C):
            cnf.exactly_one(by_factor_core[f, h])
        for tail in sorted(OUTSIDE):
            cnf.at_most_one(by_factor_outside[f, tail])
    for variables in by_physical.values():
        cnf.at_most_one(variables)

    selectors: dict[tuple[int, int], int] = {}
    for f in FACTORS:
        for h in sorted(C):
            variable = cnf.var(("H", f, h))
            selectors[f, h] = variable
            outside_variables = [
                selected[f, edge(h, tail)]
                for tail in sorted(OUTSIDE)
                if (f, edge(h, tail)) in selected
            ]
            cnf.add((-variable, row[f, h]))
            cnf.add((-variable, *outside_variables))

    unpinned_clauses = len(cnf.clauses)
    cnf.add((-row[S_LABEL, q],))
    cnf.add((-row[R_LABEL, A],))
    cnf.add((row[R_LABEL, q],))
    for label in RETAINED:
        current = edge(q, owner(S_LABEL))
        if (label, current) in selected:
            cnf.add((-selected[label, current],))
    for label in RETAINED:
        current = edge(A, owner(R_LABEL))
        if (label, current) in selected:
            cnf.add((-selected[label, current],))
    allowed_anchors = [
        variable
        for (label, head), variable in selectors.items()
        if label in RETAINED and not (label == R_LABEL and head == q)
    ]
    cnf.add(allowed_anchors)
    return cnf, row, xrow, selected, selectors, unpinned_clauses


def load_cut_payload(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("cut payload must be a JSON object")
    return value


def validate_cut_record(
    record: Mapping[str, Any],
    row: Mapping[tuple[int, int], int],
    selected: Mapping[tuple[int, tuple[int, int]], int],
) -> tuple[tuple[int, ...], str]:
    assert set(record) == {"kind", "donors", "spare", "cross_pair", "activated"}
    kind = record["kind"]
    assert kind in {"Q0", "Q1_old_outside", "Q1_new_outside_only"}
    donors = record["donors"]
    assert len(donors) == 3
    labels = tuple(item[0] for item in donors)
    edges = tuple(tuple(item[1]) for item in donors)
    heads = tuple(item[2] for item in donors)
    tails = tuple(item[3] for item in donors)
    assert labels == tuple(sorted(labels))
    assert all(label in RETAINED for label in labels)
    assert all((labels[index], edges[index]) in selected for index in range(3))
    assert all(edge(heads[index], tails[index]) == edges[index] for index in range(3))
    assert all(head in C for head in heads)
    assert len(set(heads)) == 3
    assert all(
        not (set(edges[left]) & set(edges[right]))
        for left, right in itertools.combinations(range(3), 2)
    )
    spare = record["spare"]
    cross_pair = C - set(heads) - {spare}
    assert sorted(cross_pair) == record["cross_pair"]
    assert len(cross_pair & T0) == len(cross_pair & T1) == 1
    assert spare not in tails
    assert any(tail in OUTSIDE for tail in tails)
    if kind == "Q0":
        assert record["activated"] is None
        row_atoms = tuple((True, labels[index], heads[index]) for index in range(3))
    else:
        activated = record["activated"]
        positions = [index for index, label in enumerate(labels) if label == activated]
        assert len(positions) == 1
        activated_index = positions[0]
        row_atoms = (
            *tuple((index != activated_index, labels[index], heads[index]) for index in range(3)),
            (True, activated, spare),
        )
        assert owner(activated) not in tails
        old = any(index != activated_index and tails[index] in OUTSIDE for index in range(3))
        assert kind == ("Q1_old_outside" if old else "Q1_new_outside_only")
        if not old:
            assert tails[activated_index] in OUTSIDE
    literals = [-selected[labels[index], edges[index]] for index in range(3)]
    for truth, label, head in row_atoms:
        literals.append(-row[label, head] if truth else row[label, head])
    clause = IndependentCNF.normalize(literals)
    assert clause is not None
    return clause, kind


def endpoint_counts(
    rows: Mapping[int, frozenset[int]],
    factors: Mapping[int, frozenset[tuple[int, int]]],
    labels: tuple[int, ...],
) -> dict[str, int]:
    found: dict[object, str] = {}
    for donor_labels in itertools.combinations(labels, 3):
        for chosen in itertools.product(*(sorted(factors[label]) for label in donor_labels)):
            if any(
                set(chosen[left]) & set(chosen[right])
                for left, right in itertools.combinations(range(3), 2)
            ):
                continue
            for oriented in itertools.product(*(orientations(current) for current in chosen)):
                heads = tuple(item[0] for item in oriented)
                tails = tuple(item[1] for item in oriented)
                if len(set(heads)) != 3:
                    continue
                remaining = C - set(heads)
                for spare in sorted(remaining):
                    cross_pair = remaining - {spare}
                    if len(cross_pair & T0) != 1 or len(cross_pair & T1) != 1:
                        continue
                    if spare in tails:
                        continue
                    assert any(tail in OUTSIDE for tail in tails)
                    eligible = tuple(
                        heads[index] in rows[donor_labels[index]] for index in range(3)
                    )
                    if all(eligible):
                        kind = "Q0"
                        atoms = tuple(
                            (True, donor_labels[index], heads[index]) for index in range(3)
                        )
                    elif eligible.count(False) == 1:
                        activated_index = eligible.index(False)
                        activated = donor_labels[activated_index]
                        if spare not in rows[activated] or owner(activated) in tails:
                            continue
                        old = any(eligible[index] and tails[index] in OUTSIDE for index in range(3))
                        kind = "Q1_old_outside" if old else "Q1_new_outside_only"
                        if not old:
                            assert tails[activated_index] in OUTSIDE
                        atoms = (
                            *tuple(
                                (eligible[index], donor_labels[index], heads[index])
                                for index in range(3)
                            ),
                            (True, activated, spare),
                        )
                    else:
                        continue
                    key = (
                        tuple(sorted((donor_labels[index], chosen[index]) for index in range(3))),
                        tuple(sorted(set(atoms))),
                    )
                    previous = found.setdefault(key, kind)
                    assert previous == kind
    return dict(sorted(Counter(found.values()).items()))


def validate_model(
    model_path: Path,
    config: Mapping[str, Any],
    cnf: IndependentCNF,
    row: Mapping[tuple[int, int], int],
    xrow: Mapping[int, int],
    selected: Mapping[tuple[int, tuple[int, int]], int],
    selectors: Mapping[tuple[int, int], int],
) -> dict[str, object]:
    payload = json.loads(model_path.read_text(encoding="utf-8"))
    model = payload["model"]
    rows = {int(label): frozenset(values) for label, values in model["rows"].items()}
    rx = frozenset(model["x_row"])
    factors = {
        int(label): frozenset(tuple(current) for current in values)
        for label, values in model["factors"].items()
    }
    assert set(rows) == set(FACTORS) == set(factors)
    assert [len(rows[label]) for label in FACTORS] == config["row_sizes"]
    columns = {head: sum(head in rows[label] for label in FACTORS) for head in C}
    assert rx == {A}
    assert columns == {0: 3, 1: 4, 2: 4, 3: 4, 4: 4, 5: 4}
    q = int(config["q_vertex"])
    assert q not in rows[S_LABEL]
    assert A not in rows[R_LABEL] and q in rows[R_LABEL]

    for label in FACTORS:
        assert all(owner(label) not in current for current in factors[label])
        assert all(sum(head in current for current in factors[label]) == 1 for head in C)
        assert all(sum(tail in current for current in factors[label]) <= 1 for tail in OUTSIDE)
        for current in factors[label]:
            core = set(current) & C
            outside = set(current) & OUTSIDE
            assert core
            if outside:
                assert len(core) == len(outside) == 1
                head = next(iter(core))
                tail = next(iter(outside))
                if tail in OWNERS:
                    assert head not in rows[OWNERS.index(tail)]
                elif tail == X:
                    assert head not in rx
    for left, right in itertools.combinations(FACTORS, 2):
        assert not (factors[left] & factors[right])

    assert all(edge(q, owner(S_LABEL)) not in factors[label] for label in RETAINED)
    assert all(edge(A, owner(R_LABEL)) not in factors[label] for label in RETAINED)
    anchors = []
    for label in RETAINED:
        for current in factors[label]:
            core = set(current) & C
            outside = set(current) & OUTSIDE
            if len(core) == len(outside) == 1:
                head = next(iter(core))
                tail = next(iter(outside))
                if head in rows[label] and not (label == R_LABEL and head == q):
                    anchors.append((label, head, tail))
    assert anchors

    retained_counts = endpoint_counts(rows, factors, RETAINED)
    all_counts = endpoint_counts(rows, factors, FACTORS)
    assert retained_counts == {}
    assert retained_counts == payload["retained_endpoint_types"]
    assert all_counts == payload["all_label_endpoint_types"]

    true_variables: set[int] = set()
    true_variables.update(row[label, head] for label in FACTORS for head in rows[label])
    true_variables.update(xrow[head] for head in rx)
    true_variables.update(
        selected[label, current] for label in FACTORS for current in factors[label]
    )
    chosen_anchor = min(anchors)
    true_variables.add(selectors[chosen_anchor[0], chosen_anchor[1]])
    assert all(
        any(
            (literal > 0 and literal in true_variables)
            or (literal < 0 and -literal not in true_variables)
            for literal in clause
        )
        for clause in cnf.clauses
    )
    return {
        "status": "PASS",
        "row_sizes": [len(rows[label]) for label in FACTORS],
        "columns": [columns[head] for head in sorted(C)],
        "x_row": sorted(rx),
        "factor_edge_counts": [len(factors[label]) for label in FACTORS],
        "safe_transfer_pins_verified": True,
        "allowed_retained_anchor": list(chosen_anchor),
        "retained_endpoint_types": retained_counts,
        "all_label_endpoint_types": all_counts,
        "cnf_assignment_verified": True,
        "model_sha256": sha256(model_path),
    }


def main() -> int:
    if not __debug__:
        raise RuntimeError("the independent checker must not run with Python optimization enabled")
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    checkpoint_path = run_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert isinstance(checkpoint, dict)
    assert checkpoint["schema"] == "total-coloring.m6-protected-transfer-checkpoint.v2"
    config = checkpoint["config"]
    provenance = checkpoint["provenance"]
    digest = hashlib.sha256(
        canonical_json({"config": config, "provenance": provenance}).encode()
    ).hexdigest()
    assert digest == checkpoint["input_digest"]

    checker_identity = provenance["independent_checker"]
    helper_identity = provenance["independent_helper"]
    assert checker_identity == {"name": Path(__file__).name, "sha256": sha256(Path(__file__))}
    helper_path = Path(__file__).with_name("independent_static.py")
    assert helper_identity == {"name": helper_path.name, "sha256": sha256(helper_path)}
    runtime_python = Path(sys.executable).resolve(strict=True)
    assert provenance["python_runtime"] == {
        "name": runtime_python.name,
        "sha256": sha256(runtime_python),
    }

    cnf, row, xrow, selected, selectors, unpinned = build_formula(config)
    assert len(cnf.key_of) - 1 == checkpoint["variables"]
    assert unpinned == checkpoint["unpinned_base_clauses"]
    assert len(cnf.clauses) == checkpoint["pinned_baseline_clauses"]
    baseline_path = run_dir / "baseline.cnf"
    header, clauses = parse_dimacs(baseline_path)
    assert header[0] == len(cnf.key_of) - 1
    assert clauses == cnf.clauses
    assert sha256(baseline_path) == checkpoint["baseline_cnf_sha256"]

    cut_set: set[tuple[int, ...]] = set()
    kinds: Counter[str] = Counter()
    count = 0
    referenced: set[str] = set()
    for expected_round, summary in enumerate(checkpoint["rounds"]):
        assert summary["round"] == expected_round
        assert summary["cut_file"] == f"cuts/round-{expected_round:07d}.json.gz"
        referenced.add(summary["cut_file"])
        cut_path = run_dir / summary["cut_file"]
        assert sha256(cut_path) == summary["cut_file_sha256"]
        payload = load_cut_payload(cut_path)
        assert payload["schema"] == "total-coloring.m6-protected-transfer-cut-round.v2"
        assert payload["input_digest"] == checkpoint["input_digest"]
        assert payload["round"] == summary["round"]
        payload_summary = payload["summary"]
        assert payload_summary == {key: summary[key] for key in payload_summary}
        assert payload_summary["clauses_before_cuts"] == len(cnf.clauses) + len(cut_set)
        current_clauses = cnf.clauses | cut_set
        ordered = sorted(current_clauses, key=lambda clause: (len(clause), clause))
        lines = [f"p cnf {len(cnf.key_of) - 1} {len(ordered)}"]
        lines.extend(" ".join(map(str, clause)) + " 0" for clause in ordered)
        current_digest = hashlib.sha256(("\n".join(lines) + "\n").encode("ascii")).hexdigest()
        assert payload_summary["solver_cnf_sha256"] == current_digest
        assert len(payload["records"]) == summary["new_cuts"]
        for record in payload["records"]:
            clause, kind = validate_cut_record(record, row, selected)
            assert clause not in cut_set and clause not in cnf.clauses
            cut_set.add(clause)
            kinds[kind] += 1
        assert payload_summary["clauses_after_cuts"] == len(cnf.clauses) + len(cut_set)
        count += len(payload["records"])
    available = {
        str(path.relative_to(run_dir)) for path in (run_dir / "cuts").glob("round-*.json.gz")
    }
    assert available == referenced
    assert count == checkpoint["cut_count"]
    assert dict(sorted(kinds.items())) == checkpoint["cut_types"]
    cnf.clauses.update(cut_set)
    assert len(cnf.clauses) == checkpoint["clauses_after_cuts"]

    terminal_cnf = run_dir / "terminal.cnf"
    terminal_exact = None
    if terminal_cnf.exists():
        header, clauses = parse_dimacs(terminal_cnf)
        assert header[0] == len(cnf.key_of) - 1
        assert clauses == cnf.clauses
        assert sha256(terminal_cnf) == checkpoint["terminal_cnf_sha256"]
        terminal_exact = True

    model_check = None
    if checkpoint["status"] in {
        "candidate_sat_no_retained_endpoint",
        "verified_sat_no_retained_endpoint",
    }:
        model_path = run_dir / "endpoint-negative-model.json"
        assert sha256(model_path) == checkpoint["endpoint_negative_model_sha256"]
        model_check = validate_model(model_path, config, cnf, row, xrow, selected, selectors)
        assert terminal_exact is True
    elif checkpoint["status"] == "candidate_unsat":
        assert terminal_exact is True

    result = {
        "schema": "total-coloring.m6-protected-transfer-independent-check.v2",
        "status": "PASS",
        "run_status": checkpoint["status"],
        "input_digest": checkpoint["input_digest"],
        "config": config,
        "variables": len(cnf.key_of) - 1,
        "unpinned_base_clauses": unpinned,
        "pinned_baseline_clauses": checkpoint["pinned_baseline_clauses"],
        "validated_cut_rounds": len(checkpoint["rounds"]),
        "validated_lazy_cuts": len(cut_set),
        "cut_types": dict(sorted(kinds.items())),
        "clauses_after_cuts": len(cnf.clauses),
        "baseline_exact_clause_set_equality": True,
        "terminal_exact_clause_set_equality": terminal_exact,
        "terminal_model_check": model_check,
        "baseline_cnf_sha256": sha256(baseline_path),
        "checkpoint_sha256": sha256(checkpoint_path),
    }
    atomic_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
