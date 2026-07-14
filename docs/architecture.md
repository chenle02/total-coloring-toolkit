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
copies the same five files into `total_coloring/_schemas/`; no second checked-in
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

## Trust boundary

A SAT result becomes usable only after its semantic certificate passes the
independent verifier. An UNSAT result is backend evidence until accompanied by
a checkable proof trace or by a separately reproduced exhaustive reference
search. Census manifests distinguish these states.

## Public/private/data split

- `Article-Total-Coloring`: private drafts, raw output, shard logs, and failed
  experiments.
- `total-coloring-toolkit`: public software, schemas, tests, documentation,
  and tiny fixtures.
- `total-coloring-data`: public reviewed results, reports, checksums, and
  release manifests.
