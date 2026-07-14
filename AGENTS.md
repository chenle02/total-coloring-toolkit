# Total Coloring Toolkit contributor contract

This repository contains public, reusable software for exact finite graph
coloring experiments. Scientific correctness and reproducibility take priority
over throughput.

## Trust boundary

- Treat every solver as an untrusted witness producer.
- Verify positive assignments through a semantic checker that does not invoke
  the producing solver or reuse its encoding.
- Use `candidate_unsat` for exhausted searches without an independently checked
  negative proof. Never rename it to `unsat`, `counterexample`, or `proved`.
- A bounded census is evidence only for its exact generator, filters, shards,
  software identity, and resource limits. It is not an unbounded theorem.

## Repository boundary

- Algorithms, schemas, tests, tiny fixtures, and documentation belong here.
- Raw runs, checkpoints, scheduler logs, and exploratory output belong in the
  private `Article-Total-Coloring` working repository.
- Only reviewed, schema-valid, hash-pinned artifacts may be promoted to the
  separate `total-coloring-data` repository.
- Never add credentials, private paths, source PDFs, or unpublished manuscript
  text.

## Engineering rules

- Keep the runtime core dependency-free. Optional integrations must not alter
  canonical graph or certificate semantics.
- Defensively freeze nested inputs before hashing them.
- Use canonical JSON and SHA-256 for scientific identities. Distinguish
  numbered-graph identity from any future isomorphism-canonical identity.
- Preserve deterministic item, edge, partition, clause, and record ordering.
- Stream graph generators without a shell. Account for every input as witness,
  candidate negative, unknown, error, or explicit skip.
- Write completed artifacts through fsync plus atomic replacement. Never emit a
  completion marker before the input stream is fully consumed.
- Version schemas before making an incompatible representation change.

## Required checks

Run before every commit:

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=total_coloring --cov-report=term-missing
uv build
uv run twine check dist/*
```

Changes to a solver or encoding require an independent small-instance
differential test. Changes to a schema, checkpoint, publisher, or verifier
require malformed-input and interrupted-write tests in addition to a happy
path.

Before a release, build the wheel only from the unpacked source distribution,
audit exact archive membership, install the final wheel outside the source
tree on every supported Python version, and record immutable artifact hashes.

