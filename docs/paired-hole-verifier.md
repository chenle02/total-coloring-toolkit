# Paired-hole semantic verifier

This module is a verifier for one exact local proof residue, not a graph
generator, a solver, or a theorem prover. Its input contract is the public
`total-coloring.paired-hole-state.v1` schema. A generator should emit only:

- the canonical numbered simple graph;
- the degree parameter and `R+2` palette size;
- fixed vertex colors and graph-edge-aligned partial edge colors;
- one distinguished uncolored edge;
- the vertex-unused color `alpha`; and
- pointers naming `x`, `y`, and the two proposed satellites in each orientation.

The pointers are claims, not trusted derived data. The dependency-free
`verify_paired_hole_state` function independently checks the partial total
coloring and reconstructs all of the following:

- missing colors at every vertex;
- both full fan-reachability closures, their two-satellite sizes, and weighted
  surplus zero;
- the matching maximum-degree core and size-at-most-two fixed color classes;
- the pairwise-disjoint `A`, `B`, `p`, `q`, and `alpha` roles;
- aligned versus nonaligned satellite fixed-color profiles;
- all twelve full `alpha`--`beta` components and their fixed-color terminal
  blockages;
- the `R+1` single-edge and `R+2` longer-path terminal degree bounds;
- replayed terminal-release witnesses, only when the alternating path has a
  preceding beta edge (at least three edges);
- linked versus distinct topology for every one of the four `A x B` color
  pairs;
- both endpoint-rooted full-component swap attempts for every distinct pair;
  and
- any direct or bounded two-component-orbit completion as a fresh independent
  total-coloring certificate.

Use the schema through the installed resource API rather than a repository
path:

```python
from total_coloring.paired_hole import PairedHoleState, verify_paired_hole_state
from total_coloring.schema_resources import SchemaName, read_schema_json

schema = read_schema_json(SchemaName.PAIRED_HOLE_STATE_V1)
state = PairedHoleState.from_json(raw_state)
receipt = verify_paired_hole_state(state)
```

## Status meanings

- `verified_one_swap_exit`: at least one of the four independently derived
  cross-color components has a legal full-component swap, a common endpoint
  hole, and a valid fill certificate.
- `verified_two_swap_orbit_exit`: no direct cross exit was found, but the
  bounded role-color orbit contains two legal full-component swaps followed by
  a certificate-verified fill. The first move may use any pair of the seven
  role colors and is recomputed from the raw state over every graph component
  (including components disjoint from the six named fan vertices).
  The second `A x B` component is recomputed from the verified intermediate
  coloring. This documented two-move verifier orbit requires the two move
  pairs to share exactly one role color; it does not advertise negative
  exhaustion beyond that scope.
- `verified_cross_terminal_release_exit`: a failed direct cross swap has a
  replayable terminal-edge release with a preceding opposite-color edge; the
  prefix swap creates a common endpoint hole and the fill certificate passes.
- `verified_alpha_terminal_release`: an alpha--beta terminal move was replayed
  and releases alpha at its fan-role hole. This is deliberately a local
  structural status, not a completed total-coloring certificate; the bounded
  orbit search does not run until these local exits have been eliminated.
- `fully_blocked_candidate`: no direct cross exit, cross-terminal completion,
  alpha-terminal local release, or verified bounded orbit was found. This is
  only a candidate structural residue. It is not an impossibility certificate,
  an exhaustive statement beyond the documented orbit, or a theorem.
- `invalid_state`: the input is malformed, its role pointers are false in an
  otherwise exact closure, or it is not a proper one-hole partial total
  coloring.
- `unsupported_out_of_scope`: the partial coloring is meaningful but does not
  satisfy the exact matching-core, fan, surplus, color-role, or twelve-blockage
  envelope.

The result object retains the derived fan, profile, blockage, terminal,
cross-component, canonical component-walk, orbit-detachment, and certificate
data. Census code should count statuses only
after preserving these semantic fields; reducing them to an unverified Boolean
would discard the structural evidence needed for the next proof lemma.

The exact-cell generator also has an `all-partial` alpha scope. It does not
weaken this verifier. Instead, it computes the first uncovered fixed-colour
terminal for each nonperfect alpha matching and increments a canonical
aggregate histogram before non-alpha edge generation. It does not emit one
raw witness per prune. The
[terminal-coverage note](paired-hole-alpha-terminal-coverage.md) proves why
all twelve blockage arms force alpha-perfectness and records the independent
finite frontier audit.

Candidate record schema v2 retains the raw-state-only `candidate_fingerprint`
and adds a top-level `run_config_fingerprint`. Readback must compare that field
with the enclosing completion receipt's `config_fingerprint`; the work-unit
record also states its `alpha_scope` explicitly.

When several bounded two-swap exits exist, the retained certificate is chosen
deterministically: `alpha`--role first-move pairs precede cross pairs, followed
by the first component's canonical edge-index tuple, the second cross-color
pair, and the rooted second-component edge-index tuple. The orbit topology
records linkedness before and after the first move, the pre-move cross
component, the first component's edges in the uniquely shared role color, and
their exact intersection. A path walk starts at its smaller endpoint; a cycle
starts at its smallest vertex and uses the lexicographically smaller
orientation.

## Dependency-free JSONL readback

The following standalone snippet streams generator records from standard
input. Pass the enclosing completion receipt's `config_fingerprint` as its one
argument. It treats `raw_state` as the sole semantic input, checks the
raw-state fingerprint without folding run metadata into it, binds every record
to that run configuration, replays the verifier, and emits one canonical
aggregate receipt. Redirect its output to a file when reading back a cluster
shard.

```python
from __future__ import annotations

import sys
from collections import Counter
from typing import cast

from total_coloring.graph import canonical_json_bytes, strict_json_loads
from total_coloring.paired_hole import PairedHoleState, verify_paired_hole_state

counts: Counter[str] = Counter()
failures: list[dict[str, object]] = []
if len(sys.argv) != 2:
    raise SystemExit("usage: readback.py RUN_CONFIG_FINGERPRINT")
expected_run_config_fingerprint = sys.argv[1]
for line_index, line in enumerate(sys.stdin.buffer):
    try:
        record = strict_json_loads(line)
        if not isinstance(record, dict):
            raise ValueError("record is not an object")
        if record.get("schema_version") != "total-coloring.paired-hole-orbit-candidate.v2":
            raise ValueError("candidate record schema mismatch")
        if record.get("run_config_fingerprint") != expected_run_config_fingerprint:
            raise ValueError("run config fingerprint mismatch")
        state = PairedHoleState.from_dict(cast(dict[str, object], record["raw_state"]))
        expected = record["candidate_fingerprint"]
        if expected != state.fingerprint:
            raise ValueError("state fingerprint mismatch")
        result = verify_paired_hole_state(state)
        counts[result.status.value] += 1
    except (KeyError, TypeError, ValueError) as exc:
        failures.append({"line_index": line_index, "detail": str(exc)})

receipt = {
    "run_config_fingerprint": expected_run_config_fingerprint,
    "record_count": sum(counts.values()) + len(failures),
    "counts": dict(sorted(counts.items())),
    "failures": failures,
}
sys.stdout.buffer.write(canonical_json_bytes(receipt) + b"\n")
raise SystemExit(bool(failures))
```

This aggregate is an operational readback receipt, not a census-completion
claim. A production census must additionally bind its generator scope,
software identity, shard coverage, durable artifacts, and exact end of stream.
