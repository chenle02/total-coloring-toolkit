# Exact `D = 8` dependency root-pivot auditor

`auditors/d8_dependency_audit.cpp` is the production implementation of a
small, exhaustive audit used by the total-coloring proof program. It is C++20,
not C: the implementation uses standard-library bit operations, containers,
and string views, but has no project or third-party runtime dependencies.

The auditor answers a deliberately narrow finite question. For each built-in
role-labelled dependency profile, it enumerates every loopless source-set
assignment, applies the exact rooted-incidence constraints, and computes the
minimum number of legal oriented-hole root pivots needed to expose a robust
mobile triple. It does **not** decide whether a dependency state is realized by
a graph coloring, and it does not prove the open `D = 8` coloring statement.

## Reproducible build and run

Any conforming C++20 compiler is sufficient. The publication gate uses a
strict warning build; the following is the supported local command:

```bash
mkdir -p build
c++ -std=c++20 -O2 -Wall -Wextra -Wpedantic -Wconversion \
  -Wsign-conversion -Wshadow -Werror \
  auditors/d8_dependency_audit.cpp -o build/d8_dependency_audit
```

List the normalized profiles, audit one or more profiles, or run the complete
suite:

```bash
build/d8_dependency_audit --list-profiles
build/d8_dependency_audit --profile d8-a-w5
build/d8_dependency_audit --profile d8-a-w5 --profile d8-b-w6
build/d8_dependency_audit --suite
```

Successful commands write one canonical, newline-terminated JSON document to
standard output. Invalid command lines write a JSON error to standard error
and return exit code `2`. The complete suite must match
`tests/fixtures/d8-dependency-counts-v1.json` byte for byte:

```bash
build/d8_dependency_audit --suite > /tmp/d8-audit.json
cmp /tmp/d8-audit.json tests/fixtures/d8-dependency-counts-v1.json
uv run pytest tests/integration/test_d8_dependency_audit.py \
  tests/test_schema_resources.py
```

The versioned receipt schema is
`schemas/d8-dependency-pivot-audit-v1.schema.json`. The receipt intentionally
contains no elapsed time, hostname, compiler path, temporary path, or hash-map
iteration order. Repeating a command with the same source therefore gives the
same bytes.

For `auditor_version` 1.0.0 and `semantics_version`
`exact-incidence-root-pivot-v1`, the checked-in seven-profile suite receipt is
3,450 bytes with SHA-256
`222c283764ebcaecefde9b08590fa5d376993ef541de80ec646a4b0b63831230`.

## Mathematical state model

For a profile on `w` vertices, the normalized initial root is vertex `0` and
active column `c` initially targets vertex `c + 1`. A column of multiplicity
`m` chooses an `m`-element source set that excludes its target. Each source
`s` contributes the directed dependency arc

```text
s -> target(c).
```

Columns and their targets are role-labelled. Equal-multiplicity columns are
not divided by an isomorphism action. This choice makes the enumeration easy
to audit and makes every count refer to a concrete Cartesian product of source
sets.

The seven built-in profiles are:

| Profile | `w` | Active indegrees | Mobile triples | Inert multiplicity |
| --- | ---: | --- | --- | ---: |
| `d8-a-w5` | 5 | `(3,2,2,2)` | column 0 | 2 |
| `d8-b-w6` | 6 | `(3,3,2,2,1)` | columns 0,1 | 2 |
| `d8-c-frozen-w5` | 5 | `(3,3,1,1)` | columns 0,1 | 3 |
| `d8-c-frozen-w6` | 6 | `(3,3,2,1,1)` | columns 0,1 | 3 |
| `d8-c-frozen-w7` | 7 | `(3,3,2,2,1,1)` | columns 0,1 | 3 |
| `d8-c-mobile-w6` | 6 | `(3,3,3,1,1)` | columns 0,1,2 | 2 |
| `d8-c-mobile-w7` | 7 | `(3,3,3,2,1,1)` | columns 0,1,2 | 2 |

“Frozen” means that the omitted inert column is the third triple; “mobile”
means that all three triple columns are active and the inert column is an
ordinary double. The inert column itself does not contribute active arcs. Its
source set is reconstructed uniquely from the row deficits below.

## Enumeration filters and receipt counters

