"""Independent reference model for the finite D=8 dependency audits.

The production auditor is C++.  This module deliberately shares no code with
it: states are generated directly from the mathematical incidence definition,
and every reachability, dominator, robustness, and pivot calculation is done
in Python.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import combinations, permutations, product


@dataclass(frozen=True, slots=True)
class ActiveRole:
    """One labelled non-inert center-edge target."""

    name: str
    multiplicity: int
    mobile_triple: bool = False


@dataclass(frozen=True, slots=True)
class Profile:
    """A saturated rooted incidence profile.

    There are ``order - 1`` active colors.  In the normalized enumeration,
    role ``i`` has target vertex ``i + 1`` and the root is vertex zero.  The
    one inert column is not an active target; its source set is forced by the
    exact row totals.
    """

    name: str
    order: int
    active_roles: tuple[ActiveRole, ...]
    inert_multiplicity: int

    def __post_init__(self) -> None:
        if self.order < 2:
            raise ValueError("a profile needs at least two vertices")
        if len(self.active_roles) != self.order - 1:
            raise ValueError("active roles must label every nonroot target")
        if not 0 <= self.inert_multiplicity <= self.order:
            raise ValueError("invalid inert-column multiplicity")
        names = tuple(role.name for role in self.active_roles)
        if len(set(names)) != len(names):
            raise ValueError("active role names must be distinct")
        for role in self.active_roles:
            if not 1 <= role.multiplicity < self.order:
                raise ValueError("invalid active-column multiplicity")
            if role.mobile_triple and role.multiplicity != 3:
                raise ValueError("a robust candidate must have three predecessors")


@dataclass(frozen=True, slots=True)
class State:
    """A role-labelled oriented-hole dependency state."""

    root: int
    targets: tuple[int, ...]
    sources: tuple[int, ...]
    inert_sources: int


@dataclass(frozen=True, slots=True)
class AuditSummary:
    """Counts for normalized-root states in one profile."""

    profile: str
    order: int
    candidate_assignments: int
    root_outdegree_at_least_two: int
    root_reachable: int
    dependency_admissible: int
    incidence_admissible: int
    initial_all_mobile_triples_fragile: int
    pivot_resolved: int
    pivot_unresolved: int
    minimum_pivot_depths: tuple[tuple[int, int], ...]

    def counts_dict(self) -> dict[str, object]:
        return {
            "candidate_assignments": self.candidate_assignments,
            "dependency_admissible": self.dependency_admissible,
            "incidence_admissible": self.incidence_admissible,
            "initial_all_mobile_triples_fragile": self.initial_all_mobile_triples_fragile,
            "minimum_pivot_depth_histogram": {
                str(depth): count for depth, count in self.minimum_pivot_depths
            },
            "pivot_resolved": self.pivot_resolved,
            "pivot_unresolved": self.pivot_unresolved,
            "root_outdegree_at_least_two": self.root_outdegree_at_least_two,
            "root_reachable": self.root_reachable,
        }


@dataclass(frozen=True, order=True, slots=True)
class PivotOrbitCertificate:
    """Canonical predecessor/deficit data for one normalized state.

    The root is vertex zero and the target of role ``i`` is vertex ``i + 1``.
    Equal active roles may be permuted simultaneously with their target
    vertices.  ``predecessor_masks`` and ``deficit_mask`` are the
    lexicographically least data among all such permutations.
    """

    predecessor_masks: tuple[int, ...]
    deficit_mask: int


@dataclass(frozen=True, slots=True)
class PivotIsomorphismClass:
    """One equal-role isomorphism class of colored pivot orbits."""

    certificate: PivotOrbitCertificate
    normalized_initial_states: int
    colored_pivot_orbits: int


@dataclass(frozen=True, slots=True)
class UnresolvedPivotClassification:
    """Classification of normalized states with no robust state in their orbit."""

    unresolved_normalized_initial_states: int
    colored_pivot_orbits: int
    colored_pivot_orbit_size_histogram: tuple[tuple[int, int], ...]
    pivot_isomorphism_classes: tuple[PivotIsomorphismClass, ...]


PROFILES: dict[str, Profile] = {
    "d8-a-w5": Profile(
        name="d8-a-w5",
        order=5,
        active_roles=(
            ActiveRole("triple", 3, True),
            ActiveRole("double_1", 2),
            ActiveRole("double_2", 2),
            ActiveRole("double_3", 2),
        ),
        inert_multiplicity=2,
    ),
    "d8-b-w6": Profile(
        name="d8-b-w6",
        order=6,
        active_roles=(
            ActiveRole("triple_1", 3, True),
            ActiveRole("triple_2", 3, True),
            ActiveRole("double_1", 2),
            ActiveRole("double_2", 2),
            ActiveRole("spare", 1),
        ),
        inert_multiplicity=2,
    ),
    "d8-c-frozen-w5": Profile(
        name="d8-c-frozen-w5",
        order=5,
        active_roles=(
            ActiveRole("mobile_triple_1", 3, True),
            ActiveRole("mobile_triple_2", 3, True),
            ActiveRole("spare_1", 1),
            ActiveRole("spare_2", 1),
        ),
        inert_multiplicity=3,
    ),
    "d8-c-frozen-w6": Profile(
        name="d8-c-frozen-w6",
        order=6,
        active_roles=(
            ActiveRole("mobile_triple_1", 3, True),
            ActiveRole("mobile_triple_2", 3, True),
            ActiveRole("double", 2),
            ActiveRole("spare_1", 1),
            ActiveRole("spare_2", 1),
        ),
        inert_multiplicity=3,
    ),
    "d8-c-frozen-w7": Profile(
        name="d8-c-frozen-w7",
        order=7,
        active_roles=(
            ActiveRole("mobile_triple_1", 3, True),
            ActiveRole("mobile_triple_2", 3, True),
            ActiveRole("double_1", 2),
            ActiveRole("double_2", 2),
            ActiveRole("spare_1", 1),
            ActiveRole("spare_2", 1),
        ),
        inert_multiplicity=3,
    ),
    "d8-c-mobile-w6": Profile(
        name="d8-c-mobile-w6",
        order=6,
        active_roles=(
            ActiveRole("mobile_triple_1", 3, True),
            ActiveRole("mobile_triple_2", 3, True),
            ActiveRole("mobile_triple_3", 3, True),
            ActiveRole("spare_1", 1),
            ActiveRole("spare_2", 1),
        ),
        inert_multiplicity=2,
    ),
    "d8-c-mobile-w7": Profile(
        name="d8-c-mobile-w7",
        order=7,
        active_roles=(
            ActiveRole("mobile_triple_1", 3, True),
            ActiveRole("mobile_triple_2", 3, True),
            ActiveRole("mobile_triple_3", 3, True),
            ActiveRole("double", 2),
            ActiveRole("spare_1", 1),
            ActiveRole("spare_2", 1),
        ),
        inert_multiplicity=2,
    ),
}


def _vertices(mask: int) -> Iterator[int]:
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def _subset_masks(vertices: tuple[int, ...], size: int) -> tuple[int, ...]:
    return tuple(sum(1 << vertex for vertex in subset) for subset in combinations(vertices, size))


def outgoing_masks(profile: Profile, state: State) -> tuple[int, ...]:
    outgoing = [0] * profile.order
    for target, sources in zip(state.targets, state.sources, strict=True):
        for source in _vertices(sources):
            outgoing[source] |= 1 << target
    return tuple(outgoing)


def reachable_mask(
    profile: Profile,
    state: State,
    *,
    forbidden_vertex: int | None = None,
) -> int:
    """Return the vertices reachable from the state's root.

    ``forbidden_vertex`` is removed from the search, which computes a
    dominator region without enumerating paths.
    """

    all_vertices = (1 << profile.order) - 1
    allowed = all_vertices
    if forbidden_vertex is not None:
        allowed &= ~(1 << forbidden_vertex)
    seen = (1 << state.root) & allowed
    frontier = seen
    outgoing = outgoing_masks(profile, state)
    while frontier:
        next_frontier = 0
        for vertex in _vertices(frontier):
            next_frontier |= outgoing[vertex]
        next_frontier &= allowed & ~seen
        seen |= next_frontier
        frontier = next_frontier
    return seen


def dominator_region(profile: Profile, state: State, role_index: int) -> int:
    """Return the vertices whose every rooted path contains the target."""

    target = state.targets[role_index]
    all_vertices = (1 << profile.order) - 1
    return all_vertices & ~reachable_mask(profile, state, forbidden_vertex=target)


def is_robust(profile: Profile, state: State, role_index: int) -> bool:
    """Test the exact three-predecessor dominator criterion."""

    role = profile.active_roles[role_index]
    if not role.mobile_triple:
        return False
    region = dominator_region(profile, state, role_index)
    return state.sources[role_index] & region == 0


def has_robust_mobile_triple(profile: Profile, state: State) -> bool:
    return any(
        is_robust(profile, state, index)
        for index, role in enumerate(profile.active_roles)
        if role.mobile_triple
    )


def pivot(profile: Profile, state: State, role_index: int) -> State | None:
    """Perform the actual fan pivot for one root dependency arc."""

    root_bit = 1 << state.root
    sources = state.sources[role_index]
    if sources & root_bit == 0:
        return None
    old_target = state.targets[role_index]
    targets = list(state.targets)
    new_sources = list(state.sources)
    targets[role_index] = state.root
    new_sources[role_index] = (sources & ~root_bit) | (1 << old_target)
    return State(
        root=old_target,
        targets=tuple(targets),
        sources=tuple(new_sources),
        inert_sources=state.inert_sources,
    )


def pivot_successors(profile: Profile, state: State) -> Iterator[State]:
    for role_index in range(len(profile.active_roles)):
        successor = pivot(profile, state, role_index)
        if successor is not None:
            yield successor


def pivot_orbit(profile: Profile, start: State) -> frozenset[State]:
    """Return the colored orbit generated by every legal root pivot.

    Active roles remain labelled: no color or equal-role permutations are
    taken here.  A legal pivot is an involution, so breadth-first traversal of
    the successors computes the whole connected component.
    """

    orbit = {start}
    queue = deque([start])
    while queue:
        state = queue.popleft()
        for successor in pivot_successors(profile, state):
            if successor not in orbit:
                orbit.add(successor)
                queue.append(successor)
    return frozenset(orbit)


def _equal_role_groups(profile: Profile) -> tuple[tuple[int, ...], ...]:
    groups: dict[tuple[int, bool], list[int]] = {}
    for role_index, role in enumerate(profile.active_roles):
        signature = (role.multiplicity, role.mobile_triple)
        groups.setdefault(signature, []).append(role_index)
    return tuple(tuple(group) for group in groups.values())


def _relabel_mask(mask: int, vertex_mapping: tuple[int, ...]) -> int:
    relabelled = 0
    for vertex in _vertices(mask):
        relabelled |= 1 << vertex_mapping[vertex]
    return relabelled


def canonicalize_equal_roles(profile: Profile, state: State) -> PivotOrbitCertificate:
    """Normalize a state modulo permutations of roles with equal semantics.

    The root is sent to zero.  For each allowed role permutation, the target
    of the role in position ``i`` is sent to vertex ``i + 1``.  The same
    vertex relabelling is applied to every predecessor mask and to the exact
    row-deficit mask (``inert_sources``).  The least resulting pair is the
    canonical certificate.
    """

    labelled_vertices = (state.root, *state.targets)
    if sorted(labelled_vertices) != list(range(profile.order)):
        raise ValueError("a state root and targets must label every vertex exactly once")

    groups = _equal_role_groups(profile)
    choices = tuple(tuple(permutations(group)) for group in groups)
    certificates: list[PivotOrbitCertificate] = []
    for selected_permutations in product(*choices):
        old_role_at_new_position = list(range(len(profile.active_roles)))
        for new_positions, old_roles in zip(groups, selected_permutations, strict=True):
            for new_position, old_role in zip(new_positions, old_roles, strict=True):
                old_role_at_new_position[new_position] = old_role

        vertex_mapping = [-1] * profile.order
        vertex_mapping[state.root] = 0
        for new_role, old_role in enumerate(old_role_at_new_position):
            vertex_mapping[state.targets[old_role]] = new_role + 1
        mapping = tuple(vertex_mapping)
        certificates.append(
            PivotOrbitCertificate(
                predecessor_masks=tuple(
                    _relabel_mask(state.sources[old_role], mapping)
                    for old_role in old_role_at_new_position
                ),
                deficit_mask=_relabel_mask(state.inert_sources, mapping),
            )
        )
    return min(certificates)


def canonicalize_pivot_orbit(profile: Profile, orbit: frozenset[State]) -> PivotOrbitCertificate:
    """Return the least equal-role certificate over a colored pivot orbit."""

    if not orbit:
        raise ValueError("a pivot orbit must be nonempty")
    return min(canonicalize_equal_roles(profile, state) for state in orbit)


def _row_inert_mask(profile: Profile, active_outdegrees: tuple[int, ...]) -> int | None:
    inert_mask = 0
    for vertex, outdegree in enumerate(active_outdegrees):
        required = (3 if vertex == 0 else 2) - outdegree
        if required not in (0, 1):
            return None
        if required:
            inert_mask |= 1 << vertex
    if inert_mask.bit_count() != profile.inert_multiplicity:
        return None
    return inert_mask


def _candidate_sources(profile: Profile) -> Iterator[tuple[int, ...]]:
    targets = tuple(range(1, profile.order))
    choices = []
    for target, role in zip(targets, profile.active_roles, strict=True):
        vertices = tuple(vertex for vertex in range(profile.order) if vertex != target)
        choices.append(_subset_masks(vertices, role.multiplicity))
    yield from product(*choices)


def enumerate_states(profile: Profile) -> Iterator[State]:
    """Generate every exact normalized-root state for ``profile``."""

    targets = tuple(range(1, profile.order))
    all_vertices = (1 << profile.order) - 1
    for sources in _candidate_sources(profile):
        provisional = State(root=0, targets=targets, sources=sources, inert_sources=0)
        outgoing = outgoing_masks(profile, provisional)
        inert_mask = _row_inert_mask(profile, tuple(mask.bit_count() for mask in outgoing))
        if inert_mask is None:
            continue
        state = State(
            root=0,
            targets=targets,
            sources=tuple(sources),
            inert_sources=inert_mask,
        )
        if reachable_mask(profile, state) == all_vertices:
            yield state


def _orbit_distances(profile: Profile, start: State) -> dict[State, int | None]:
    """Compute exact distance to a robust state for the whole pivot orbit."""

    orbit = pivot_orbit(profile, start)

    robust_states = [state for state in orbit if has_robust_mobile_triple(profile, state)]
    if not robust_states:
        return {state: None for state in orbit}

    distances = {state: 0 for state in robust_states}
    queue = deque(robust_states)
    while queue:
        state = queue.popleft()
        depth = distances[state]
        for successor in pivot_successors(profile, state):
            if successor not in distances:
                distances[successor] = depth + 1
                queue.append(successor)
    return {state: distances[state] for state in orbit}


def minimum_pivot_depth(profile: Profile, state: State) -> int | None:
    return _orbit_distances(profile, state)[state]


def classify_unresolved_pivot_orbits(profile: Profile) -> UnresolvedPivotClassification:
    """Classify exact normalized states whose colored orbit stays fragile.

    A colored orbit retains all active-role labels.  The final quotient first
    normalizes every state by simultaneous permutations of roles having the
    same multiplicity and mobility status, then takes the least certificate
    across its pivot orbit.  Counts of normalized initial states are retained
    because several such states could, in principle, lie in one colored
    orbit.
    """

    resolved_states: set[State] = set()
    unresolved_orbit_index: dict[State, int] = {}
    unresolved_orbits: list[frozenset[State]] = []
    normalized_initial_counts: list[int] = []

    for state in enumerate_states(profile):
        if has_robust_mobile_triple(profile, state) or state in resolved_states:
            continue
        orbit_index = unresolved_orbit_index.get(state)
        if orbit_index is not None:
            normalized_initial_counts[orbit_index] += 1
            continue

        orbit = pivot_orbit(profile, state)
        if any(has_robust_mobile_triple(profile, member) for member in orbit):
            resolved_states.update(orbit)
            continue

        orbit_index = len(unresolved_orbits)
        unresolved_orbits.append(orbit)
        normalized_initial_counts.append(1)
        for member in orbit:
            unresolved_orbit_index[member] = orbit_index

    orbit_size_counts = Counter(map(len, unresolved_orbits))
    class_counts: dict[PivotOrbitCertificate, list[int]] = {}
    for orbit, normalized_initial_count in zip(
        unresolved_orbits,
        normalized_initial_counts,
        strict=True,
    ):
        certificate = canonicalize_pivot_orbit(profile, orbit)
        counts = class_counts.setdefault(certificate, [0, 0])
        counts[0] += normalized_initial_count
        counts[1] += 1

    classes = tuple(
        PivotIsomorphismClass(
            certificate=certificate,
            normalized_initial_states=counts[0],
            colored_pivot_orbits=counts[1],
        )
        for certificate, counts in sorted(class_counts.items())
    )
    return UnresolvedPivotClassification(
        unresolved_normalized_initial_states=sum(normalized_initial_counts),
        colored_pivot_orbits=len(unresolved_orbits),
        colored_pivot_orbit_size_histogram=tuple(sorted(orbit_size_counts.items())),
        pivot_isomorphism_classes=classes,
    )


def audit_profile(profile: Profile) -> AuditSummary:
    candidate_assignments = 0
    root_outdegree_at_least_two = 0
    root_reachable = 0
    dependency_admissible = 0
    incidence_admissible = 0
    initially_all_fragile = 0
    pivot_resolved = 0
    pivot_unresolved = 0
    depth_counts: Counter[int] = Counter()
    depth_cache: dict[State, int | None] = {}
    targets = tuple(range(1, profile.order))
    all_vertices = (1 << profile.order) - 1

    for sources in _candidate_sources(profile):
        candidate_assignments += 1
        provisional = State(root=0, targets=targets, sources=sources, inert_sources=0)
        outgoing = outgoing_masks(profile, provisional)
        root_outdegree_ok = outgoing[0].bit_count() >= 2
        reachable = reachable_mask(profile, provisional) == all_vertices
        root_outdegree_at_least_two += root_outdegree_ok
        root_reachable += reachable
        if not (root_outdegree_ok and reachable):
            continue
        dependency_admissible += 1
        inert_mask = _row_inert_mask(profile, tuple(mask.bit_count() for mask in outgoing))
        if inert_mask is None:
            continue
        incidence_admissible += 1
        state = State(
            root=0,
            targets=targets,
            sources=sources,
            inert_sources=inert_mask,
        )
        if has_robust_mobile_triple(profile, state):
            depth_counts[0] += 1
            continue
        initially_all_fragile += 1
        if state not in depth_cache:
            depth_cache.update(_orbit_distances(profile, state))
        depth = depth_cache[state]
        if depth is None:
            pivot_unresolved += 1
        else:
            pivot_resolved += 1
            depth_counts[depth] += 1

    return AuditSummary(
        profile=profile.name,
        order=profile.order,
        candidate_assignments=candidate_assignments,
        root_outdegree_at_least_two=root_outdegree_at_least_two,
        root_reachable=root_reachable,
        dependency_admissible=dependency_admissible,
        incidence_admissible=incidence_admissible,
        initial_all_mobile_triples_fragile=initially_all_fragile,
        pivot_resolved=pivot_resolved,
        pivot_unresolved=pivot_unresolved,
        minimum_pivot_depths=tuple(sorted(depth_counts.items())),
    )
