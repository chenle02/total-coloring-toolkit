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

## Universal release preparation

Public release preparation reconstructs each completed run configuration from
its canonical manifest and validates it through the same trust path used for a
completed-run replay. The local `geng` basename, executable SHA-256, arguments,
graph order, record sequence, and exact EOF must all match. The release-v1
profile accepts exactly the three default DSATUR/static checks and one
unrestricted, unsharded stream per order. It derives one canonical
`finite_bound` claim from all included orders; callers cannot override its
scope or its two bounded-evidence limitations.

The replay archive is gzip-wrapped USTAR with no directory entries. Its exact
gzip header is `1f8b08000000000002ff` (level 9, no optional fields, mtime zero,
OS 255). Validation requires one gzip member through raw EOF and verifies its
CRC32 and ISIZE trailer. USTAR headers occur at receipt-derived offsets;
members are lexicographically ordered; mode is `0644`; mtime, uid, and gid are
zero; and user, group, and link names are empty. File padding, the two terminal
zero blocks, and padding to the next 10,240-byte tar record are all zero and
have the exact derived length. Every member path, size, and SHA-256 is bound
into the compact summary before the complete archive's size and digest are
bound into both the summary and dataset manifest.

Untrusted JSON is bounded to 16 MiB, 128 nesting levels, and 1,024 integer
digits. Run manifests and completion markers, including archived copies, are
limited to 4 MiB. Archive validation streams member hashing and enforces the
same 16 MiB raw-line cap as universal-census JSONL replay.

Export is non-overwriting and stages both outputs beside their destinations.
Linux `renameat2(RENAME_NOREPLACE)` installs the archive first and then the
compact bundle. An ordinary exception rolls back an installed entry only while
its exact inode identity is still owned by the transaction; a foreign
replacement is preserved and reported. This is process-level rollback, not a
two-name power-loss-atomic commit: a crash after the first durable rename can
leave an archive without its bundle.

Promotion remains a separate dry run by default and requires caller-supplied
offline bytes for every declared external artifact. Missing files use the same
no-clobber rename. Existing files use `RENAME_EXCHANGE`, then verify that the
displaced inode and digest are exactly the inspected original before accepting
the new file. Rollback and displaced-file cleanup are identity-gated, so a
concurrent foreign replacement is never unlinked as transaction-owned state.
A promotion spans several Git worktree paths and is therefore not power-loss
atomic; its rollback guarantee covers ordinary process-level failures while
the process is alive.