The program visits every product of loopless source-set choices. It records
the filters separately so a future refactor cannot silently change the
enumeration universe:

- `candidate_assignments`: every Cartesian-product assignment before filters;
- `root_outdegree_at_least_two`: assignments whose normalized root has at
  least two active outgoing arcs, whether or not they are reachable;
- `root_reachable`: assignments in which every vertex is reachable from the
  normalized root, whether or not the root has two outgoing arcs;
- `dependency_admissible`: the intersection of the preceding two conditions;
- `incidence_admissible`: dependency-admissible assignments satisfying every
  exact row constraint.

For active outdegree `d+(v)`, define the inert-column deficit

```text
epsilon(root) = 3 - d+(root),
epsilon(v)    = 2 - d+(v)       for v != root.
```

Exact incidence requires every deficit to be `0` or `1` and their sum to be
the profile's inert multiplicity. Equivalently, the active root outdegree is
`2` or `3`, every other active outdegree is `1` or `2`, and the vertices with
deficit one are exactly the sources of the inert column.

## Robustness and the root pivot

Let `q` be the current target of a mobile triple. Delete `q` from the active
digraph and recompute reachability from the current root. The target is
robust exactly when all three of its sources remain reachable. This is the
executable form of the dominator criterion: all three sources lie outside the
region dominated by `q`.

A pivot on column `c` is legal exactly when the current root `r` is one of its
sources. If `q` is the current target, the transition is

```text
new root      = q
new target(c) = r
new sources(c) = (sources(c) - {r}) union {q}.
```

Every other target and source set is unchanged. The transition is an
involution. It preserves column multiplicities, looplessness, exact row
deficits, and rooted reachability. These preservation facts are why the C++
state key needs only the current root, active targets, and active source masks;
the inert source mask is invariant and is reconstructed by the row deficits.

The implementation explores each connected component of the finite pivot
graph once. Because pivot edges are undirected, a multi-source breadth-first
search from all robust states gives the exact minimum pivot distance for every
state in that component. A component with no robust state is recorded as
unresolved.

The final counters have the following identities:

```text
incidence_admissible
  = sum(minimum_pivot_depth_histogram.values()) + pivot_unresolved

initial_all_mobile_triples_fragile
  = pivot_resolved + pivot_unresolved
```

Depth zero counts states already having a robust mobile triple. Positive
depths count initially all-fragile states first reaching one after that many
pivots. `complete: true` means the specified finite role-labelled search was
exhausted; it is not a claim that the mathematical proof is complete.

## Version-1 exact result ledger

The current complete-suite counts are summarized here for human review; the
JSON fixture remains the machine authority.

| Profile | Exact states | Initially fragile | Resolved | Unresolved | Minimum-depth histogram |
| --- | ---: | ---: | ---: | ---: | --- |
| `d8-a-w5` | 237 | 6 | 6 | 0 | `0:231, 1:6` |
| `d8-b-w6` | 5,787 | 32 | 32 | 0 | `0:5755, 1:32` |
| `d8-c-frozen-w5` | 85 | 8 | 8 | 0 | `0:77, 1:8` |
| `d8-c-frozen-w6` | 3,978 | 248 | 248 | 0 | `0:3730, 1:224, 2:24` |
| `d8-c-frozen-w7` | 232,049 | 9,368 | 9,228 | 140 | `0:222681, 1:7856, 2:1212, 3:160` |
| `d8-c-mobile-w6` | 3,192 | 0 | 0 | 0 | `0:3192` |
| `d8-c-mobile-w7` | 193,713 | 246 | 246 | 0 | `0:193467, 1:246` |

Thus the finite pivot mechanism resolves every initially fragile state in A,
B, frozen C through `w=6`, and all-mobile C through `w=7`. The 140 unresolved
one-frozen `w=7` states are an exact dependency-level residue, not negative
coloring certificates.

### Frozen `w=7` orbit classification

The independent Python reference additionally classifies the 140 frozen
residues. A *colored pivot orbit* retains all six active-column labels. Every
one of the 140 normalized initial residues lies in a different colored orbit,
and every such orbit has 56 oriented states. The classifier then quotients by
simultaneously swapping the two mobile triples, the two doubles, and the two
singleton columns. Exactly three pivot-isomorphism types remain, accounting
for 56, 56, and 28 normalized residues.

