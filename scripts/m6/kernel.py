"""Deterministic variable-row kernel for the finite m=6 Type-I I.3+3 search.

The kernel keeps the six eligibility rows as SAT variables, uses the full 18
inactive-tail domain in the D=26 normalization, and selects only restrictions
of the six active factors to edges incident with the six-vertex core.

The endpoint exclusions are not pre-generated.  A decoded SAT model is
checked semantically, every exact canonical Q0 / Q1 witness in that model is
decoded (including old- and new-outside Q1), and sound clauses blocking those
concrete Boolean conjunctions can be added lazily. A solver SAT response is
accepted only after semantic decoding. A solver exhaustion response remains a
candidate negative until an independently reconstructed CNF and LRAT proof
have both been checked.
"""

from __future__ import annotations

import hashlib
import itertools
import subprocess
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeAlias

from total_coloring.external_tools import PinnedExecutable

C = frozenset(range(6))
T0 = frozenset((0, 1, 2))
T1 = frozenset((3, 4, 5))
SHORES = (T0, T1)
M = 6
FACTORS = tuple(range(M))
OWNERS = tuple(range(6, 12))
X = 12
INACTIVE = tuple(range(13, 31))
OUTSIDE = frozenset((*OWNERS, X, *INACTIVE))
V = frozenset(C | OUTSIDE)
D = 26


Edge: TypeAlias = tuple[int, int]
Clause: TypeAlias = tuple[int, ...]
RowVariables: TypeAlias = dict[tuple[int, int], int]
SelectedVariables: TypeAlias = dict[tuple[int, Edge], int]
Rows: TypeAlias = dict[int, frozenset[int]]
Factors: TypeAlias = dict[int, frozenset[Edge]]
SelectedAtom: TypeAlias = tuple[int, Edge]
RowAtom: TypeAlias = tuple[bool, int, int]
EndpointKind: TypeAlias = Literal["Q0", "Q1_old_outside", "Q1_new_outside_only"]
EndpointWitness: TypeAlias = dict[str, Any]


def edge(a: int, b: int) -> Edge:
    if a == b:
        raise ValueError("an edge must have distinct endpoints")
    return (a, b) if a < b else (b, a)


CORE_EDGES = tuple(itertools.combinations(sorted(C), 2))
CORE_OUTSIDE_EDGES = tuple(edge(h, t) for h in sorted(C) for t in sorted(OUTSIDE))
PHYSICAL_EDGES = tuple(sorted(CORE_EDGES + CORE_OUTSIDE_EDGES))


class CNF:
    """Stable variable allocation and canonical clause-set serialization."""

    def __init__(self) -> None:
        self.var_of: dict[object, int] = {}
        self.key_of: list[object | None] = [None]
        self.clauses: set[tuple[int, ...]] = set()

    def var(self, key: object) -> int:
        if key not in self.var_of:
            self.var_of[key] = len(self.key_of)
            self.key_of.append(key)
        return self.var_of[key]

    @staticmethod
    def normalize(literals: Iterable[int]) -> Clause | None:
        clause = tuple(sorted(set(literals), key=lambda z: (abs(z), z)))
        if any(-z in clause for z in clause):
            return None
        if not clause:
            raise AssertionError("empty clause")
        return clause

    def add(self, literals: Iterable[int]) -> bool:
        clause = self.normalize(literals)
        if clause is None:
            return False
        before = len(self.clauses)
        self.clauses.add(clause)
        return len(self.clauses) != before

    def at_most_one(self, variables: Iterable[int]) -> None:
        for a, b in itertools.combinations(sorted(set(variables)), 2):
            self.add((-a, -b))

    def exactly_one(self, variables: Iterable[int]) -> None:
        variables = tuple(sorted(set(variables)))
        assert variables
        self.add(variables)
        self.at_most_one(variables)

    def at_most_k(self, variables: Iterable[int], k: int) -> None:
        variables = tuple(sorted(set(variables)))
        assert 0 <= k <= len(variables)
        for chosen in itertools.combinations(variables, k + 1):
            self.add(tuple(-v for v in chosen))

    def at_least_k(self, variables: Iterable[int], k: int) -> None:
        variables = tuple(sorted(set(variables)))
        assert 0 <= k <= len(variables)
        for chosen in itertools.combinations(variables, len(variables) - k + 1):
            self.add(chosen)

    def ordered_clauses(self) -> list[Clause]:
        return sorted(self.clauses, key=lambda c: (len(c), c))

    def dimacs_bytes(self) -> bytes:
        """Return the exact canonical DIMACS representation."""

        clauses = self.ordered_clauses()
        lines = [f"p cnf {len(self.key_of) - 1} {len(clauses)}"]
        lines.extend(" ".join(map(str, clause)) + " 0" for clause in clauses)
        return ("\n".join(lines) + "\n").encode("ascii")

    def write(self, path: Path) -> str:
        data = self.dimacs_bytes()
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()


