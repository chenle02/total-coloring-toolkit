# Architecture

The toolkit separates five responsibilities:

1. **Canonical model:** immutable graphs, elements, and stable fingerprints.
2. **Problem construction:** total-coloring and rainbow-edge-coloring conflict
   systems.
3. **Search backends:** deterministic algorithms that return verified-witness,
   candidate-UNSAT, proved-UNSAT (when supported), or UNKNOWN under explicit
   resource limits.
4. **Independent verification:** direct semantic checks that do not call a
   search backend.
5. **Orchestration:** graph generation, filtering, sharding, manifests, and
   review-gated promotion.

The core has no runtime dependencies. Optional adapters may integrate NetworkX,
nauty, or SAT solvers without changing the graph or certificate schemas.

## Schema packaging boundary

`schemas/` at the repository root is the single canonical schema tree. It is
included directly in the source distribution. When building a wheel, Hatch
copies the same versioned files into `total_coloring/_schemas/`; no second checked-in
copy exists. `total_coloring.schema_resources` reads the canonical root during
source development and uses `importlib.resources` in an installed wheel. Its
`SchemaName` allowlist prevents path traversal and makes additions to the public
schema contract explicit. Source-parity tests compare every API result byte for
byte with the canonical tree, while the foreign-directory wheel smoke verifies
the installed resource surface without the repository on `sys.path`.

Two dependency-free exhaustive backends intentionally make different search
choices. The primary backend uses dynamic DSATUR ordering and safe color
symmetry breaking. The audit backend uses a fixed item order and tries every
palette color. Agreement between them is useful bounded evidence, but neither
produces a checkable UNSAT proof. A future proof-producing SAT backend must
retain the same semantic verifier and add an independently checked proof trace.

`census` and `universal-census` intentionally have different contracts. The
former is a backward-compatible existential search that stops after one
verified extension. The latter writes one graph record containing the complete
canonical partition sequence and a configured backend/palette check matrix.
Universal records omit wall-clock time, retain deterministic node/backtrack
counters, and embed every positive auxiliary assignment. Parsing reconstructs
the graph, partition, auxiliary construction, semantic problem digest, edge
coloring, and decoded total coloring without calling the producing backend.
Completeness has two independent layers: a vertex-mask dynamic program counts
the required-size complement matchings, then the canonical enumerator's exact
ordered partition sequence is compared with the stored transcript.

## Finite proof-obligation auditors

The root-level `auditors/` tree contains standalone native programs for finite
mathematical audits that are not part of the installed Python runtime. The
`D = 8` dependency auditor is included in the source distribution, while the
wheel contains only the Python package and versioned schemas. Its C++20
implementation is optimized for exhaustive orbit traversal; a structurally
independent Python model under `tests/reference/` recomputes the same counts
from the definitions. The checked-in JSON fixture is a deterministic golden
receipt, not raw run output.

This separation is intentional. Search speed, reference readability, public
data structure, and mathematical interpretation are different trust layers.
The production and reference implementations share profile semantics but no
enumeration, reachability, pivot, or state-encoding helpers. See
[`d8-dependency-audit.md`](d8-dependency-audit.md) for the complete contract.

## Trust boundary

A SAT result becomes usable only after its semantic certificate passes the
independent verifier. An UNSAT result is backend evidence until accompanied by
a checkable proof trace or by a separately reproduced exhaustive reference
search. Census manifests distinguish these states.

Likewise, a complete dependency-audit receipt proves exhaustion only within
its abstract role-labelled incidence model. It is not a physical coloring
certificate and does not establish that any enumerated state is graph
realizable.

## Public/private/data split

- `Article-Total-Coloring`: private drafts, raw output, shard logs, and failed
  experiments.
- `total-coloring-toolkit`: public software, schemas, tests, documentation,
  and tiny fixtures.
- `total-coloring-data`: public reviewed results, reports, checksums, and
  release manifests.
