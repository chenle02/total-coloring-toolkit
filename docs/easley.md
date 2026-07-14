# Easley universal-census campaigns

The modules in `scripts/easley/` run a universal census as a guarded Slurm
pipeline. They are reusable, but intentionally encode the current golden
contracts for the order-eight smoke and order-nine production campaigns. Raw
records and scheduler logs stay outside Git under one isolated scratch root.

## Trust and storage boundaries

- Compile nauty on a compute node from the pinned 2.9.3 source archive. The
  bootstrap rejects any archive except SHA-256
  `9fc4edae04f88a0f5883985be3b39cf7f898fd6cc96e96b9ee25452743cc1b5b`.
- Snapshot the exact caller-supplied wheel into a sealed Linux memory file
  after checking its SHA-256. Bootstrap installs a private, rechecked copy. A
  runtime receipt records the wheel, compiled `geng`, Python, nauty source,
  Slurm job, and order-four replay smoke.
- Build a deterministic launcher ZIP from the clean release checkout. The
  wrapper starts the cluster Python with `-I -B -S`, snapshots the ZIP and
  wheel into write-sealed memory files, and only then adds those stable
  `/proc/self/fd` views to `sys.path`. No launcher, wheel, `.pth`,
  `sitecustomize`, or bytecode from a mutable path executes before validation.
  Bootstrap also copies the launcher into the frozen runtime as an audit
  artifact; later stages continue to execute the submitted in-memory snapshot.
- Seal one canonical campaign contract containing the complete `TC_*`
  environment before the first `sbatch`. Every job validates that contract.
  Job-local `geng`, runtime-receipt, wheel, and launcher snapshots are immutable
  for the lifetime of the process.
- Separate runtime creation from science. A bootstrap-only campaign may create
  one frozen runtime but cannot submit census work. A later campaign must name
  the exact runtime-receipt and compiled-`geng` SHA-256 values in its sealed
  contract before it can submit any scientific stage.
- Keep the runtime and campaign source under a small home-directory campaign
  root; keep `runs/`, `logs/`, and `status/` under `/scratch/$USER/...`.
- The venv, launcher sources, `geng`, and Python executable digest are sealed
  and rechecked. The runtime still trusts Easley's cluster-managed, read-only
  Anaconda standard library and operating-system shared libraries; the Python
  version, platform, and executable SHA-256 make that external trust boundary
  explicit.
- Never use `rsync --delete` against a live campaign. Sync back only after
  `status/exact-union-complete.json` exists and has been reviewed.
- The completion marker validates an exact finite computation. It is not an
  unbounded total-coloring theorem and does not authorize the release-v1
  exporter, which still requires an unsharded run.

## Job graph

Runtime creation is a one-job bootstrap-only campaign. The order-eight
scientific submitter then creates five dependency-linked stages:

1. `bootstrap`: validate the separately built, read-only runtime against the
   prior receipt and binary pins;
2. `census-array`: 64 one-core, checkpoint-aware `-X2` shards;
3. `validation-array`: independently replay every completed transcript and its
   exact shard generator stream;
4. `reduce`: require all receipts, expected totals, three checks per partition,
   and zero candidate-UNSAT, UNKNOWN, or ERROR statuses;
5. `exact-union`: replay every shard again and prove that their disjoint graph6
   union equals the corresponding unsharded `geng` stream through EOF.

Order nine adds a fail-closed stage before bootstrap:

0. `order8-prerequisite`: on a Nova compute node, validate all 64 retained
   order-eight validation receipts and their `records.jsonl`, `manifest.json`,
   and `completion.json` artifacts, then independently replay the complete
   shard set and exact unsharded union. It writes a canonical artifact-root and
   replay-bound gate receipt into the new order-nine scratch tree.

The production dependency chain is therefore
`order8-prerequisite -> bootstrap -> census-array -> validation-array -> reduce
-> exact-union`. Bootstrap and the final exact-union stage both require the
gate receipt and its order-eight receipt SHA-256; a direct final-stage launch
without that gate fails.

The census array receives `USR1` five minutes before walltime. The Python
handler lets the graph-level checkpoint and lock close cleanly, then asks Slurm
to requeue that exact array element. The wrapper never deletes a stale lock;
unexpected termination therefore fails closed for operator review.

## Two-phase dry run and submission

Run from the exact staged toolkit commit. Before a real submission, the
submitter requires `HEAD` to equal `--code-commit`, requires the entire checkout
to be clean, and requires the launcher files and their package directories to
be read-only. Paths and hashes below are examples; the submitter verifies the
wheel before doing anything and does not create the scratch tree in dry-run
mode. Use a dedicated release clone and freeze its launcher path after
inspection with:

