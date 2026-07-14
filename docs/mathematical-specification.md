# Mathematical specification

For a finite simple graph `G`, a total coloring assigns colors to vertices and
edges so that adjacent vertices, adjacent edges, and every incident
vertex--edge pair receive different colors. Verification therefore reduces to
checking a finite list of explicit conflicts.

## Auxiliary extension experiment

Set `k = Delta(G) + 1`. In the high-degree regime `2k >= |V(G)|`, an equitable
proper `k`-coloring has classes of size one or two. Its two-vertex classes form
a matching in the complement of `G`.

For each such partition, the toolkit constructs the draft's auxiliary graph:

- add one edge joining the two vertices of every two-vertex class;
- if singleton classes occur, add a new vertex and join it to every singleton;
- call the added family `M`.

Every original vertex is incident with exactly one member of `M`. A proper
edge coloring of the auxiliary graph in which all members of `M` have distinct
colors decodes directly to a total coloring of `G`.

The experimental question is existential over **all** equitable partitions.
The search distinguishes:

- a verified extension certificate;
- candidate failure after the reference search exhausts every partition,
  without an independently checked negative proof;
- incomplete/unknown because a partition or solver branch hit a limit.

The second state is evidence against the proposed reduction, not against the
Total Coloring Conjecture itself.

## Counting-obligation audit

The draft defines `h = max(q, p*c)` and seeks
`p + q < 3*h/(2*c)`. The toolkit treats that implication as a separately
testable arithmetic claim. No search result may silently assume it.