Use root `r`, mobile-triple targets `G,H`, double targets `A,B`, singleton
targets `S,T`, and let `F` be the three-vertex deficit set of the frozen inert
triple. Canonical representatives are:

```text
I (56):
  Pred(G)={r,H,A}  Pred(H)={r,S,T}  Pred(A)={G,B}
  Pred(B)={G,A}    Pred(S)={r}      Pred(T)={H}
  F={B,S,T}

II (56):
  Pred(G)={r,H,S}  Pred(H)={A,B,T}  Pred(A)={r,B}
  Pred(B)={r,A}    Pred(S)={G}      Pred(T)={H}
  F={G,S,T}

III (28):
  Pred(G)={r,A,S}  Pred(H)={r,S,T}  Pred(A)={G,B}
  Pred(B)={G,A}    Pred(S)={r}      Pred(T)={H}
  F={H,B,T}
```

With vertices ordered `(r,G,H,A,B,S,T)` and active columns ordered
`(G,H,A,B,S,T)`, the corresponding machine certificates
`(predecessor_masks; deficit_mask)` are:

```text
I:   (13,97,18,10,1,4; 112)
II:  (37,88,17,9,2,4; 98)
III: (41,97,18,10,1,4; 84)
```

All three contain the directed double diamond `A <-> B`, with one common
predecessor entering both double targets. The classification identifies a
rigid target for the next physical lemma, but it still says nothing about
proper edge-coloring realizability.

## Independent implementation and regression strategy

`tests/reference/d8_dependency_reference.py` is a deliberately separate
Python model. It shares neither state encoding nor search helpers with the C++
program:

- C++ packs a state into one 64-bit integer; Python uses immutable tuples;
- C++ generates masks by integer scan; Python uses combinations and products;
- C++ computes row deficits from an incremental array; Python reconstructs
  outgoing masks;
- both independently compute reachability, dominator robustness, pivots, and
  orbit distances from the mathematical definitions above.

The Python-only `classify_unresolved_pivot_orbits` diagnostic retains colored
pivot orbits first and applies equal-role canonicalization only in a second,
explicit step. Its immutable certificate types expose the predecessor and
deficit masks tested in the frozen `w=7` regression above.

Tests compile the production source with strict warnings, validate its JSON
against the public schema, compare all seven profile counts with the checked-in
golden receipt, and differentially reproduce representative small and large
profiles with Python. CLI failure cases and profile-selection ordering are
also tested. The release package gate separately unpacks the built sdist,
compiles the auditor using only source and fixture paths inside that unpacked
tree, and requires its complete-suite bytes to equal the packaged golden
receipt. Thus checkout compilation cannot conceal a missing sdist dependency.

The two `w=7` Python enumerations are opt-in because they are intentionally
much slower than the C++ suite:

```bash
TOTAL_COLORING_RECOMPUTE_D8_LARGE=1 \
  uv run pytest tests/integration/test_d8_dependency_audit.py \
  -k independent_python_reference_reproduces_large_goldens
```

The three-type frozen classification is a separate opt-in regression:

```bash
TOTAL_COLORING_RECOMPUTE_D8_LARGE=1 \
  uv run pytest tests/integration/test_d8_dependency_audit.py \
  -k frozen_w7_unresolved_pivot_orbit_classification
```

When extending the auditor, update all of the following in one reviewable
change:

1. add or change the C++ `Profile` descriptor;
2. make the same mathematical change independently in the Python `PROFILES`;
3. bump `semantics_version` for any change to the state universe, filters,
   robustness predicate, or pivot transition;
4. version the schema if its public structure changes;
5. regenerate the golden receipt only after both implementations agree;
6. document the new proof obligation and its limitations here.

## Trust boundary

The audit establishes only a finite fact about the exact abstract dependency
model. In particular, it does not check:

- realization by a simple graph or by a proper partial edge coloring;
- the distinguished matching-plus-star structure;
- alternating-component pairing or shared-edge compatibility;
- preservation of the rainbow condition under a physical recoloring;
- criticality, maximum-degree hypotheses, or degree-sum hypotheses;
- the remaining family-C star-lollipop or center-changing lemma.

Those exclusions are repeated in every suite receipt so downstream users do
not have to discover the boundary by reading the source.
