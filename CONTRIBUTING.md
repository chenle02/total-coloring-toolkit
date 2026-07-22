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

## Native finite auditors

`auditors/d8_dependency_audit.cpp` is standalone C++20 publication source, not
part of the Python wheel. Changes must preserve its dependency-free build and
pass the strict compiler gate:

```bash
c++ -std=c++20 -O2 -Wall -Wextra -Wpedantic -Wconversion \
  -Wsign-conversion -Wshadow -Werror \
  auditors/d8_dependency_audit.cpp -o d8_dependency_audit
```

Do not share enumeration or graph-search helpers with
`tests/reference/d8_dependency_reference.py`; its value is structural
independence. A change to the state universe, incidence filters, robustness
test, or pivot transition requires a `semantics_version` decision. Regenerate
the golden JSON receipt only after both implementations agree, validate it
against the public schema, and explain every changed count in the pull request.
Keep ad hoc runs, profiling output, and manuscript-only countermodels out of
this repository.
