# Research target and proof audit

This document is a computational specification, not a theorem announcement.
The associated Chen--Shan manuscript has not locked a formal main theorem.

## High-degree reduction

Let `G` be a nonempty finite simple graph of order `n` and maximum degree
`Delta`, and put

```text
D = Delta + 1,
a = n - D,
b = 2D - n.
```

In the regime `Delta >= n/2`, an equitable proper `D`-coloring has `a`
two-vertex classes and `b` singleton classes. Its two-vertex classes are a
matching in the complement of `G`. Conversely, every complement matching of
size `a` gives exactly such an unlabeled partition.

For one fixed partition, construct `H` by adding:

- the complement edge for every paired class; and
- a new vertex `x` joined to every singleton.

Call the added family `J`. Then:

- every original vertex is incident with exactly one edge of `J`;
- `|J| = D`;
- `Delta(H) = D`; and
- a proper edge coloring of `H` in which `J` is rainbow decodes directly to a
  total coloring of `G` with the same palette.

Thus the first computational target uses `D+2 = Delta+3` colors. The stronger
mode uses `D+1 = Delta+2` colors. The toolkit tests both, but does not label an
exhausted solver search as a proved negative result without a separately
checkable proof artifact.

The theorem-sufficient statement is existential over equitable partitions.
Failure for one partition can refute the stronger universal extension lemma,
but it cannot refute the existential reduction while another partition is
untested.

## Candidate extension statements

The smallest stable graph statement to investigate is:

> If `H` is a simple graph with `Delta(H) <= D` and `J` is a distinguished
> matching-plus-star family of `D` edges covering every noncenter vertex once,
> does `H` admit a proper `(D+2)`-edge coloring in which `J` is rainbow?

The `(D+1)` version is a stronger conjectural target. Neither statement is
asserted as proved here.

## Corrected missing-incidence obligation

Suppose one non-`J` edge is uncolored in a partial `(D+2)`-edge coloring, and
`T` contains both endpoints. If `u(v)` is the number of incident uncolored
edges and `m_T(alpha)` counts vertices of `T` missing color `alpha`, then

```text
sum_alpha m_T(alpha)
  = 2|T| + sum_{v in T}(D - degree_H(v)) + sum_{v in T} u(v)
  >= 2|T| + 2.
```

The draft's use of three automatically missing colors per tree vertex is an
off-by-one error: the added class edge raises an original vertex's auxiliary
degree by one.

If `P` colors are missed at least twice, `Q` colors are missed exactly once,
and repeated multiplicity is at most `r`, then the upper incidence count is
`rP+Q`. A numerical contradiction would follow from both

```text
max_alpha m_T(alpha) <= r,
|T| >= max(Q, rP).
```

The graph-theoretic work is to prove those two estimates. For the intended
size-two-class regime, improving the multiplicity cap to `r=2` is the most
promising current route.

The draft inequality `P+Q < 3 max(Q,cP)/(2c)` does not follow from its stated
hypotheses; `c=2` and `P=Q=1` is already a counterexample. The CLI's
`proof-audit` command checks these arithmetic implications using exact rational
arithmetic.

## Rainbow-safe Kempe exchanges

For an alpha-beta component, swapping preserves rainbow colors on `J` when the
component contains neither or both corresponding distinguished edges, or when
one of the two colors is unused on `J`. In particular, if beta is a spare
color, every alpha-beta component is safe: the unused color moves rather than
creating a duplicate. The proof plan should use this exact invariant instead
of requiring a chain to avoid `J` entirely.

## Literature boundary

- [Dalal--McDonald--Shan](https://arxiv.org/abs/2405.07382) prove the general
  bound `Delta + 2 ceil(n/(Delta+1))`, hence `Delta+4` in the half-density
  regime, and obtain `Delta+2` asymptotically for dense regular graphs.
- [Henderschedt--McDonald--Shan](https://arxiv.org/abs/2507.05548) obtain
  `Delta+2` under a dense minimum-degree hypothesis.
- [Hamilton--Hilton--Hind](https://doi.org/10.1006/jctb.1999.1902) are a
  required even-order comparison.
- [Edwards et al.](https://arxiv.org/abs/1407.4339) show why extending
  precolored matching edges is not routine; the family here has additional
  covering and rainbow structure.
- [Sebő](https://doi.org/10.1007/s00373-023-02712-1) is a modern annotated
  Tashkinov-tree reference. A constrained-critical transfer still has to be
  proved for the global rainbow condition.

Even order is not used by the reduction above. It should remain a separate
manuscript hypothesis only if a later structural lemma needs it.

