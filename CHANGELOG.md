# Changelog

All notable changes are documented here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/chenle02/total-coloring-toolkit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chenle02/total-coloring-toolkit/releases/tag/v0.1.0