```bash
find scripts/easley -type f -name '*.py' -exec chmod a-w {} +
find scripts/easley -type d -exec chmod a-w {} +
chmod a-w scripts/__init__.py scripts .
```

The `--scratch` path must not exist. On `--submit`, one process atomically
reserves it, writes the hash-bound launcher ZIP and campaign contract, and
creates the journal before calling `sbatch`; a concurrent submitter therefore
fails instead of sharing a campaign. SIGINT, SIGTERM, and SIGHUP are deferred
across each `sbatch` plus journal update, then cancel every recorded job if
submission is interrupted.

```bash
# Phase A: build only; this campaign cannot submit census jobs.
python -m scripts.easley.submit \
  --profile order8-smoke \
  --bootstrap-only \
  --code-root "$HOME/total-coloring/CAMPAIGN/code" \
  --code-commit 40_HEX_GIT_COMMIT \
  --scratch "/scratch/$USER/CAMPAIGN-runtime-bootstrap" \
  --runtime "$HOME/total-coloring/CAMPAIGN/runtime" \
  --wheel "$HOME/total-coloring/CAMPAIGN/artifacts/total_coloring_toolkit-0.2.1-py3-none-any.whl" \
  --wheel-sha256 WHEEL_SHA256 \
  --toolkit-version 0.2.1 \
  --nauty-tar "$HOME/total-coloring/CAMPAIGN/artifacts/nauty2_9_3.tar.gz"
```

Inspect the dry-run JSON and repeat it with `--submit`. Wait for the bootstrap
job to finish, inspect `runtime/runtime-receipt.json`, and record independent
pins from the frozen files:

```bash
RUNTIME_RECEIPT_SHA256=$(sha256sum \
  "$HOME/total-coloring/CAMPAIGN/runtime/runtime-receipt.json" | awk '{print $1}')
GENG_SHA256=$(sha256sum \
  "$HOME/total-coloring/CAMPAIGN/runtime/bin/geng" | awk '{print $1}')
```

Use a fresh, nonexistent scratch root for Phase B. Omitting either pin is an
error; the submitter also checks that both files, the runtime launcher, Python
executable, no-bytecode invariant, and complete read-only tree still match the
receipt before producing even a dry-run scientific plan.

```bash
# Phase B: pinned scientific campaign.
python -m scripts.easley.submit \
  --profile order8-smoke \
  --code-root "$HOME/total-coloring/CAMPAIGN/code" \
  --code-commit 40_HEX_GIT_COMMIT \
  --scratch "/scratch/$USER/CAMPAIGN-order8" \
  --runtime "$HOME/total-coloring/CAMPAIGN/runtime" \
  --wheel "$HOME/total-coloring/CAMPAIGN/artifacts/total_coloring_toolkit-0.2.1-py3-none-any.whl" \
  --wheel-sha256 WHEEL_SHA256 \
  --toolkit-version 0.2.1 \
  --nauty-tar "$HOME/total-coloring/CAMPAIGN/artifacts/nauty2_9_3.tar.gz" \
  --geng-sha256 "$GENG_SHA256" \
  --runtime-receipt-sha256 "$RUNTIME_RECEIPT_SHA256"
```

Inspect the canonical JSON plan, then repeat Phase B with `--submit`. After the
full order-eight pipeline produces its exact-union marker, use a separate
scratch root and add `--profile order9-production` plus
`--order8-receipt /scratch/$USER/CAMPAIGN-order8/status/exact-union-complete.json`.
The login-side submitter validates the exact-union v1 field set, all 64 ordered `-X2`
shard receipts and their sums, the canonical three-check matrix, the reduction
receipt and its SHA-256, the immutable runtime receipt, launcher archive,
toolkit identity, wheel, code commit, compiled `geng`, and all golden
order-eight totals before it will construct a production plan. This structural
check is deliberately not the trust anchor: the scheduled prerequisite stage
opens and replays the retained order-eight artifacts on Nova. The exact
order-eight receipt SHA-256, full artifact-root SHA-256, replay result, and gate
receipt SHA-256 are carried into the order-nine final receipt. Order nine
inherits both runtime pins from the validated order-eight evidence; explicitly
supplied runtime or `geng` pins must agree with that evidence.

The built-in exact expectations are:

| Profile | Graphs | Verified | Skipped | Partitions | Checks |
|---|---:|---:|---:|---:|---:|
| `order8-smoke` | 12,346 | 11,922 | 424 | 514,050 | 1,542,150 |
| `order9-production` | 274,668 | 259,197 | 15,471 | 26,634,630 | 79,903,890 |

The order-nine verified count is an acceptance gate, not an assumption hidden
inside the solver: if any eligible graph has a candidate negative, incomplete
search, backend disagreement, or error, the transcript remains preserved but
reduction fails and no exact-union completion marker is written.
