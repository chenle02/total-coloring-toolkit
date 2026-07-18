# Removing alpha-perfectness from the exact paired-hole input

This note documents a hand reduction and its bounded computational audit for
the exact `R = 5`, order-twelve, seven-colour paired-hole cell. It does not
prove that an ambient critical graph produces this cell, and the computation
is evidence rather than a theorem.

## Terminal-coverage lemma

Let `alpha` be unused by the fixed vertex colouring. For every non-alpha
colour `beta`, suppose the exact paired-hole blockage condition requires both
fixed-`beta` vertices to lie in the two `alpha`--`beta` components starting at
the two distinguished `beta` holes. Suppose also that neither distinguished
hole is itself fixed `beta`.

Then every fixed-`beta` vertex is incident with an `alpha` edge. Indeed, an
edge incident with a fixed-`beta` vertex cannot itself have colour `beta` by
proper total-colouring incidence. The vertex is distinct from the component's
starting hole, so belonging to that component requires an incident edge of
the two-colour subgraph. Its only possible colour is `alpha`.

In the normalized order-twelve cell, the six fixed colours

```text
p, q, a1, a2, b1, b2
```

each occur twice and their fixed-colour classes cover all twelve vertices.
Applying the preceding argument to all six colours shows that every vertex is
incident with an `alpha` edge. Properness makes the `alpha` edges a matching,
so they form a perfect matching.

Thus `AlphaPerfect` is redundant once all twelve blockage arms and the fixed
colour-pair normalization have been proved. It should be a derived lemma, not
an independent input assumption. This implication is generic: in any finite
partial total colouring, a family of fixed-colour terminal requirements that
covers every vertex forces alpha-perfectness in the same way.

## Exact all-partial frontier

The `all-partial` generator scope begins with every proper partial `alpha`
matching for each of the 64 canonical fan profiles. The only restrictions in
this initial stream are:

- no edge is `xy`; and
- the two endpoints of an edge have different fixed colours.

The six outside vertices have no named role. Sorting their forced fixed-colour
multiset is a sound quotient by their relabelling; the generator still
enumerates every matching on the canonical labelled representative. No
colour-role reflection, index swap, or Kempe-orbit quotient is used for the
all-partial frontier.

The exact stage partition is:

| Stage | Work units |
|---|---:|
| all proper partial `alpha` matchings | 5,529,408 |
| nonperfect, pruned by terminal coverage | 5,181,504 |
| perfect but using a forced fan edge | 135,904 |
| perfect, fan-disjoint, but joining one distinguished hole pair | 116,500 |
| admissible for non-alpha edge search | 95,500 |

The last three rows partition the 347,904 perfect matchings. A forced fan edge
must carry its prescribed non-alpha colour, so it cannot also carry `alpha`.
An `alpha` edge joining the two distinguished `beta` holes puts them in the
same `alpha`--`beta` component before any `beta` edge is chosen, contradicting
the blockage condition. Both are exact semantic prunes.

The independent script
`scripts/research/paired_hole_alpha_frontier_audit.py` recomputes these counts
with bit-mask dynamic programming. It does not import the streaming generator.
The generator itself records each prune class in its completion receipt. For
the nonperfect class it also maintains a sorted list of typed aggregate rows

```text
{terminal, fixed_colour, distinguished_holes, count}.
```

The row counts sum exactly to the nonperfect-prune counter. `run.json` starts
with the empty histogram; checkpoints and completion contain the observed
aggregate. No per-work-unit pruned raw state or terminal witness is emitted.

## Semantic verification boundary

No relaxation of the raw-state verifier is needed. It already reconstructs
all twelve blockage arms from the raw graph and returns out-of-scope when a
terminal is not covered. The new frontier audit covers work units that are
pruned before a complete raw state exists; emitted candidates continue through
the existing dependency-free semantic verifier and fresh colouring-certificate
checks. Candidate-record schema v2 binds every emitted record to the enclosing
run's configuration fingerprint while keeping the raw-state fingerprint
unchanged.

An exhausted all-partial campaign therefore supports only this finite claim:
within the normalized exact cell, dropping `AlphaPerfect` from the generator's
input creates no additional blocked states. It does not establish the ambient
extraction, the general-degree case, or the total-colouring conjecture.

## Future sealed Easley campaign

Use a fresh source archive and fresh home/scratch roots; do not reuse or alter
the sealed full-role v6 campaign.

Two canaries precede production:

1. The near-perfect matching at global ordinal `3,166,734` has five alpha
   edges, misses outside terminals 8 and 9, uses no forced fan edge, and joins
   no distinguished hole pair. Its singleton shard must record exactly one
   terminal-coverage prune and zero non-alpha matching generation.
2. The cross-first regression at global ordinal `3,175,482` is perfect and
   admissible. Its singleton shard must reproduce the three known raw-state
   fingerprints and their two-swap release pattern, while using the new work
   ordinals and run schema.

After both canaries pass detached readback, use 64 one-CPU shards with
`--alpha-scope all-partial --shard-count 64`. Every shard receives exactly
86,397 initial alpha work units. The expensive admissible counts range from
1,431 to 1,585 per shard, so the deterministic modulo split has less than an
11 percent max/min imbalance at that gate. Aggregate completion must reconcile
the four stage counts in the table exactly, then independently replay every
emitted raw state. One CPU, 4 GiB, and a two-hour bound match the predecessor
campaign envelope; a fresh timed canary must calibrate the final walltime
before production submission.
