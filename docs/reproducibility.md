# Reproducibility policy

Every census record must identify:

- the graph in canonical graph6 and SHA-256 form;
- portable generator executable basename, executable SHA-256, and exact
  argument vector (resolved local paths are deliberately never serialized);
- all mathematical filters;
- shard index and shard count;
- toolkit version and schema version;
- solver backend and explicit node/time limits;
- SAT certificate verification status;
- counts of tested, skipped, verified-witness, candidate-UNSAT, proved-UNSAT
  (when supported), UNKNOWN, and ERROR instances.

Output is written to a temporary file, flushed and synchronized, then promoted
atomically. Completion manifests are emitted only after the input stream is
fully consumed. Raw shard output remains private. Public promotion is a
separate dry-run-by-default operation that verifies every hash.

The universal census uses one JSONL line as the graph-level checkpoint. Each
line contains every canonical equitable partition and each configured
backend/palette check. Witness checks retain the full auxiliary edge-color
assignment; UNKNOWN, ERROR, and candidate-UNSAT checks retain no assignment.
Elapsed time is deliberately excluded because it is not deterministic.
`verified_all` requires all nested checks to be replayed witnesses, while a
DSATUR/static status disagreement at `D+1` fails closed as ERROR.

The `eligible` bit means the graph lies in the configured generator-order,
filter, and auxiliary-construction domain. A solver exception is retained as
an ERROR check inside a complete eligible transcript. A structural partition
enumeration or construction failure instead aborts the run, leaves the prior
checkpoint durable, and withholds completion; it is never rewritten as an
ineligible graph.

Artifact parsing bounds each canonical JSONL graph record at 16 MiB before
decoding. This is far above order-eight transcript sizes while preventing an
untrusted output directory from forcing an unbounded line allocation.
