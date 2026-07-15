# Changelog

All notable changes are documented here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Scale the sealed order-nine Easley profile from 64 to 2,048 `geng -X2`
  shards, with a profile-default 2,048-task throttle on `nova_short`.
- Give the serial order-nine exact-union replay a 24-hour `nova_long` walltime
  so its global disjointness and EOF gate has measured scaling headroom.
- Require every Easley stage to reject shard counts that are not powers of two
  or exceed the current safe 2,048-shard campaign bound, while retaining the
  golden order-eight prerequisite at exactly 64 shards.

### Fixed

- Discover `sbatch`, `scancel`, and `scontrol` through Easley's stable Slurm
  installation when module operations remove the scheduler directory from
  `PATH`.

## [0.2.1] - 2026-07-14

### Fixed

- Run release-checkout Git commands from `cwd` instead of using `git -C`, and
  use the legacy-compatible `git status --porcelain` spelling, so the
  documented Easley submission works with the cluster's Git 1.8.3 after
  loading the pinned Python module.

## [0.2.0] - 2026-07-14

### Added

- Provenance-bound `geng -X#` split-depth support for balanced census arrays,
  while retaining byte-compatible version-1 configuration manifests when the
  option is absent.
- Read-only validation of complete universal-census shard sets, including
  per-shard replay, uniform-contract checks, and exact comparison of the
  disjoint shard union with the unrestricted generator stream.
- Reusable Easley/Slurm launch tooling with isolated no-site execution from
  sealed launcher/wheel memory snapshots, canonical campaign contracts,
  a separate bootstrap-only runtime phase, prior runtime and `geng` hash pins,
  immutable receipt chains, atomic campaign reservation, checkpoint-aware
  arrays, independent validation, fail-closed reduction, and a replayed
  order-eight prerequisite gate for order-nine production.

## [0.1.0] - 2026-07-14

### Fixed

- Discover Debian's `nauty-geng` executable automatically and keep synthetic
  census tests hermetic when nauty is not installed on a development machine.
- Harden universal export and public-data promotion against final-path
  creation, existing-file edits, parent symlink substitution, and foreign
  replacement during rollback by using dirfd-pinned, identity-owned Linux
  rename transactions.

### Changed

- Document the durability boundary explicitly: ordinary process failures roll
  back transaction-owned identities, while the archive-then-bundle export and
  multi-file Git worktree promotion are not power-loss atomic.

### Added

- Read-only completed universal-census replay from strict manifest provenance.
- Deterministic universal release export with exact archive receipts,
  finite-claim summary, dataset-manifest v2, and staged non-overwriting output.
- Public-data promotion trust pins for v2 schemas and required offline
  validation of external replay archives, while preserving v1 bundles.
- Bounded JSON depth, document size, and integer parsing plus exact single-member
  gzip/USTAR trailer, header, layout, and padding verification.
- Explicit DSATUR/static auxiliary backend selection and a separate resumable
  universal-census transcript with complete all-partition witness replay.
- Canonical immutable simple graphs with strict JSON and graph6 codecs.
- Independent total-coloring certificates and semantic verification.
- Deterministic iterative DSATUR, an independent static-order audit backend,
  and transparent pairwise one-hot CNF encoding.
- Complement-matching enumeration and the Chen--Shan auxiliary rainbow search.
- Streamed nauty `geng` integration and resumable, hash-pinned census artifacts.
- Exact arithmetic audits for the draft counting obligations.
- Transactional, dry-run-by-default promotion into a separate public data repo.
- Typed CLI, strict quality gates, differential tests, and security workflows.

[Unreleased]: https://github.com/chenle02/total-coloring-toolkit/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/chenle02/total-coloring-toolkit/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/chenle02/total-coloring-toolkit/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/chenle02/total-coloring-toolkit/releases/tag/v0.1.0
