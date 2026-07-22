# Total Coloring Toolkit

`total-coloring-toolkit` is a typed, deterministic research package for exact
total-coloring verification and finite graph search. Its central design rule is
**proof-carrying output**: a solver may propose a coloring, but a small,
independent verifier decides whether the certificate is valid.

The first research application tests the Chen--Shan auxiliary-coloring route:
enumerate every relevant equitable partition, construct the auxiliary graph,
and ask whether its distinguished family admits a rainbow edge-coloring with
the target palette. Failure for one partition is never reported as a
counterexample when another partition remains untested.

## Scope

- immutable canonical simple-graph objects and graph6 I/O;
- deterministic exact coloring through a dependency-free DSATUR backend;
- an independent static-order, no-symmetry audit backend for differential checks;
- independently checked total-coloring certificates;
- high-degree equitable partitions via complement matchings;
- auxiliary-graph construction, rainbow extension, and decoding;
- streamed `nauty-geng` enumeration with reproducible sharding;
- resumable existential and replayable universal census output with versioned
  schemas and SHA-256 provenance;
- wheel-installed access to every versioned JSON schema through a typed,
  traversal-safe resource API;
- a deterministic, independently reconstructed m=6 protected-transfer CEGAR
  campaign with resumable cuts and dual-checker LRAT receipts;
- explicit finite audits of algebraic proof obligations.

Solver success is computational evidence, not a theorem. Exhaustive claims
must state the generator, filters, shard coverage, software version, and
verification method.

## Development

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=total_coloring --cov-report=term-missing
uv run python scripts/package_gate.py
```

## Install and 60-second quickstart

The supported platform is Linux/POSIX with Python 3.11--3.14. Install from the
public GitHub repository or a hash-verified GitHub release wheel (there is no
PyPI release claim):

```bash
python -m pip install \
  "total-coloring-toolkit @ git+https://github.com/chenle02/total-coloring-toolkit.git"
# Isolated CLI alternative:
pipx install "git+https://github.com/chenle02/total-coloring-toolkit.git"
```

Direct solving and certificate verification need no external runtime packages.
Create the graph6 encoding of one edge and find its three-color total coloring:

```bash
printf 'A_\n' > edge.g6
total-coloring solve --graph edge.g6 --graph-format graph6 --colors 3
```

The same verified workflow is available as a typed Python API:

```python
from total_coloring.certificates import TotalColoringCertificate, verify_total_coloring
from total_coloring.graph import SimpleGraph
from total_coloring.solver import SolveStatus, solve_dsatur
from total_coloring.total import split_total_assignment, total_coloring_problem

graph = SimpleGraph.from_edges(2, [(0, 1)])
result = solve_dsatur(total_coloring_problem(graph, 3))
assert result.status is SolveStatus.WITNESS and result.assignment is not None
vertex_colors, edge_colors = split_total_assignment(graph, result.assignment)
certificate = TotalColoringCertificate.create(graph, 3, vertex_colors, edge_colors)
assert verify_total_coloring(graph, certificate).valid
```

Large graph censuses additionally require nauty's `geng`. The toolkit
automatically discovers both the upstream `geng` name and Debian-family
distributions' `nauty-geng` name:

```bash
GENG=$(command -v geng || command -v nauty-geng)
"$GENG" -help 2>&1 | head -n 2
# Use --geng /custom/path/geng only to select a nonstandard installation.
```

## Command-line examples

Solve a direct total-coloring instance and write an independently verified
certificate:

```bash
total-coloring solve --graph graph.g6 --colors 6 \
  --certificate-out certificate.json
total-coloring verify --graph graph.g6 --certificate certificate.json
```

Search the equitable-partition auxiliary construction:

```bash
total-coloring aux-search --graph graph.g6 --colors 7 \
  --max-nodes 1000000 --timeout 60
```

Test the stronger statement that **every** equitable partition extends:

```bash
total-coloring aux-check-all --graph graph.g6 --colors 7
# Independently repeat the same check with the static-order backend:
total-coloring aux-check-all --graph graph.g6 --colors 7 \
  --backend static-order-iterative-v1
```

Run or resume an atomic, provenance-pinned `geng` census. The default palette
is `D+2 = Delta(G)+3`, and the default filter is the paper's high-degree regime
`2 Delta(G) >= n`:

```bash
total-coloring census --order 8 --output runs/order-8 \
  --shard-index 0 --shard-count 16 --split-depth 2
```

`--split-depth 2` passes the canonical `-X2` work-division option to `geng`.
It requires a shard index and count, and every shard in one set must use the
same value. The exact token is bound into the generator argument vector and
run fingerprint; it changes how `geng` divides work, not the union of graphs.

For a replayable universal transcript, use the separate command. It stores one
canonical JSONL record per generated graph, nests every equitable partition,
and retains the complete auxiliary edge-color assignment for each successful
check. The defaults compare DSATUR at `D+1` and `D+2` with the independent
static-order backend at `D+1`:

```bash
total-coloring universal-census --order 8 --output runs/order-8-universal \
  --shard-index 0 --shard-count 16 --split-depth 2