def owner(factor: int) -> int:
    return OWNERS[factor]


def build_base(
    *,
    noncage_mode: str = "require",
    min_deficiency: int = 0,
    max_deficiency: int = 4,
    exact_row_sizes: tuple[int, ...] | None = None,
    exact_rx_mask: int | None = None,
    anchor_encoding: str = "compact",
    inactive_first_use_break: bool = False,
) -> tuple[
    CNF,
    RowVariables,
    dict[int, int],
    SelectedVariables,
    dict[object, int],
    dict[str, object],
]:
    assert noncage_mode in {"require", "forbid", "free"}
    assert anchor_encoding in {"compact", "full_equivalence"}
    assert 0 <= min_deficiency <= max_deficiency <= 4
    if exact_row_sizes is not None:
        assert len(exact_row_sizes) == M
        assert all(1 <= value <= 4 for value in exact_row_sizes)
        exact_deficiency = sum(4 - value for value in exact_row_sizes)
        assert min_deficiency <= exact_deficiency <= max_deficiency
    if exact_rx_mask is not None:
        assert 0 <= exact_rx_mask < (1 << len(C))
        mask_deficiency = exact_rx_mask.bit_count()
        assert min_deficiency <= mask_deficiency <= max_deficiency
        if exact_row_sizes is not None:
            assert mask_deficiency == exact_deficiency
    cnf = CNF()
    row = {(f, h): cnf.var(("R", f, h)) for f in FACTORS for h in sorted(C)}
    xrow = {h: cnf.var(("RX", h)) for h in sorted(C)}

    # Every active row is nonempty and has size at most four.  Every core
    # column has size three or four.  RX(h) is exactly the size-three case,
    # so the active rows plus x's row give complement degree four at h.
    for f in FACTORS:
        variables = [row[f, h] for h in sorted(C)]
        cnf.at_least_k(variables, 1)
        cnf.at_most_k(variables, 4)
        if exact_row_sizes is not None:
            cnf.at_least_k(variables, exact_row_sizes[f])
            cnf.at_most_k(variables, exact_row_sizes[f])
    for h in sorted(C):
        variables = [row[f, h] for f in FACTORS]
        cnf.at_least_k(variables, 3)
        cnf.at_most_k(variables, 4)
        # RX -> column <= 3; not RX -> column >= 4.  With 3 <= column <= 4,
        # this is the exact equivalence RX(h) <-> column(h)=3.
        for four_chosen in itertools.combinations(variables, 4):
            cnf.add((-xrow[h], *(-v for v in four_chosen)))
        for three_chosen in itertools.combinations(variables, 3):
            cnf.add((xrow[h], *three_chosen))
    # The exposed-vertex complement row also has size at most four.  Because
    # RX is the set of size-three columns, this is the exact total-incidence
    # lower bound sum_f |R_f| >= 20.
    cnf.at_most_k(xrow.values(), 4)
    # In the exact column ledger, total active-row deficiency is |R_x|.
    cnf.at_least_k(xrow.values(), min_deficiency)
    cnf.at_most_k(xrow.values(), max_deficiency)
    if exact_rx_mask is not None:
        for h in sorted(C):
            cnf.add((xrow[h] if exact_rx_mask & (1 << h) else -xrow[h],))

    selected: dict[tuple[int, tuple[int, int]], int] = {}
    by_factor_core: dict[tuple[int, int], list[int]] = defaultdict(list)
    by_factor_outside: dict[tuple[int, int], list[int]] = defaultdict(list)
    by_physical: dict[tuple[int, int], list[int]] = defaultdict(list)

    for f in FACTORS:
        for current in PHYSICAL_EDGES:
            # The singleton factor f misses its own owner.
            if owner(f) in current:
                continue
            variable = cnf.var(("E", f, current))
            selected[f, current] = variable
            by_physical[current].append(variable)
            for v in current:
                if v in C:
                    by_factor_core[f, v].append(variable)
                else:
                    by_factor_outside[f, v].append(variable)

            core_ends = set(current) & C
            outside_ends = set(current) & OUTSIDE
            if outside_ends:
                assert len(core_ends) == len(outside_ends) == 1
                h = next(iter(core_ends))
                tail = next(iter(outside_ends))
                if tail in OWNERS:
                    g = OWNERS.index(tail)
                    # Graph law: h-owner(g) lies in L only off R_g.
                    cnf.add((-variable, -row[g, h]))
                elif tail == X:
                    # h-x lies in L only off R_x, i.e. in a size-four column.
                    cnf.add((-variable, -xrow[h]))

    if inactive_first_use_break:
        # S_18 first-use normal form.  Scan (factor,core-head) cells in
        # lexicographic order.  If synthetic inactive tail j occurs in a cell,
        # tail j-1 must already have occurred in an earlier cell.  Renaming
        # used inactive tails by their first occurrence puts every orbit into
        # this form, while unused tails remain irrelevant.
        cells = tuple((f, h) for f in FACTORS for h in sorted(C))
        for j in range(1, len(INACTIVE)):
            previous_tail = INACTIVE[j - 1]
            current_tail = INACTIVE[j]
            for position, (f, h) in enumerate(cells):
                current_var = selected[f, edge(h, current_tail)]
                earlier = [selected[g, edge(q, previous_tail)] for g, q in cells[:position]]
                cnf.add((-current_var, *earlier))

    # Each restriction is a matching saturating C.  Core exact coverage also
    # prevents two selected core-core edges sharing a core endpoint; the extra
    # outside constraints prevent repeated tails within one factor.
    for f in FACTORS:
        for h in sorted(C):
            cnf.exactly_one(by_factor_core[f, h])
        for tail in sorted(OUTSIDE):
            cnf.at_most_one(by_factor_outside[f, tail])

    # Different active factors cannot reuse one physical edge.
    for variables in by_physical.values():
        cnf.at_most_one(variables)

    anchor_witnesses: dict[object, int] = {}
    outside_edge_vars: dict[tuple[int, int], tuple[int, ...]] = {}
    for f in FACTORS:
        for h in sorted(C):
            outside_edge_vars[f, h] = tuple(
                selected[f, edge(h, tail)]
                for tail in sorted(OUTSIDE)
                if (f, edge(h, tail)) in selected
            )
            assert outside_edge_vars[f, h]

    if anchor_encoding == "full_equivalence":
        for (f, current), evar in sorted(selected.items()):
            core_ends = set(current) & C
            outside_ends = set(current) & OUTSIDE
            if len(core_ends) != 1 or len(outside_ends) != 1:
                continue
            h = next(iter(core_ends))
            tail = next(iter(outside_ends))
            avar = cnf.var(("A", f, h, tail))
            anchor_witnesses[f, h, tail] = avar
            cnf.add((-avar, evar))
            cnf.add((-avar, row[f, h]))
            cnf.add((-evar, -row[f, h], avar))
        if noncage_mode == "require":
            cnf.add(anchor_witnesses.values())
        elif noncage_mode == "forbid":
            for variable in anchor_witnesses.values():
                cnf.add((-variable,))
    else:
        # Production existential encoding: H(f,h) chooses an eligible head and
        # certifies that its factor uses some outside spoke there.  Reverse
        # implications are unnecessary for existence and would reintroduce
        # one auxiliary variable per physical outside spoke.
        for f in FACTORS:
            for h in sorted(C):
                hvar = cnf.var(("H", f, h))
                anchor_witnesses[f, h] = hvar
                cnf.add((-hvar, row[f, h]))
                cnf.add((-hvar, *outside_edge_vars[f, h]))
        if noncage_mode == "require":
            cnf.add(anchor_witnesses.values())
        elif noncage_mode == "forbid":
            for variable in anchor_witnesses.values():
                cnf.add((-variable,))
            # H is only a one-way witness.  Exact Cage additionally forbids
            # every actual selected eligible outside spoke.
            for f in FACTORS:
                for h in sorted(C):
                    for evar in outside_edge_vars[f, h]:
                        cnf.add((-evar, -row[f, h]))

    metadata = {
        "variables": len(cnf.key_of) - 1,
        "base_clauses": len(cnf.clauses),
        "row_variables": len(row),
        "x_row_variables": len(xrow),
        "selected_edge_variables": len(selected),
        "anchor_encoding": anchor_encoding,
        "anchor_variables": len(anchor_witnesses),
        "physical_edge_domain": len(PHYSICAL_EDGES),
        "noncage_mode": noncage_mode,
        "min_deficiency": min_deficiency,
        "max_deficiency": max_deficiency,
        "exact_row_sizes": list(exact_row_sizes) if exact_row_sizes is not None else None,
        "exact_rx_mask": exact_rx_mask,
        "inactive_first_use_break": inactive_first_use_break,
    }
    return cnf, row, xrow, selected, anchor_witnesses, metadata


