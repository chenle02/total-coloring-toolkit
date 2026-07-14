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
