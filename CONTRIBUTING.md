# Contributing

Contributions should preserve three guarantees: deterministic inputs produce
deterministic records, every positive certificate has an independent verifier,
and an interrupted search cannot be mistaken for an exhaustive one.

1. Open an issue describing the mathematical or engineering change.
2. Add tests that fail before the implementation change.
3. Run the full quality gate documented in `README.md`.
4. Keep solver code separate from certificate verification code.
5. Do not commit generated census output or private manuscript material.

Changes to a JSON schema require a schema-version decision and migration note.
Performance changes require a correctness cross-check against the reference
backend on the same deterministic fixture set.