def solve(
    cnf: CNF,
    solver: PinnedExecutable,
    cnf_path: Path,
) -> tuple[Literal["SAT", "UNSAT"], set[int], float, str]:
    """Run a hash-pinned DIMACS solver with the deterministic RC2 options."""

    digest = cnf.write(cnf_path)
    started = time.monotonic()
    proc = subprocess.run(
        [str(solver.verify()), "--quiet", "--seed=0", str(cnf_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    status: Literal["SAT", "UNSAT"]
    if proc.returncode == 10:
        status = "SAT"
    elif proc.returncode == 20:
        status = "UNSAT"
    else:
        raise RuntimeError(
            f"solver return {proc.returncode}: stdout={proc.stdout[-1000:]} "
            f"stderr={proc.stderr[-1000:]}"
        )
    true_vars: set[int] = set()
    if status == "SAT":
        for line in proc.stdout.splitlines():
            if line.startswith("v "):
                for token in line.split()[1:]:
                    value = int(token)
                    if value > 0:
                        true_vars.add(value)
        if not true_vars:
            raise RuntimeError("SAT status without model literals")
    return status, true_vars, elapsed, digest


def decode(
    true_vars: set[int],
    row_vars: Mapping[tuple[int, int], int],
    xrow_vars: Mapping[int, int],
    selected_vars: Mapping[tuple[int, Edge], int],
) -> tuple[Rows, frozenset[int], Factors]:
    rows = {f: frozenset(h for h in sorted(C) if row_vars[f, h] in true_vars) for f in FACTORS}
    rx = frozenset(h for h in sorted(C) if xrow_vars[h] in true_vars)
    factors = {
        f: frozenset(
            current
            for (g, current), variable in selected_vars.items()
            if g == f and variable in true_vars
        )
        for f in FACTORS
    }
    return rows, rx, factors


def inactive_circulant() -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    n = len(INACTIVE)
    for i, v in enumerate(INACTIVE):
        for delta in (1, 2):
            result.add(edge(v, INACTIVE[(i + delta) % n]))
            result.add(edge(v, INACTIVE[(i - delta) % n]))
    assert all(sum(v in e for e in result) == 4 for v in INACTIVE)
    return result


def complete_complement(
    rows: Mapping[int, frozenset[int]], rx: frozenset[int]
) -> tuple[set[Edge], set[Edge], tuple[Edge, Edge]]:
    """Construct the D=26 4-regular complement promised by normalization."""
    K: set[tuple[int, int]] = set()
    for f in FACTORS:
        K.update(edge(owner(f), h) for h in rows[f])
    K.update(edge(X, h) for h in rx)

    inactive_edges = inactive_circulant()
    deleted = (edge(INACTIVE[0], INACTIVE[1]), edge(INACTIVE[2], INACTIVE[3]))
    assert all(current in inactive_edges for current in deleted)
    assert len(set().union(*(set(current) for current in deleted))) == 4
    inactive_edges.difference_update(deleted)
    K.update(inactive_edges)

    residual_named: list[int] = []
    for f in FACTORS:
        residual_named.extend([owner(f)] * (4 - len(rows[f])))
    residual_named.extend([X] * (4 - len(rx)))
    # The exact m=6 identity gives four stubs, matching two deleted edges.
    assert len(residual_named) == 4
    endpoints = [INACTIVE[0], INACTIVE[1], INACTIVE[2], INACTIVE[3]]
    for named, tail in zip(residual_named, endpoints, strict=True):
        K.add(edge(named, tail))

    assert not any(set(current) <= C for current in K)
    assert all(sum(v in current for current in K) == 4 for v in V)
    complete = set(itertools.combinations(sorted(V), 2))
    L = complete - K
    assert all(sum(v in current for current in L) == D for v in V)
    return K, L, deleted


def selected_anchors(
    rows: Mapping[int, frozenset[int]], factors: Mapping[int, frozenset[Edge]]
) -> tuple[tuple[int, Edge, int, int], ...]:
    anchors: list[tuple[int, Edge, int, int]] = []
    for f in FACTORS:
        for current in factors[f]:
            core = set(current) & C
            outside = set(current) & OUTSIDE
            if len(core) == len(outside) == 1:
                h = next(iter(core))
                tail = next(iter(outside))
                if h in rows[f]:
                    anchors.append((f, current, h, tail))
    return tuple(sorted(anchors))


def semantic_check(
    rows: Mapping[int, frozenset[int]],
    rx: frozenset[int],
    factors: Mapping[int, frozenset[Edge]],
    *,
    require_noncage: bool,
) -> dict[str, object]:
    assert set(rows) == set(FACTORS)
    assert set(factors) == set(FACTORS)
    assert all(1 <= len(rows[f]) <= 4 for f in FACTORS)
    columns = {h: sum(h in rows[f] for f in FACTORS) for h in C}
    assert all(columns[h] in (3, 4) for h in C)
    assert rx == frozenset(h for h in C if columns[h] == 3)
    assert sum(map(len, rows.values())) in range(20, 25)
    deficiency = sum(4 - len(rows[f]) for f in FACTORS)
    assert deficiency == len(rx) <= 4

    K, L, deleted = complete_complement(rows, rx)
    for f in FACTORS:
        current = factors[f]
        assert all(e in PHYSICAL_EDGES for e in current)
        assert all(owner(f) not in e for e in current)
        assert all(e in L for e in current)
        assert all(sum(h in e for e in current) == 1 for h in C)
        assert all(sum(t in e for e in current) <= 1 for t in OUTSIDE)
    for f, g in itertools.combinations(FACTORS, 2):
        assert not (factors[f] & factors[g])

    anchors = selected_anchors(rows, factors)
    if require_noncage:
        assert anchors
    return {
        "row_sizes": [len(rows[f]) for f in FACTORS],
        "column_sizes": [columns[h] for h in sorted(C)],
        "active_incidence": sum(map(len, rows.values())),
        "deficiency": deficiency,
        "x_row_size": len(rx),
        "x_row": sorted(rx),
        "factor_edge_counts": [len(factors[f]) for f in FACTORS],
        "eligible_outside_anchors": len(anchors),
        "complement_edges": len(K),
        "L_edges": len(L),
        "normalization_deleted_edges": [list(e) for e in deleted],
    }


ORIENTATION_CACHE: dict[Edge, tuple[tuple[int, int], ...]] = {
    current: tuple((h, next(iter(set(current) - {h}))) for h in sorted(set(current) & C))
    for current in PHYSICAL_EDGES
}


HEAD_GEOMETRY: dict[frozenset[int], tuple[tuple[int, frozenset[int]], ...]] = {}
for _heads in itertools.combinations(sorted(C), 3):
    _remaining = C - set(_heads)
    HEAD_GEOMETRY[frozenset(_heads)] = tuple(
        (spare, frozenset(_remaining - {spare}))
        for spare in sorted(_remaining)
        if len((_remaining - {spare}) & T0) == 1 and len((_remaining - {spare}) & T1) == 1
    )


def orientations(current: Edge) -> tuple[tuple[int, int], ...]:
    return ORIENTATION_CACHE[current]


def three_edges_disjoint(first: Edge, second: Edge, third: Edge) -> bool:
    return len(set(first + second + third)) == 6


def symbolic_witness_key(
    selected_atoms: Iterable[SelectedAtom], row_atoms: Iterable[RowAtom]
) -> tuple[tuple[SelectedAtom, ...], tuple[RowAtom, ...]]:
    return (
        tuple(sorted(selected_atoms)),
        tuple(sorted(set(row_atoms))),
    )


def witness_cut(
    cnf: CNF,
    row_vars: Mapping[tuple[int, int], int],
    selected_vars: Mapping[tuple[int, Edge], int],
    selected_atoms: Iterable[SelectedAtom],
    row_atoms: Iterable[RowAtom],
) -> Clause:
    del cnf  # The parameter preserves the audited producer/checker call shape.
    literals: list[int] = []
    for factor, current in selected_atoms:
        literals.append(-selected_vars[factor, current])
    for truth, factor, h in row_atoms:
        variable = row_vars[factor, h]
        literals.append(-variable if truth else variable)
    clause = CNF.normalize(literals)
    assert clause is not None
    return clause


def endpoint_witnesses(
    rows: Mapping[int, frozenset[int]],
    factors: Mapping[int, frozenset[Edge]],
    cnf: CNF,
    row_vars: Mapping[tuple[int, int], int],
    selected_vars: Mapping[tuple[int, Edge], int],
) -> list[EndpointWitness]:
    """Decode the full canonical Q0/Q1 endpoint, without choosing an anchor.

    Three pairwise-disjoint selected edges of three distinct active factors
    are oriented at the three heads C - (U union {a}), where U is a cross-pair.
    Their tails need only be tested against a: edge-disjointness already keeps
    them away from all three heads.  Q1 is classified as old-outside when an
    eligible donor has an outside tail, and otherwise as new-outside-only.
    """
    witnesses: list[EndpointWitness] = []
    seen: set[Clause] = set()
    seen_symbolic: set[tuple[tuple[SelectedAtom, ...], tuple[RowAtom, ...]]] = set()
    factor_edges = {f: tuple(sorted(factors[f])) for f in FACTORS}
    for labels in itertools.combinations(FACTORS, 3):
        for chosen_edges in itertools.product(*(factor_edges[f] for f in labels)):
            if not three_edges_disjoint(*chosen_edges):
                continue
            for oriented in itertools.product(*(orientations(e) for e in chosen_edges)):
                heads = tuple(item[0] for item in oriented)
                tails = tuple(item[1] for item in oriented)
                if len(set(heads)) != 3:
                    continue
                geometry = HEAD_GEOMETRY[frozenset(heads)]
                for spare, cross_pair in geometry:
                    if spare in tails:
                        continue
                    # The only available core tails are the two vertices of U;
                    # three distinct tails therefore force an outside donor.
                    assert any(tail in OUTSIDE for tail in tails)
                    selected_atoms = tuple(zip(labels, chosen_edges, strict=True))
                    eligibility = tuple(heads[i] in rows[labels[i]] for i in range(3))
                    activated: int | None = None
                    kind: EndpointKind
                    if all(eligibility):
                        kind = "Q0"
                        row_atoms: tuple[RowAtom, ...] = tuple(
                            (True, labels[i], heads[i]) for i in range(3)
                        )
                    elif eligibility.count(False) == 1:
                        activated_index = eligibility.index(False)
                        activated = labels[activated_index]
                        if spare not in rows[activated]:
                            continue
                        if owner(activated) in tails:
                            continue
                        old_outside = any(eligibility[i] and tails[i] in OUTSIDE for i in range(3))
                        kind = "Q1_old_outside" if old_outside else "Q1_new_outside_only"
                        if not old_outside:
                            assert tails[activated_index] in OUTSIDE
                        row_atoms = (
                            *tuple((eligibility[i], labels[i], heads[i]) for i in range(3)),
                            (True, activated, spare),
                        )
                    else:
                        continue
                    symbolic = symbolic_witness_key(selected_atoms, row_atoms)
                    if symbolic in seen_symbolic:
                        continue
                    seen_symbolic.add(symbolic)
                    clause = witness_cut(
                        cnf,
                        row_vars,
                        selected_vars,
                        selected_atoms,
                        row_atoms,
                    )
                    if clause in seen:
                        continue
                    seen.add(clause)
                    witnesses.append(
                        {
                            "kind": kind,
                            "donors": [
                                [labels[i], list(chosen_edges[i]), heads[i], tails[i]]
                                for i in range(3)
                            ],
                            "spare": spare,
                            "cross_pair": sorted(cross_pair),
                            "activated": activated,
                            "selected_atoms": [[f, list(current)] for f, current in selected_atoms],
                            "row_atoms": [list(atom) for atom in row_atoms],
                            "cut": list(clause),
                        }
                    )
    return witnesses


def anchored_old_cut_set(
    rows: Mapping[int, frozenset[int]],
    factors: Mapping[int, frozenset[Edge]],
    cnf: CNF,
    row_vars: Mapping[tuple[int, int], int],
    selected_vars: Mapping[tuple[int, Edge], int],
) -> set[Clause]:
    """Independent anchor-first reconstruction of Q0 + old-outside Q1 cuts.

    This retains the earlier branch-specific organization solely as a
    cross-check.  Its cut set must equal the Q0/old subset produced by the
    full canonical decoder above on every decoded model.
    """
    cuts: set[Clause] = set()
    seen_symbolic: set[tuple[tuple[SelectedAtom, ...], tuple[RowAtom, ...]]] = set()
    factor_edges = {f: tuple(sorted(factors[f])) for f in FACTORS}
    for c, anchor_edge, b, anchor_tail in selected_anchors(rows, factors):
        for d, f in itertools.combinations(tuple(g for g in FACTORS if g != c), 2):
            for first_edge in factor_edges[d]:
                for second_edge in factor_edges[f]:
                    if not three_edges_disjoint(anchor_edge, first_edge, second_edge):
                        continue
                    for h, first_tail in orientations(first_edge):
                        for k, second_tail in orientations(second_edge):
                            if len({b, h, k}) != 3:
                                continue
                            geometry = HEAD_GEOMETRY[frozenset((b, h, k))]
                            for spare, _cross_pair in geometry:
                                if spare in {anchor_tail, first_tail, second_tail}:
                                    continue
                                selected_atoms = (
                                    (c, anchor_edge),
                                    (d, first_edge),
                                    (f, second_edge),
                                )
                                first_good = h in rows[d]
                                second_good = k in rows[f]
                                if first_good and second_good:
                                    row_atoms: tuple[RowAtom, ...] = (
                                        (True, c, b),
                                        (True, d, h),
                                        (True, f, k),
                                    )
                                elif first_good != second_good:
                                    activated = f if first_good else d
                                    if spare not in rows[activated]:
                                        continue
                                    if owner(activated) in {
                                        anchor_tail,
                                        first_tail,
                                        second_tail,
                                    }:
                                        continue
                                    row_atoms = (
                                        (True, c, b),
                                        (first_good, d, h),
                                        (second_good, f, k),
                                        (True, activated, spare),
                                    )
                                else:
                                    continue
                                symbolic = symbolic_witness_key(selected_atoms, row_atoms)
                                if symbolic in seen_symbolic:
                                    continue
                                seen_symbolic.add(symbolic)
                                cuts.add(
                                    witness_cut(
                                        cnf,
                                        row_vars,
                                        selected_vars,
                                        selected_atoms,
                                        row_atoms,
                                    )
                                )
    return cuts


def endpoint_crosscheck(
    rows: Mapping[int, frozenset[int]],
    factors: Mapping[int, frozenset[Edge]],
    witnesses: Iterable[EndpointWitness],
    cnf: CNF,
    row_vars: Mapping[tuple[int, int], int],
    selected_vars: Mapping[tuple[int, Edge], int],
) -> dict[str, object]:
    canonical_old = {
        tuple(witness["cut"])
        for witness in witnesses
        if witness["kind"] in {"Q0", "Q1_old_outside"}
    }
    anchored_old = anchored_old_cut_set(rows, factors, cnf, row_vars, selected_vars)
    assert canonical_old == anchored_old
    return {
        "canonical_q0_or_old_cut_count": len(canonical_old),
        "anchor_first_q0_or_old_cut_count": len(anchored_old),
        "exact_cut_set_equality": True,
    }


def serialize_model(
    rows: Mapping[int, frozenset[int]],
    rx: frozenset[int],
    factors: Mapping[int, frozenset[Edge]],
) -> dict[str, object]:
    return {
        "rows": {str(f): sorted(rows[f]) for f in FACTORS},
        "x_row": sorted(rx),
        "factors": {str(f): [list(current) for current in sorted(factors[f])] for f in FACTORS},
        "anchors": [
            [f, list(current), h, tail] for f, current, h, tail in selected_anchors(rows, factors)
        ],
    }