# Override the check matrix by repeating --check BACKEND:OFFSET.
total-coloring universal-census --order 6 --output runs/custom \
  --check dsatur:1 --check static:1
```

An artifact-level `verified_all` status is accepted only after the parser
reconstructs every partition and auxiliary problem and semantically replays
every stored witness. A cross-backend `D+1` status disagreement is an error,
not a result to average or majority-vote.

Running the same `universal-census` command again on a completed directory is
also its verification operation: it checks hashes and canonical schemas,
replays every stored witness, regenerates the configured `geng` stream, and
compares graph6, fingerprint, index, and end-of-stream coverage exactly.

After all shards complete, validate the array as one scientific object. The
validator replays each run, rejects mixed configurations or executable/toolkit
identities, detects overlap, and compares the shard union with the matching
unsharded `geng` stream through exact EOF. It is read-only and retains graph6
identifiers only up to an explicit memory cap:

```bash
total-coloring universal-validate-shards \
  --run runs/order-8/shard-00 --run runs/order-8/shard-01 \
  --geng /absolute/path/to/geng --max-union-graphs 1000000
```

This validation does not weaken the public release-v1 rule below: sharded
transcripts remain an audit result until a separately reviewed sharded release
profile is implemented. The guarded Slurm workflow used for larger arrays is
documented in [`docs/easley.md`](docs/easley.md).

After every per-order run is complete, prepare the reviewed public-data
candidate and its separate deterministic replay archive. This command is
strictly offline: it replays every witness, regenerates every `geng` stream,
requires one unrestricted unsharded run per order, and rejects mixed identities
or adverse statuses before writing either final output.

```bash
total-coloring universal-export \
  --run runs/order-01 --run runs/order-02 --run runs/order-03 \
  --bundle candidates/order-1-3-v1 \
  --archive release-assets/order-1-3-universal-census-replay-v1.tar.gz \
  --summary-id order-1-3-universal-census \
  --created-utc 2026-07-14T12:00:00Z \
  --release-version 1.0.0-rc.1 \
  --code-commit 61c576fba28a03a91f6a7695e21d130cd7e76f22 \
  --external-name archives/order-1-3-universal-census-replay-v1.tar.gz \
  --external-url https://github.com/OWNER/REPO/releases/download/TAG/ASSET.tar.gz
```

The compact bundle contains the version-2 dataset manifest, one finite-scope
summary, exact schema bytes, and `SHA256SUMS`. The large archive remains an
external release asset. `PublicationConfig.external_files` makes the existing
dry-run-by-default promotion layer validate the supplied archive, all member
receipts, and the finite claim before any Git worktree is changed.

Both write paths fail closed on concurrent pathname changes. Export installs
the archive before the bundle, and ordinary exceptions roll back only entries
whose inode identities still belong to the transaction. A process or power
loss between the two durable renames can therefore leave an archive-only
candidate. Public promotion likewise provides identity-owned rollback for
ordinary process failures, but a multi-file Git worktree update is not
power-loss atomic; inspect or re-plan after an interrupted machine-level write.

Audit the draft's smallest `c=2`, `P=Q=1` arithmetic case:

```bash
total-coloring proof-audit --repeated 1 --singletons 1 --cap 2
```

Commands emit canonical JSON. Exit code `0` means a verified witness or valid
certificate, `1` means a candidate negative/invalid certificate, `2` means an
operational error, and `3` means the search was incomplete. A candidate
negative is never presented as a proved UNSAT result.

See [the mathematical specification](docs/mathematical-specification.md),
[architecture](docs/architecture.md), and
[reproducibility policy](docs/reproducibility.md). The
[research-target audit](docs/research-target.md) records the exact reduction,
current conjectural extension statements, and corrected proof obligations.
The [m=6 protected-transfer guide](docs/m6-protected-transfer.md) documents the
separate bounded proof-search formula, resume semantics, and LRAT trust
boundary.

## Schema resources

The ten public JSON schemas are part of both the source distribution and the
wheel. Applications should use the typed API instead of assuming a repository
layout:

```python
from total_coloring.schema_resources import SchemaName, read_schema_json, schema_names

assert SchemaName.GRAPH_V1 in schema_names()
graph_schema = read_schema_json(SchemaName.GRAPH_V1)
```

Only names in `SchemaName` are accepted. The repository-level `schemas/` tree
is canonical; the build copies those exact bytes into the wheel.

## Repository boundary

This public-ready code repository contains algorithms, schemas, tests, and
small fixtures only. Raw searches and HPC shard output belong in the private
`Article-Total-Coloring` working repository. Only reviewed, merged,
hash-pinned results are promoted to the separate `total-coloring-data`
repository.

## License

MIT. See [LICENSE](LICENSE).
