# m=6 protected-transfer finite campaign

The `scripts.m6.kernel` and `scripts.m6` campaign modules implement a
deterministic, resumable CEGAR search for one exact finite Type-I I.3+3
subproblem. This is computational proof-search infrastructure, not a theorem
statement and not a novelty claim.

The source integration preserves the mathematical encoder and optimized
endpoint semantics audited in the m=6 RC2 input set whose detached manifest
SHA-256 is
`5bbf6762027acaae5cc85b71572756f0cc6ae4da9da0ed1c56b1bc7d6fc771fd`.
Runtime receipts, LIMIT checkpoints, solver binaries, and cluster launch
contracts from that development artifact are deliberately not included.

## Exact finite scope

The vertex labels and formula are fixed as follows:

- the six-vertex core is `C = {0,1,2,3,4,5}`, split into shores
  `{0,1,2}` and `{3,4,5}`;
- `m = 6`, `D = 26`, owners are `6..11`, the exposed vertex is `12`,
  and the full inactive-tail domain is `13..30`;
- the active row sizes are exactly `(3,4,4,4,4,4)`, total deficiency is
  one, and the exposed row is exactly `{0}`;
- deleted label `s` is `0`, transfer label `r` is `1`, and `a = 0`;
- `q=1` represents the same-shore case and `q=3` the opposite-shore case;
- the original deficiency-one rows remain in the formula while the transfer
  pins require `q` outside row `s`, `a` outside row `r`, `q` inside row `r`,
  no retained factor using `q-owner(s)` or `a-owner(r)`, and at least one
  retained eligible outside anchor other than selector `(r,q)`;
- lazy cuts cover retained-label canonical Q0, old-outside Q1, and
  new-outside-only Q1 endpoints.

The kernel also retains the general compact/full anchor encodings, optional
inactive-tail first-use symmetry break, semantic decoder, complement
completion, optimized canonical endpoint enumeration, and independent
anchor-first Q0/old-Q1 cross-check needed to audit nearby m=6 configurations.

Variable allocation, physical edges, normalized clauses, witness order, cut
order, checkpoint/cut JSON, and gzip bytes are deterministic; per-round wall
timings are deliberately excluded from retained state. The protected baseline has
1,032 variables, 19,931 unpinned clauses, and 19,944 pinned clauses. Regression
digests are:

| Case | Baseline DIMACS SHA-256 |
|---|---|
| `q=1` | `10d430f18005abb27648f0885381687a68479622a332d09513030d7f8576e386` |
| `q=3` | `65a1031f2248af17755c91984f3d9d389c6d79dbe58d6a288cb34b1fa39f8890` |

## External tools and invocation

No solver or LRAT binary is distributed. Every external executable is given
by an explicit path and lowercase SHA-256; its bytes are checked at argument
validation and immediately before invocation. The independent checker and its
independent helper are also explicit, hash-pinned source inputs. Child checker
processes use `sys.executable`, preserving the already selected Python runtime.
The runtime configuration is an opaque caller-owned file whose hash becomes
part of the run identity.

Run from a toolkit source checkout, substituting reviewed paths and hashes:

```bash
python -m scripts.m6.protected_transfer \
  --q 1 \
  --run-dir RUN_ROOT/q1 \
  --solver SOLVER --solver-sha256 SOLVER_SHA256 \
  --independent-checker scripts/m6/check_protected_transfer.py \
  --independent-checker-sha256 CHECKER_SHA256 \
  --independent-helper scripts/m6/independent_static.py \
  --independent-helper-sha256 HELPER_SHA256 \
  --lrat-trim LRAT_TRIM --lrat-trim-sha256 LRAT_TRIM_SHA256 \
  --lrat-check LRAT_CHECK --lrat-check-sha256 LRAT_CHECK_SHA256 \
  --runtime-config RUNTIME_CONFIG \
  --runtime-config-sha256 RUNTIME_CONFIG_SHA256 \
  --max-invocation-seconds 600
```

Repeat the identical command to resume. Add
`--prove-on-candidate-unsat` only when the pinned solver supports
`--lrat --no-binary` and both pinned LRAT checkers are appropriate for its
proof output. Run the checker directly for a read-only reconstruction audit:

```bash
python scripts/m6/check_protected_transfer.py \
  --run-dir RUN_ROOT/q1 --output AUDIT_ROOT/q1-independent.json
```

The campaign and checker fail if Python optimization (`-O` or `-OO`) is
enabled because their internal invariant checks are part of the executable
audit contract.

## Status semantics and resume boundary

| Status | Meaning |
|---|---|
| `ready` | Baseline and immutable identity were written; no SAT round completed. |
| `running` | At least one complete SAT/cut round is checkpointed. |
| `limit` | This invocation hit its round or wall-time limit; it may be resumed. |
| `candidate_sat_no_retained_endpoint` | A semantically checked producer model was frozen but independent checking has not yet completed. |
| `verified_sat_no_retained_endpoint` | The checker independently rebuilt the CNF, all cuts, the assignment, and retained-endpoint absence. |
| `candidate_unsat` | The solver exhausted the current finite CNF, but no checked negative proof is claimed. |
| `verified_unsat` | A separate proof receipt records independent CNF reconstruction and successful checks by both pinned LRAT tools. |

The config, toolkit source hashes, checker/helper hashes, all external tool
hashes, runtime-config hash, and Python executable hash determine
`input_digest`. Resume validates that identity before altering retained files.
Each cut payload is written with deterministic gzip and atomic fsync/replace
before its checkpoint entry; one exact orphan payload can therefore be
reconciled after interruption. Foreign, missing, reordered, or hash-mismatched
cut files fail closed.

Run directories are private computational artifacts and do not belong in this
repository. A `limit` is not a result, and failure for either `q` case says
nothing about configurations outside the finite formula above.

## LRAT trust boundary

The SAT solver is an untrusted witness/proof producer. Positive assignments
are decoded and checked semantically, and the standalone checker deliberately
does not import the producer kernel. It independently allocates variables,
reconstructs every base clause and pin, validates every compact endpoint cut,
requires exact DIMACS clause-set equality, and re-enumerates terminal endpoint
semantics.

For a negative result, that independent reconstruction runs before a fresh
LRAT-producing solver invocation. Acceptance then requires both strict
`lrat-trim` verification and a separate `lrat-check` verification. Their paths
and hashes are recorded in the proof receipt. `verified_unsat` therefore means
only that the exact bounded CNF has this dual-checked proof under the recorded
checker/tool identities; it is not an unbounded graph-theoretic theorem.
