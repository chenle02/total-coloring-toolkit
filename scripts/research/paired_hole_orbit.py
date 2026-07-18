#!/usr/bin/env python3
"""Generate paired-hole states and propose bounded recolouring orbits.

This is an *untrusted research generator*, not a verifier and not a proof.  It
normalizes the first possible two-sided ``q = 2`` zero-surplus scale to

``R = 5``, vertices ``x,y,u,v,w,z`` followed by six anonymous outside
vertices, colours ``alpha,p_x,p_y,a_1,a_2,b_1,b_2 = 0,...,6``, and the four
forced fan edges.  It then enumerates proper partial edge-colourings which
block the two distinguished ``alpha`` paths for every non-alpha colour.

Two profile scopes are deliberately separate:

* ``frozen`` reproduces the exact twelve-vertex regression which has eight
  labelled states.
* ``canonical-fan`` enumerates all 64 fixed-colour allocations on the four
  named fan vertices, modulo permutations of the six otherwise anonymous
  outside vertices.  It generates colour matchings directly.  Outside
  missing sizes ``2`` through ``5`` correspond respectively to three through
  zero non-alpha incidences in addition to the alpha edge.  Selecting only
  size ``2`` recovers the degree-saturated subcase.

The broad scopes are very large.  Any resource cap produces a bounded receipt,
and only exhaustion of the configured input writes ``completion.json``.
Records contain raw candidate states and proposed component swaps.  Neither a
proposal nor failure to find one is independently certified here.

The deterministic development canary ``--shard-count 347904 --shard-index
199186 --outside-missing-sizes 2`` selects exactly one alpha matching (the
post-terminal-lock regression's matching).  Exhausting that shard is complete
for one alpha work unit only, never for the 347,904-unit canonical-fan input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from itertools import combinations, pairwise, product
from pathlib import Path
from typing import Literal

ALPHA = 0
PALETTE = tuple(range(7))
NON_ALPHA = tuple(range(1, 7))
ORDER = 12
XY = (0, 1)

# Labels: x,y,u,v,w,z followed by six vertices having no fan role.
FAN_EDGES: Mapping[int, tuple[int, int]] = {
    3: (1, 4),
    4: (1, 5),
    5: (0, 2),
    6: (0, 3),
}
DISTINGUISHED_HOLES: Mapping[int, tuple[int, int]] = {
    1: (2, 3),
    2: (4, 5),
    3: (0, 5),
    4: (0, 4),
    5: (1, 3),
    6: (1, 2),
}
SPECIAL_MISSING = (
    frozenset({3, 4}),
    frozenset({5, 6}),
    frozenset({1, 6}),
    frozenset({1, 5}),
    frozenset({2, 4}),
    frozenset({2, 3}),
)
FROZEN_VERTEX_COLOURS = (1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5, 6)
FROZEN_MISSING = (
    *SPECIAL_MISSING,
    frozenset({3, 4}),
    frozenset({5, 6}),
    frozenset({2, 6}),
    frozenset({2, 5}),
    frozenset({1, 4}),
    frozenset({1, 3}),
)

RUN_SCHEMA = "total-coloring.paired-hole-orbit-run.v1"
RECORD_SCHEMA = "total-coloring.paired-hole-orbit-candidate.v1"
ProfileScope = Literal["frozen", "canonical-fan"]
RunStatus = Literal[
    "running",
    "complete_generation",
    "bounded_generation",
    "interrupted",
]


def edge(left: int, right: int) -> tuple[int, int]:
    """Return an undirected edge in canonical endpoint order."""

    return (left, right) if left < right else (right, left)


def historical_move_pairs() -> tuple[tuple[int, int], ...]:
    """Return the alpha-role and A-by-B pairs used before this wave."""

    return tuple((ALPHA, beta) for beta in NON_ALPHA) + tuple(
        (a, b) for a in (3, 4) for b in (5, 6)
    )


def configured_first_move_pairs() -> tuple[tuple[int, int], ...]:
    """Return every role pair after the historical stable prefix."""

    preferred = historical_move_pairs()
    preferred_set = set(preferred)
    return preferred + tuple(pair for pair in combinations(PALETTE, 2) if pair not in preferred_set)


def canonical_json_payload_bytes(value: object) -> bytes:
    """Encode canonical semantic JSON without transport whitespace."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_bytes(value: object) -> bytes:
    """Encode one canonical JSON-lines transport record."""

    return canonical_json_payload_bytes(value) + b"\n"


def sha256_bytes(value: object) -> str:
    return hashlib.sha256(canonical_json_payload_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json_write(path: Path, value: object) -> None:
    """Write canonical JSON through fsync and atomic replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(canonical_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def canonical_vertex_colour_profiles() -> tuple[tuple[int, ...], ...]:
    """Return the 64 normalized fixed-colour allocations.

    The fan vertices ``u,v`` may use ``2,3,4`` and ``w,z`` may use ``1,5,6``.
    Capacity two excludes only ``u=v=2`` and ``w=z=1``.  Once those four
    colours are fixed, the outside multiset is forced; sorting it quotients
    only permutations of vertices which have no named role.
    """

    profiles: list[tuple[int, ...]] = []
    for u, v, w, z in product((2, 3, 4), (2, 3, 4), (1, 5, 6), (1, 5, 6)):
        prefix = (1, 2, u, v, w, z)
        if any(prefix.count(colour) > 2 for colour in NON_ALPHA):
            continue
        outside = tuple(colour for colour in NON_ALPHA for _ in range(2 - prefix.count(colour)))
        profiles.append((*prefix, *outside))
    return tuple(profiles)


def perfect_matchings(
    vertices: tuple[int, ...],
    vertex_colours: tuple[int, ...],
) -> Iterator[tuple[tuple[int, int], ...]]:
    """Yield every proper perfect matching once in lexicographic recursion order."""

    if not vertices:
        yield ()
        return
    left = vertices[0]
    for index in range(1, len(vertices)):
        right = vertices[index]
        candidate = edge(left, right)
        if candidate == XY or vertex_colours[left] == vertex_colours[right]:
            continue
        remaining = vertices[1:index] + vertices[index + 1 :]
        for tail in perfect_matchings(remaining, vertex_colours):
            yield (candidate, *tail)


def partial_matchings(
    vertices: tuple[int, ...],
    vertex_colours: tuple[int, ...],
    forbidden_edges: frozenset[tuple[int, int]],
) -> Iterator[tuple[tuple[int, int], ...]]:
    """Yield every matching on an arbitrary subset of ``vertices`` once."""

    if not vertices:
        yield ()
        return
    left = vertices[0]
    yield from partial_matchings(vertices[1:], vertex_colours, forbidden_edges)
    for index in range(1, len(vertices)):
        right = vertices[index]
        candidate = edge(left, right)
        if (
            candidate == XY
            or candidate in forbidden_edges
            or vertex_colours[left] == vertex_colours[right]
        ):
            continue
        remaining = vertices[1:index] + vertices[index + 1 :]
        for tail in partial_matchings(remaining, vertex_colours, forbidden_edges):
            yield (candidate, *tail)


def _missing_options(
    vertex_colour: int,
    sizes: tuple[int, ...],
) -> tuple[frozenset[int], ...]:
    available = tuple(colour for colour in NON_ALPHA if colour != vertex_colour)
    return tuple(frozenset(choice) for size in sizes for choice in combinations(available, size))


def _parity_mask(colours: frozenset[int]) -> int:
    result = 0
    for colour in colours:
        result ^= 1 << (colour - 1)
    return result


def outside_missing_profiles(
    vertex_colours: tuple[int, ...],
    sizes: tuple[int, ...],
) -> Iterator[tuple[frozenset[int], ...]]:
    """Yield every parity-admissible outside missing profile once.

    A non-alpha colour must be incident with an even number of vertices in any
    union of colour matchings.  Since exactly two vertices are fixed with each
    colour, this is equivalent to even missing multiplicity for every colour.
    The suffix parity table prunes choices without changing the output set.
    """

    if len(vertex_colours) != ORDER:
        raise ValueError("vertex-colour profile must have order twelve")
    if not sizes or any(size < 2 or size > 5 for size in sizes):
        raise ValueError("outside missing sizes must be chosen from 2,3,4,5")
    if tuple(sorted(set(sizes))) != sizes:
        raise ValueError("outside missing sizes must be strictly increasing")

    options = tuple(_missing_options(vertex_colours[vertex], sizes) for vertex in range(6, 12))
    suffix_masks: list[set[int]] = [set() for _ in range(7)]
    suffix_masks[6].add(0)
    for index in range(5, -1, -1):
        suffix_masks[index] = {
            _parity_mask(choice) ^ tail
            for choice in options[index]
            for tail in suffix_masks[index + 1]
        }

    prefix_mask = 0
    for missing in SPECIAL_MISSING:
        prefix_mask ^= _parity_mask(missing)
    selected: list[frozenset[int]] = []

    def visit(index: int, parity: int) -> Iterator[tuple[frozenset[int], ...]]:
        if index == 6:
            if parity == 0:
                yield (*SPECIAL_MISSING, *selected)
            return
        for choice in options[index]:
            updated = parity ^ _parity_mask(choice)
            if updated not in suffix_masks[index + 1]:
                continue
            selected.append(choice)
            yield from visit(index + 1, updated)
            selected.pop()

    yield from visit(0, prefix_mask)


def configured_missing_profiles(
    profile_scope: ProfileScope,
    vertex_colours: tuple[int, ...],
    sizes: tuple[int, ...],
) -> Iterator[tuple[frozenset[int], ...]]:
    if profile_scope == "frozen":
        if vertex_colours != FROZEN_VERTEX_COLOURS or sizes != (2,):
            raise ValueError("the frozen scope requires its exact vertex and missing profile")
        yield FROZEN_MISSING
        return
    yield from outside_missing_profiles(vertex_colours, sizes)


def colour_matchings(
    colour: int,
    vertex_colours: tuple[int, ...],
    missing: tuple[frozenset[int], ...],
) -> Iterator[tuple[tuple[int, int], ...]]:
    """Yield all proper matchings forced by one fixed missing profile."""

    vertices = tuple(
        vertex
        for vertex in range(ORDER)
        if vertex_colours[vertex] != colour and colour not in missing[vertex]
    )
    if len(vertices) % 2:
        return
    required = FAN_EDGES.get(colour)
    for matching in perfect_matchings(vertices, vertex_colours):
        canonical = tuple(sorted(matching))
        if required is None or required in canonical:
            yield canonical


def component_vertices(
    alpha_matching: tuple[tuple[int, int], ...],
    beta_matching: tuple[tuple[int, int], ...],
    start: int,
) -> frozenset[int]:
    adjacency: list[list[int]] = [[] for _ in range(ORDER)]
    for left, right in (*alpha_matching, *beta_matching):
        adjacency[left].append(right)
        adjacency[right].append(left)
    seen = {start}
    stack = [start]
    while stack:
        vertex = stack.pop()
        for neighbor in adjacency[vertex]:
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return frozenset(seen)


def blocks_distinguished_holes(
    alpha_matching: tuple[tuple[int, int], ...],
    beta: int,
    beta_matching: tuple[tuple[int, int], ...],
    vertex_colours: tuple[int, ...],
) -> bool:
    """Test the configured two-hole alpha/beta blockage condition."""

    first_hole, second_hole = DISTINGUISHED_HOLES[beta]
    first_component = component_vertices(alpha_matching, beta_matching, first_hole)
    second_component = component_vertices(alpha_matching, beta_matching, second_hole)
    if first_component == second_component:
        return False
    terminals = {
        vertex for vertex, fixed_colour in enumerate(vertex_colours) if fixed_colour == beta
    }
    return (
        len(terminals) == 2
        and len(first_component & terminals) == 1
        and len(second_component & terminals) == 1
        and terminals <= first_component | second_component
    )


@dataclass(slots=True)
class ChoiceCounts:
    edge_colour_matchings_generated: int = 0
    edge_colour_matchings_blocked: int = 0
    edge_colour_matching_branches: int = 0


def blocked_matching_choices(
    alpha_matching: tuple[tuple[int, int], ...],
    vertex_colours: tuple[int, ...],
    missing: tuple[frozenset[int], ...],
    counts: ChoiceCounts | None = None,
) -> dict[int, tuple[tuple[tuple[int, int], ...], ...]]:
    """Return each colour's alpha-blocked choices, excluding alpha-edge reuse."""

    alpha_edges = set(alpha_matching)
    choices: dict[int, tuple[tuple[tuple[int, int], ...], ...]] = {}
    for beta in NON_ALPHA:
        allowed: list[tuple[tuple[int, int], ...]] = []
        for matching in colour_matchings(beta, vertex_colours, missing):
            if counts is not None:
                counts.edge_colour_matchings_generated += 1
            if not alpha_edges.isdisjoint(matching):
                continue
            if blocks_distinguished_holes(
                alpha_matching,
                beta,
                matching,
                vertex_colours,
            ):
                allowed.append(matching)
                if counts is not None:
                    counts.edge_colour_matchings_blocked += 1
        choices[beta] = tuple(allowed)
    return choices


def direct_colour_matchings(
    beta: int,
    alpha_matching: tuple[tuple[int, int], ...],
    vertex_colours: tuple[int, ...],
) -> Iterator[tuple[tuple[int, int], ...]]:
    """Generate beta matchings without first choosing outside missing pairs.

    The two beta-fixed terminals and the two distinguished beta holes cannot
    carry a beta edge.  Every other vertex is optional at this stage; the
    global incidence backtrack later enforces the configured outside
    missing-set sizes.
    """

    terminals = {
        vertex for vertex, fixed_colour in enumerate(vertex_colours) if fixed_colour == beta
    }
    forbidden_vertices = terminals | set(DISTINGUISHED_HOLES[beta])
    available = tuple(vertex for vertex in range(ORDER) if vertex not in forbidden_vertices)
    alpha_edges = frozenset(alpha_matching)
    required = FAN_EDGES.get(beta)
    prefix: tuple[tuple[int, int], ...] = ()
    if required is not None:
        if (
            required in alpha_edges
            or required == XY
            or required[0] not in available
            or required[1] not in available
            or vertex_colours[required[0]] == vertex_colours[required[1]]
        ):
            return
        prefix = (required,)
        available = tuple(vertex for vertex in available if vertex not in required)
    for tail in partial_matchings(available, vertex_colours, alpha_edges):
        yield tuple(sorted((*prefix, *tail)))


def direct_blocked_matching_choices(
    alpha_matching: tuple[tuple[int, int], ...],
    vertex_colours: tuple[int, ...],
    counts: ChoiceCounts | None = None,
) -> dict[int, tuple[tuple[tuple[int, int], ...], ...]]:
    """Return alpha-blocked direct matching choices for a configured degree scope."""

    choices: dict[int, tuple[tuple[tuple[int, int], ...], ...]] = {}
    for beta in NON_ALPHA:
        allowed: list[tuple[tuple[int, int], ...]] = []
        for matching in direct_colour_matchings(beta, alpha_matching, vertex_colours):
            if counts is not None:
                counts.edge_colour_matchings_generated += 1
            if blocks_distinguished_holes(
                alpha_matching,
                beta,
                matching,
                vertex_colours,
            ):
                allowed.append(matching)
                if counts is not None:
                    counts.edge_colour_matchings_blocked += 1
        choices[beta] = tuple(allowed)
    return choices


def compatible_edge_states(
    alpha_matching: tuple[tuple[int, int], ...],
    choices: Mapping[int, tuple[tuple[tuple[int, int], ...], ...]],
    counts: ChoiceCounts | None = None,
) -> Iterator[dict[tuple[int, int], int]]:
    """Select one pairwise edge-disjoint matching for every non-alpha colour."""

    if any(not choices[colour] for colour in NON_ALPHA):
        return
    ordered = tuple(sorted(NON_ALPHA, key=lambda colour: (len(choices[colour]), colour)))
    selected: dict[int, tuple[tuple[int, int], ...]] = {}

    def visit(
        index: int,
        used: frozenset[tuple[int, int]],
    ) -> Iterator[dict[tuple[int, int], int]]:
        if index == len(ordered):
            state = {candidate: ALPHA for candidate in alpha_matching}
            for colour in NON_ALPHA:
                state.update({candidate: colour for candidate in selected[colour]})
            yield state
            return
        colour = ordered[index]
        for matching in choices[colour]:
            if counts is not None:
                counts.edge_colour_matching_branches += 1
            if not used.isdisjoint(matching):
                continue
            selected[colour] = matching
            yield from visit(index + 1, used | frozenset(matching))
            selected.pop(colour)

    yield from visit(0, frozenset((*alpha_matching, XY)))


def compatible_direct_states(
    alpha_matching: tuple[tuple[int, int], ...],
    choices: Mapping[int, tuple[tuple[tuple[int, int], ...], ...]],
    outside_missing_sizes: tuple[int, ...],
    counts: ChoiceCounts | None = None,
) -> Iterator[dict[tuple[int, int], int]]:
    """Combine direct choices with the configured non-alpha degree envelope."""

    if any(not choices[colour] for colour in NON_ALPHA):
        return
    ordered = tuple(sorted(NON_ALPHA, key=lambda colour: (len(choices[colour]), colour)))
    selected: dict[int, tuple[tuple[int, int], ...]] = {}
    incidence = [0] * ORDER
    allowed_incidence = tuple(
        frozenset({3}) if vertex < 6 else frozenset(5 - size for size in outside_missing_sizes)
        for vertex in range(ORDER)
    )
    can_cover = {
        colour: tuple(
            any(any(vertex in candidate for candidate in matching) for matching in choices[colour])
            for vertex in range(ORDER)
        )
        for colour in NON_ALPHA
    }

    def visit(
        index: int,
        used: frozenset[tuple[int, int]],
    ) -> Iterator[dict[tuple[int, int], int]]:
        if index == len(ordered):
            if any(
                value not in allowed_incidence[vertex] for vertex, value in enumerate(incidence)
            ):
                return
            state = {candidate: ALPHA for candidate in alpha_matching}
            for colour in NON_ALPHA:
                state.update({candidate: colour for candidate in selected[colour]})
            yield state
            return
        colour = ordered[index]
        remaining_colours = ordered[index + 1 :]
        for matching in choices[colour]:
            if counts is not None:
                counts.edge_colour_matching_branches += 1
            if not used.isdisjoint(matching):
                continue
            touched = tuple(vertex for candidate in matching for vertex in candidate)
            if any(incidence[vertex] >= max(allowed_incidence[vertex]) for vertex in touched):
                continue
            for vertex in touched:
                incidence[vertex] += 1
            feasible = all(
                any(
                    incidence[vertex]
                    <= target
                    <= incidence[vertex]
                    + sum(can_cover[remaining][vertex] for remaining in remaining_colours)
                    for target in allowed_incidence[vertex]
                )
                for vertex in range(ORDER)
            )
            if feasible:
                selected[colour] = matching
                yield from visit(index + 1, used | frozenset(matching))
                selected.pop(colour)
            for vertex in touched:
                incidence[vertex] -= 1

    yield from visit(0, frozenset((*alpha_matching, XY)))


def partial_state_is_valid(
    vertex_colours: tuple[int, ...],
    state: Mapping[tuple[int, int], int],
) -> bool:
    """Check elementary partial-total-colouring constraints internally."""

    used = [{vertex_colours[vertex]} for vertex in range(ORDER)]
    for (left, right), colour in state.items():
        if not (0 <= left < right < ORDER) or colour not in PALETTE:
            return False
        if (left, right) == XY or vertex_colours[left] == vertex_colours[right]:
            return False
        if colour in used[left] or colour in used[right]:
            return False
        used[left].add(colour)
        used[right].add(colour)
    return True


def state_missing_sets(
    vertex_colours: tuple[int, ...],
    state: Mapping[tuple[int, int], int],
) -> tuple[frozenset[int], ...]:
    used = [{vertex_colours[vertex]} for vertex in range(ORDER)]
    for (left, right), colour in state.items():
        used[left].add(colour)
        used[right].add(colour)
    return tuple(frozenset(PALETTE) - colours for colours in used)


@dataclass(frozen=True, slots=True)
class OrbitMove:
    colours: tuple[int, int]
    component_edges: tuple[tuple[int, int], ...]
    component_walk: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class OrbitProposal:
    status: str
    explored_states: int
    depth: int | None
    common_missing_colour: int | None
    moves: tuple[OrbitMove, ...]


@dataclass(frozen=True, slots=True)
class TerminalRelease:
    beta: int
    hole: int
    terminal: int
    path: tuple[int, ...]
    terminal_edge: tuple[int, int]
    recolour_to: int
    prefix_edges: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class DirectCrossExit:
    colours: tuple[int, int]
    root: int
    component_edges: tuple[tuple[int, int], ...]
    component_walk: tuple[int, ...]
    fill_colour: int


@dataclass(frozen=True, slots=True)
class CrossTopology:
    colours: tuple[int, int]
    relation: str
    x_component_edges: tuple[tuple[int, int], ...]
    x_component_walk: tuple[int, ...]
    y_component_edges: tuple[tuple[int, int], ...]
    y_component_walk: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CrossTerminalRelease:
    colours: tuple[int, int]
    root: int
    terminal: int
    path: tuple[int, ...]
    terminal_edge: tuple[int, int]
    recolour_to: int
    prefix_edges: tuple[tuple[int, int], ...]


def _two_colour_path(
    state: Mapping[tuple[int, int], int],
    first: int,
    second: int,
    start: int,
    target: int,
) -> tuple[int, ...]:
    adjacency: list[list[int]] = [[] for _ in range(ORDER)]
    for (left, right), colour in state.items():
        if colour not in {first, second}:
            continue
        adjacency[left].append(right)
        adjacency[right].append(left)
    queue = deque([start])
    parent: dict[int, int | None] = {start: None}
    while queue:
        vertex = queue.popleft()
        if vertex == target:
            break
        for neighbor in sorted(adjacency[vertex]):
            if neighbor not in parent:
                parent[neighbor] = vertex
                queue.append(neighbor)
    if target not in parent:
        return ()
    reverse_path = [target]
    while parent[reverse_path[-1]] is not None:
        previous = parent[reverse_path[-1]]
        assert previous is not None
        reverse_path.append(previous)
    return tuple(reversed(reverse_path))


def terminal_releases(
    vertex_colours: tuple[int, ...],
    state: Mapping[tuple[int, int], int],
) -> tuple[TerminalRelease, ...]:
    """Find the local terminal-edge recolourings which precede a prefix swap."""

    missing = state_missing_sets(vertex_colours, state)
    alpha_matching = tuple(sorted(candidate for candidate, colour in state.items() if colour == 0))
    releases: list[TerminalRelease] = []
    for beta in NON_ALPHA:
        beta_matching = tuple(
            sorted(candidate for candidate, colour in state.items() if colour == beta)
        )
        terminals = {
            vertex for vertex, fixed_colour in enumerate(vertex_colours) if fixed_colour == beta
        }
        for hole in DISTINGUISHED_HOLES[beta]:
            component = component_vertices(alpha_matching, beta_matching, hole)
            terminal_choices = sorted(component & terminals)
            if len(terminal_choices) != 1:
                continue
            terminal = terminal_choices[0]
            path = _two_colour_path(state, ALPHA, beta, hole, terminal)
            # The audited move recolours the terminal alpha edge and swaps a
            # nonempty alpha/beta prefix.  A single alpha edge (two vertices)
            # has no preceding beta edge and is intentionally not classified
            # as a terminal release here.
            if len(path) < 4:
                continue
            terminal_candidate = edge(path[-2], path[-1])
            if state[terminal_candidate] != ALPHA:
                continue
            common = sorted(missing[path[-2]] & missing[path[-1]])
            if not common:
                continue
            prefix_edges = tuple(edge(left, right) for left, right in pairwise(path[:-1]))
            releases.append(
                TerminalRelease(
                    beta=beta,
                    hole=hole,
                    terminal=terminal,
                    path=path,
                    terminal_edge=terminal_candidate,
                    recolour_to=common[0],
                    prefix_edges=prefix_edges,
                )
            )
    return tuple(releases)


def direct_cross_exits(
    vertex_colours: tuple[int, ...],
    state: Mapping[tuple[int, int], int],
) -> tuple[DirectCrossExit, ...]:
    """Find the eight oriented one-component A-by-B exits rooted at x or y."""

    edges = tuple(sorted(state))
    colours = tuple(state[candidate] for candidate in edges)
    exits: list[DirectCrossExit] = []
    for first in (3, 4):
        for second in (5, 6):
            components = tuple(_state_components(edges, colours, first, second))
            for root, opposite, fill_colour in ((0, 1, second), (1, 0, first)):
                rooted = next(
                    (
                        component_indices
                        for component_indices in components
                        if any(root in edges[index] for index in component_indices)
                    ),
                    None,
                )
                if rooted is None:
                    continue
                component_edges = tuple(edges[index] for index in rooted)
                component_vertices_set = {
                    vertex for candidate in component_edges for vertex in candidate
                }
                if opposite in component_vertices_set:
                    continue
                target = dict(state)
                for candidate in component_edges:
                    target[candidate] = second if target[candidate] == first else first
                if not partial_state_is_valid(vertex_colours, target):
                    continue
                missing = state_missing_sets(vertex_colours, target)
                if fill_colour not in missing[0] or fill_colour not in missing[1]:
                    continue
                exits.append(
                    DirectCrossExit(
                        colours=(first, second),
                        root=root,
                        component_edges=component_edges,
                        component_walk=_component_walk(component_edges),
                        fill_colour=fill_colour,
                    )
                )
    return tuple(exits)


def cross_topologies(
    state: Mapping[tuple[int, int], int],
) -> tuple[CrossTopology, ...]:
    """Classify the four A-by-B root-component relations exactly."""

    edges = tuple(sorted(state))
    colours = tuple(state[candidate] for candidate in edges)
    result: list[CrossTopology] = []
    for first in (3, 4):
        for second in (5, 6):
            components = tuple(_state_components(edges, colours, first, second))
            x_component = next(
                (
                    component
                    for component in components
                    if any(0 in edges[index] for index in component)
                ),
                (),
            )
            y_component = next(
                (
                    component
                    for component in components
                    if any(1 in edges[index] for index in component)
                ),
                (),
            )
            x_edges = tuple(edges[index] for index in x_component)
            y_edges = tuple(edges[index] for index in y_component)
            result.append(
                CrossTopology(
                    colours=(first, second),
                    relation="coincident_xy" if x_component == y_component else "distinct",
                    x_component_edges=x_edges,
                    x_component_walk=_component_walk(x_edges) if x_edges else (),
                    y_component_edges=y_edges,
                    y_component_walk=_component_walk(y_edges) if y_edges else (),
                )
            )
    return tuple(result)


def cross_terminal_releases(
    vertex_colours: tuple[int, ...],
    state: Mapping[tuple[int, int], int],
) -> tuple[CrossTerminalRelease, ...]:
    """Find terminal-edge releases on distinct oriented A-by-B root paths."""

    missing = state_missing_sets(vertex_colours, state)
    edges = tuple(sorted(state))
    colours = tuple(state[candidate] for candidate in edges)
    releases: list[CrossTerminalRelease] = []
    for first in (3, 4):
        for second in (5, 6):
            components = tuple(_state_components(edges, colours, first, second))
            for root, opposite in ((0, 1), (1, 0)):
                component_indices = next(
                    (
                        component
                        for component in components
                        if any(root in edges[index] for index in component)
                    ),
                    (),
                )
                component_edges = tuple(edges[index] for index in component_indices)
                vertices = {vertex for candidate in component_edges for vertex in candidate}
                if not component_edges or opposite in vertices:
                    continue
                degree = {vertex: 0 for vertex in vertices}
                for left, right in component_edges:
                    degree[left] += 1
                    degree[right] += 1
                endpoints = sorted(vertex for vertex, value in degree.items() if value == 1)
                if root not in endpoints or len(endpoints) != 2:
                    continue
                terminal = endpoints[0] if endpoints[1] == root else endpoints[1]
                path = _two_colour_path(state, first, second, root, terminal)
                # The audited terminal move needs a nonempty alternating
                # prefix before the terminal edge.
                if len(path) < 3:
                    continue
                terminal_candidate = edge(path[-2], path[-1])
                preceding_candidate = edge(path[-3], path[-2])
                terminal_colour = state[terminal_candidate]
                preceding_colour = state[preceding_candidate]
                if preceding_colour == terminal_colour:
                    continue
                swapped_terminal_colour = second if terminal_colour == first else first
                if vertex_colours[terminal] != swapped_terminal_colour:
                    continue
                common = sorted(missing[path[-2]] & missing[path[-1]])
                if not common:
                    continue
                releases.append(
                    CrossTerminalRelease(
                        colours=(first, second),
                        root=root,
                        terminal=terminal,
                        path=path,
                        terminal_edge=terminal_candidate,
                        recolour_to=common[0],
                        prefix_edges=tuple(
                            edge(left, right) for left, right in pairwise(path[:-1])
                        ),
                    )
                )
    return tuple(releases)


def _state_components(
    edges: tuple[tuple[int, int], ...],
    colours: tuple[int, ...],
    first: int,
    second: int,
) -> Iterator[tuple[int, ...]]:
    selected = tuple(index for index, colour in enumerate(colours) if colour in {first, second})
    incident: list[list[int]] = [[] for _ in range(ORDER)]
    for index in selected:
        for vertex in edges[index]:
            incident[vertex].append(index)
    unseen = set(selected)
    while unseen:
        seed = min(unseen)
        unseen.remove(seed)
        stack = [seed]
        found: list[int] = []
        while stack:
            index = stack.pop()
            found.append(index)
            for vertex in edges[index]:
                for neighbor_index in incident[vertex]:
                    if neighbor_index in unseen:
                        unseen.remove(neighbor_index)
                        stack.append(neighbor_index)
        yield tuple(sorted(found))


def _component_walk(
    component_edges: tuple[tuple[int, int], ...],
) -> tuple[int, ...]:
    adjacency: dict[int, list[int]] = {}
    for left, right in component_edges:
        adjacency.setdefault(left, []).append(right)
        adjacency.setdefault(right, []).append(left)
    for neighbors in adjacency.values():
        neighbors.sort()
    endpoints = sorted(vertex for vertex, neighbors in adjacency.items() if len(neighbors) == 1)
    start = endpoints[0] if endpoints else min(adjacency)
    walk = [start]
    previous: int | None = None
    current = start
    while True:
        choices = [neighbor for neighbor in adjacency[current] if neighbor != previous]
        if not choices:
            break
        following = min(choices)
        if following == start and len(walk) == len(component_edges):
            walk.append(following)
            break
        if following in walk:
            break
        walk.append(following)
        previous, current = current, following
    return tuple(walk)


def propose_release(
    vertex_colours: tuple[int, ...],
    initial: Mapping[tuple[int, int], int],
    *,
    max_depth: int,
    max_states: int,
) -> OrbitProposal:
    """Search the configured component-swap orbit for a proposed colour of xy."""

    if max_depth < 0 or max_states <= 0:
        raise ValueError("orbit bounds must have max_depth >= 0 and max_states > 0")
    edges = tuple(sorted(initial))
    initial_colours = tuple(initial[candidate] for candidate in edges)

    def missing(colours: tuple[int, ...], vertex: int) -> frozenset[int]:
        used = {vertex_colours[vertex]}
        used.update(
            colour for candidate, colour in zip(edges, colours, strict=True) if vertex in candidate
        )
        return frozenset(PALETTE) - used

    def valid(colours: tuple[int, ...]) -> bool:
        return partial_state_is_valid(
            vertex_colours,
            dict(zip(edges, colours, strict=True)),
        )

    first_move_pairs = configured_first_move_pairs()
    later_move_pairs = historical_move_pairs()
    queue = deque([initial_colours])
    depth = {initial_colours: 0}
    parent: dict[tuple[int, ...], tuple[tuple[int, ...], OrbitMove]] = {}
    depth_truncated = False
    state_truncated = False

    while queue:
        colours = queue.popleft()
        current_depth = depth[colours]
        common = sorted(missing(colours, 0) & missing(colours, 1))
        if common:
            moves: list[OrbitMove] = []
            cursor = colours
            while cursor != initial_colours:
                previous, move = parent[cursor]
                moves.append(move)
                cursor = previous
            moves.reverse()
            return OrbitProposal(
                status="proposed_release",
                explored_states=len(depth),
                depth=current_depth,
                common_missing_colour=common[0],
                moves=tuple(moves),
            )
        if current_depth == max_depth:
            depth_truncated = True
            continue
        move_pairs = first_move_pairs if current_depth == 0 else later_move_pairs
        for first, second in move_pairs:
            for component_indices in _state_components(edges, colours, first, second):
                target = list(colours)
                for index in component_indices:
                    target[index] = second if target[index] == first else first
                candidate = tuple(target)
                if candidate in depth or not valid(candidate):
                    continue
                if len(depth) >= max_states:
                    state_truncated = True
                    continue
                component_edges = tuple(edges[index] for index in component_indices)
                move = OrbitMove(
                    colours=(first, second),
                    component_edges=component_edges,
                    component_walk=_component_walk(component_edges),
                )
                depth[candidate] = current_depth + 1
                parent[candidate] = (colours, move)
                queue.append(candidate)

    if state_truncated:
        status = "state_bound_reached"
    elif depth_truncated:
        status = "depth_bound_reached"
    else:
        status = "orbit_exhausted_no_release"
    return OrbitProposal(
        status=status,
        explored_states=len(depth),
        depth=None,
        common_missing_colour=None,
        moves=(),
    )


@dataclass(frozen=True, slots=True)
class SearchConfig:
    output_dir: Path
    profile_scope: ProfileScope = "canonical-fan"
    outside_missing_sizes: tuple[int, ...] = (2,)
    shard_index: int = 0
    shard_count: int = 1
    max_alpha_work_units: int | None = None
    max_missing_profiles: int | None = None
    max_initial_states: int | None = None
    max_candidate_states: int | None = None
    orbit_max_depth: int = 8
    orbit_max_states: int = 100_000
    checkpoint_interval: int = 100

    def validate(self) -> None:
        if self.profile_scope not in {"frozen", "canonical-fan"}:
            raise ValueError("unsupported profile scope")
        if self.shard_count <= 0 or not 0 <= self.shard_index < self.shard_count:
            raise ValueError("shard index must lie in [0, shard count)")
        if (
            not self.outside_missing_sizes
            or tuple(sorted(set(self.outside_missing_sizes))) != self.outside_missing_sizes
            or any(size < 2 or size > 5 for size in self.outside_missing_sizes)
        ):
            raise ValueError("outside missing sizes must be increasing values from 2,3,4,5")
        if self.profile_scope == "frozen" and self.outside_missing_sizes != (2,):
            raise ValueError("frozen scope requires --outside-missing-sizes 2")
        if self.profile_scope == "canonical-fan" and self.max_missing_profiles is not None:
            raise ValueError(
                "--max-missing-profiles applies only to the explicit frozen-profile mode"
            )
        for name, value in (
            ("max_alpha_work_units", self.max_alpha_work_units),
            ("max_missing_profiles", self.max_missing_profiles),
            ("max_initial_states", self.max_initial_states),
            ("max_candidate_states", self.max_candidate_states),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.orbit_max_depth < 0 or self.orbit_max_states <= 0:
            raise ValueError("invalid orbit bound")
        if self.checkpoint_interval <= 0:
            raise ValueError("checkpoint interval must be positive")

    def semantic_dict(self) -> dict[str, object]:
        return {
            "checkpoint_interval": self.checkpoint_interval,
            "max_alpha_work_units": self.max_alpha_work_units,
            "max_candidate_states": self.max_candidate_states,
            "max_initial_states": self.max_initial_states,
            "max_missing_profiles": self.max_missing_profiles,
            "orbit_max_depth": self.orbit_max_depth,
            "orbit_max_states": self.orbit_max_states,
            "outside_missing_sizes": list(self.outside_missing_sizes),
            "profile_scope": self.profile_scope,
            "shard_count": self.shard_count,
            "shard_index": self.shard_index,
        }


@dataclass(slots=True)
class RunCounts:
    vertex_colour_profiles_seen: int = 0
    alpha_matchings_seen: int = 0
    alpha_matchings_assigned_to_shard: int = 0
    alpha_work_units_completed: int = 0
    explicit_missing_profiles_seen: int = 0
    derived_missing_profile_occurrences: int = 0
    derived_distinct_missing_profiles_per_alpha_sum: int = 0
    alpha_work_units_with_all_blocking_colours: int = 0
    edge_colour_matchings_generated: int = 0
    edge_colour_matchings_blocked: int = 0
    edge_colour_matching_branches: int = 0
    initially_blocked_states_generated: int = 0
    easy_exit_states_pruned_from_broad_output: int = 0
    candidate_states_emitted: int = 0
    states_with_alpha_terminal_release: int = 0
    states_with_cross_terminal_release: int = 0
    states_with_direct_cross_exit: int = 0
    hard_residual_candidates: int = 0
    hard_both_offdiagonals_coincident: int = 0
    hard_both_offdiagonals_distinct: int = 0
    hard_mixed_offdiagonal_topology: int = 0
    hard_exact_fan_alignment: int = 0
    hard_role_set_fan_alignment: int = 0
    hard_other_fan_alignment: int = 0
    hard_alpha_hole_role_then_cross_two_swap_release: int = 0
    hard_alpha_vertex_role_then_cross_two_swap_release: int = 0
    hard_cross_role_then_cross_two_swap_release: int = 0
    hard_vertex_hole_role_then_cross_two_swap_release: int = 0
    hard_other_or_unresolved_bounded_orbit: int = 0
    proposed_releases: int = 0
    orbit_depth_bounds: int = 0
    orbit_state_bounds: int = 0
    orbit_exhausted_without_release: int = 0


class _BoundedStop(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _InterruptedStop(RuntimeError):
    pass


def fan_alignment(vertex_colours: tuple[int, ...]) -> str:
    """Classify, without assuming, the named fan vertices' fixed colours."""

    if vertex_colours[2:6] == (3, 4, 5, 6):
        return "exact_role_alignment"
    if set(vertex_colours[2:4]) == {3, 4} and set(vertex_colours[4:6]) == {5, 6}:
        return "role_set_alignment"
    return "other"


def orbit_pattern(proposal: OrbitProposal) -> str:
    """Classify the proposed move pattern without elevating it to a certificate."""

    if proposal.status != "proposed_release":
        return "no_release_within_configured_orbit_bound"
    if len(proposal.moves) != 2:
        return f"proposed_release_in_{len(proposal.moves)}_moves"
    first, second = proposal.moves
    first_set = set(first.colours)
    second_set = set(second.colours)
    second_is_cross = bool(second_set & {3, 4}) and bool(second_set & {5, 6})
    if ALPHA in first_set and second_is_cross:
        first_nonalpha = next(colour for colour in first.colours if colour != ALPHA)
        if first_nonalpha in {3, 4, 5, 6}:
            return "alpha_hole_role_then_cross_two_swap_release"
        if first_nonalpha in {1, 2}:
            return "alpha_vertex_role_then_cross_two_swap_release"
    first_is_cross = bool(first_set & {3, 4}) and bool(first_set & {5, 6})
    if first_is_cross and second_is_cross:
        return "cross_role_then_cross_two_swap_release"
    first_is_vertex_hole = len(first_set & {1, 2}) == 1 and len(first_set & {3, 4, 5, 6}) == 1
    if first_is_vertex_hole and second_is_cross:
        return "vertex_hole_role_then_cross_two_swap_release"
    return "other_two_swap_release"


def _walk_edge_positions(walk: tuple[int, ...]) -> dict[tuple[int, int], int]:
    return {edge(left, right): index for index, (left, right) in enumerate(pairwise(walk))}


def detachment_analysis(
    state: Mapping[tuple[int, int], int],
    proposal: OrbitProposal,
    topologies: tuple[CrossTopology, ...],
) -> dict[str, object] | None:
    """Derive the first-swap cross-component detachment data for a reducer."""

    if orbit_pattern(proposal) != "alpha_hole_role_then_cross_two_swap_release":
        return None
    first_move, cross_move = proposal.moves
    role_colour = next(colour for colour in first_move.colours if colour != ALPHA)
    pre = next(topology for topology in topologies if topology.colours == cross_move.colours)
    first_positions = _walk_edge_positions(first_move.component_walk)
    pre_x_positions = _walk_edge_positions(pre.x_component_walk)
    pre_y_positions = _walk_edge_positions(pre.y_component_walk)
    shared_role_edges = tuple(
        candidate
        for candidate in first_move.component_edges
        if state[candidate] == role_colour
        and (candidate in pre.x_component_edges or candidate in pre.y_component_edges)
    )
    after_first = dict(state)
    first, second = first_move.colours
    for candidate in first_move.component_edges:
        after_first[candidate] = second if after_first[candidate] == first else first
    post = next(
        topology
        for topology in cross_topologies(after_first)
        if topology.colours == cross_move.colours
    )
    return {
        "cross_colours": list(cross_move.colours),
        "first_alpha_role_colour": role_colour,
        "post_first_move_relation": post.relation,
        "pre_first_move_relation": pre.relation,
        "pre_first_move_was_coincident_xy": pre.relation == "coincident_xy",
        "shared_role_edges": [
            {
                "edge": list(candidate),
                "first_move_walk_index": first_positions.get(candidate),
                "pre_x_component_walk_index": pre_x_positions.get(candidate),
                "pre_y_component_walk_index": pre_y_positions.get(candidate),
            }
            for candidate in shared_role_edges
        ],
    }


def _scope_metadata(config: SearchConfig) -> dict[str, object]:
    if config.outside_missing_sizes == (2,):
        degree_scope = "all-vertices-have-four-coloured-edges-before-xy"
    else:
        degree_scope = "configured-submaximum-outside-alpha-terminal-degrees"
    return {
        "claim_boundary": (
            "untrusted finite candidate generation and bounded orbit proposals only; "
            "not an independent verifier, negative certificate, or proof"
        ),
        "degree_scope": degree_scope,
        "fixed_normalization": {
            "A": [3, 4],
            "B": [5, 6],
            "R": 5,
            "alpha": 0,
            "order": 12,
            "p_x": 1,
            "p_y": 2,
        },
        "profile_scope": config.profile_scope,
        "profile_scope_note": (
            "exact frozen vertex and missing profile"
            if config.profile_scope == "frozen"
            else (
                "all 64 named-fan fixed-colour profiles, modulo permutations of the "
                "six otherwise anonymous outside vertices; direct colour-matchings "
                "derive every outside missing profile having a configured size and "
                "compatible with the configured initial alpha blockages"
            )
        ),
        "outside_missing_sizes_included": list(config.outside_missing_sizes),
        "outside_missing_sizes_excluded": [
            size for size in (2, 3, 4, 5) if size not in config.outside_missing_sizes
        ],
        "record_policy": (
            "all initially alpha-blocked states for the exact regression"
            if config.profile_scope == "frozen"
            else (
                "only states surviving alpha-terminal, cross-terminal, and direct-cross easy exits"
            )
        ),
        "upstream_cardinality": {
            "alpha_matchings_per_vertex_profile": 5436,
            "alpha_work_units_before_sharding": (
                5436 if config.profile_scope == "frozen" else 347_904
            ),
            "vertex_colour_profiles": 1 if config.profile_scope == "frozen" else 64,
        },
        "sharding_unit": "global deterministic alpha-matching ordinal",
    }


def _record_payload(
    *,
    candidate_index: int,
    vertex_profile_index: int,
    alpha_global_ordinal: int,
    alpha_profile_ordinal: int,
    missing_profile_ordinal: int | None,
    vertex_colours: tuple[int, ...],
    missing: tuple[frozenset[int], ...],
    state: Mapping[tuple[int, int], int],
    proposal: OrbitProposal,
    local_terminal_releases: tuple[TerminalRelease, ...],
    local_cross_terminal_releases: tuple[CrossTerminalRelease, ...],
    local_cross_exits: tuple[DirectCrossExit, ...],
    local_cross_topologies: tuple[CrossTopology, ...],
) -> dict[str, object]:
    graph_edges = tuple(sorted((*state, XY)))
    raw_state = {
        "alpha": ALPHA,
        "degree_parameter": 5,
        "edge_colors": [None if candidate == XY else state[candidate] for candidate in graph_edges],
        "graph": {
            "edges": [list(candidate) for candidate in graph_edges],
            "order": ORDER,
            "schema_version": "total-coloring.simple-graph.v1",
        },
        "kind": "paired-hole-partial-total-coloring",
        "palette_size": len(PALETTE),
        "roles": {
            "x": 0,
            "x_fan_satellites": [2, 3],
            "y": 1,
            "y_fan_satellites": [4, 5],
        },
        "schema_version": "total-coloring.paired-hole-state.v1",
        "uncolored_edge": list(XY),
        "vertex_colors": list(vertex_colours),
    }
    derived_state = {
        "distinguished_holes": {
            str(colour): list(DISTINGUISHED_HOLES[colour]) for colour in NON_ALPHA
        },
        "missing_sets": [sorted(colours) for colours in missing],
    }
    return {
        "candidate_fingerprint": sha256_bytes(raw_state),
        "candidate_index": candidate_index,
        "detachment_analysis": detachment_analysis(
            state,
            proposal,
            local_cross_topologies,
        ),
        "fan_fixed_colour_classification": fan_alignment(vertex_colours),
        "cross_topologies": [
            {
                "colours": list(topology.colours),
                "relation": topology.relation,
                "x_component_edges": [list(candidate) for candidate in topology.x_component_edges],
                "x_component_walk": list(topology.x_component_walk),
                "y_component_edges": [list(candidate) for candidate in topology.y_component_edges],
                "y_component_walk": list(topology.y_component_walk),
            }
            for topology in local_cross_topologies
        ],
        "easy_exits": {
            "direct_cross": [
                {
                    "colours": list(cross_exit.colours),
                    "component_edges": [
                        list(candidate) for candidate in cross_exit.component_edges
                    ],
                    "component_walk": list(cross_exit.component_walk),
                    "fill_colour": cross_exit.fill_colour,
                    "root": cross_exit.root,
                }
                for cross_exit in local_cross_exits
            ],
            "alpha_terminal_release": [
                {
                    "beta": release.beta,
                    "hole": release.hole,
                    "path": list(release.path),
                    "prefix_edges": [list(candidate) for candidate in release.prefix_edges],
                    "recolour_terminal_edge_to": release.recolour_to,
                    "terminal": release.terminal,
                    "terminal_edge": list(release.terminal_edge),
                }
                for release in local_terminal_releases
            ],
            "cross_terminal_release": [
                {
                    "colours": list(release.colours),
                    "path": list(release.path),
                    "prefix_edges": [list(candidate) for candidate in release.prefix_edges],
                    "recolour_terminal_edge_to": release.recolour_to,
                    "root": release.root,
                    "terminal": release.terminal,
                    "terminal_edge": list(release.terminal_edge),
                }
                for release in local_cross_terminal_releases
            ],
        },
        "orbit_proposal": {
            "common_missing_colour": proposal.common_missing_colour,
            "depth": proposal.depth,
            "explored_states": proposal.explored_states,
            "moves": [
                {
                    "colours": list(move.colours),
                    "component_edges": [list(candidate) for candidate in move.component_edges],
                    "component_walk": list(move.component_walk),
                }
                for move in proposal.moves
            ],
            "status": proposal.status,
            "trust_note": "proposal is not independently certificate-verified",
        },
        "orbit_proposal_pattern": orbit_pattern(proposal),
        "residual_classification": (
            "hard_residual_candidate"
            if not local_terminal_releases
            and not local_cross_terminal_releases
            and not local_cross_exits
            else "easy_exit_present"
        ),
        "schema_version": RECORD_SCHEMA,
        "derived_analysis": derived_state,
        "raw_state": raw_state,
        "work_unit": {
            "alpha_global_ordinal": alpha_global_ordinal,
            "alpha_profile_ordinal": alpha_profile_ordinal,
            "missing_profile_ordinal": missing_profile_ordinal,
            "missing_profile_origin": (
                "explicit-frozen" if missing_profile_ordinal is not None else "derived-from-state"
            ),
            "vertex_profile_index": vertex_profile_index,
        },
    }


def _profiles_for_scope(scope: ProfileScope) -> tuple[tuple[int, ...], ...]:
    if scope == "frozen":
        return (FROZEN_VERTEX_COLOURS,)
    return canonical_vertex_colour_profiles()


def run_search(
    config: SearchConfig,
    *,
    should_interrupt: Callable[[], bool] = lambda: False,
) -> dict[str, object]:
    """Run one isolated shard and return its final checkpoint payload."""

    config.validate()
    try:
        config.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError("output directory must not already exist") from exc

    config_payload = config.semantic_dict()
    config_fingerprint = sha256_bytes(config_payload)
    script_path = Path(__file__)
    run_manifest = {
        "config": config_payload,
        "config_fingerprint": config_fingerprint,
        "schema_version": RUN_SCHEMA,
        "scope": _scope_metadata(config),
        "script_sha256": sha256_file(script_path),
    }
    atomic_json_write(config.output_dir / "run.json", run_manifest)

    records_path = config.output_dir / "candidates.jsonl"
    counts = RunCounts()
    alpha_global_ordinal = 0
    stop_reason = "configured_input_exhausted"
    status: RunStatus = "complete_generation"

    def checkpoint_payload(
        current_status: RunStatus,
        current_stop_reason: str,
    ) -> dict[str, object]:
        return {
            **run_manifest,
            "counts": asdict(counts),
            "checkpoint_resumable": False,
            "input_exhausted": current_status == "complete_generation",
            "next_alpha_global_ordinal_hint": alpha_global_ordinal,
            "records_bytes": records_path.stat().st_size,
            "records_sha256": sha256_file(records_path),
            "status": current_status,
            "stop_reason": current_stop_reason,
        }

    with records_path.open("xb") as records:
        try:
            for vertex_profile_index, vertex_colours in enumerate(
                _profiles_for_scope(config.profile_scope)
            ):
                counts.vertex_colour_profiles_seen += 1
                for alpha_profile_ordinal, alpha_matching in enumerate(
                    perfect_matchings(tuple(range(ORDER)), vertex_colours)
                ):
                    counts.alpha_matchings_seen += 1
                    current_global_ordinal = alpha_global_ordinal
                    alpha_global_ordinal += 1
                    if current_global_ordinal % config.shard_count != config.shard_index:
                        continue
                    if should_interrupt():
                        raise _InterruptedStop
                    if (
                        config.max_alpha_work_units is not None
                        and counts.alpha_work_units_completed >= config.max_alpha_work_units
                    ):
                        raise _BoundedStop("max_alpha_work_units")
                    counts.alpha_matchings_assigned_to_shard += 1
                    derived_profiles: set[str] = set()

                    def emit_state(
                        state: dict[tuple[int, int], int],
                        missing: tuple[frozenset[int], ...],
                        missing_profile_ordinal: int | None,
                        *,
                        current_vertex_colours: tuple[int, ...] = vertex_colours,
                        current_vertex_profile_index: int = vertex_profile_index,
                        current_alpha_global_ordinal: int = current_global_ordinal,
                        current_alpha_profile_ordinal: int = alpha_profile_ordinal,
                        current_derived_profiles: set[str] = derived_profiles,
                    ) -> None:
                        if should_interrupt():
                            raise _InterruptedStop
                        if not partial_state_is_valid(current_vertex_colours, state):
                            raise RuntimeError("generator produced an internally invalid state")
                        if state_missing_sets(current_vertex_colours, state) != missing:
                            raise RuntimeError(
                                "generated state does not realize its missing profile"
                            )
                        if missing[:6] != SPECIAL_MISSING or any(
                            len(colours) not in config.outside_missing_sizes
                            for colours in missing[6:]
                        ):
                            raise RuntimeError("state lies outside the configured degree scope")
                        if (
                            config.max_initial_states is not None
                            and counts.initially_blocked_states_generated
                            >= config.max_initial_states
                        ):
                            raise _BoundedStop("max_initial_states")
                        counts.initially_blocked_states_generated += 1
                        counts.derived_missing_profile_occurrences += 1
                        current_derived_profiles.add(
                            sha256_bytes([sorted(colours) for colours in missing])
                        )
                        local_terminal_releases = terminal_releases(
                            current_vertex_colours,
                            state,
                        )
                        local_cross_exits = direct_cross_exits(
                            current_vertex_colours,
                            state,
                        )
                        local_cross_terminal_releases = cross_terminal_releases(
                            current_vertex_colours,
                            state,
                        )
                        local_cross_topologies = cross_topologies(state)
                        if local_terminal_releases:
                            counts.states_with_alpha_terminal_release += 1
                        if local_cross_terminal_releases:
                            counts.states_with_cross_terminal_release += 1
                        if local_cross_exits:
                            counts.states_with_direct_cross_exit += 1
                        is_hard = (
                            not local_terminal_releases
                            and not local_cross_terminal_releases
                            and not local_cross_exits
                        )
                        if config.profile_scope != "frozen" and not is_hard:
                            counts.easy_exit_states_pruned_from_broad_output += 1
                            return
                        if (
                            config.max_candidate_states is not None
                            and counts.candidate_states_emitted >= config.max_candidate_states
                        ):
                            raise _BoundedStop("max_candidate_states")
                        proposal = propose_release(
                            current_vertex_colours,
                            state,
                            max_depth=config.orbit_max_depth,
                            max_states=config.orbit_max_states,
                        )
                        if is_hard:
                            counts.hard_residual_candidates += 1
                            alignment = fan_alignment(current_vertex_colours)
                            if alignment == "exact_role_alignment":
                                counts.hard_exact_fan_alignment += 1
                            elif alignment == "role_set_alignment":
                                counts.hard_role_set_fan_alignment += 1
                            else:
                                counts.hard_other_fan_alignment += 1
                            pattern = orbit_pattern(proposal)
                            if pattern == "alpha_hole_role_then_cross_two_swap_release":
                                counts.hard_alpha_hole_role_then_cross_two_swap_release += 1
                            elif pattern == "alpha_vertex_role_then_cross_two_swap_release":
                                counts.hard_alpha_vertex_role_then_cross_two_swap_release += 1
                            elif pattern == "cross_role_then_cross_two_swap_release":
                                counts.hard_cross_role_then_cross_two_swap_release += 1
                            elif pattern == "vertex_hole_role_then_cross_two_swap_release":
                                counts.hard_vertex_hole_role_then_cross_two_swap_release += 1
                            else:
                                counts.hard_other_or_unresolved_bounded_orbit += 1
                            topology_by_colours = {
                                topology.colours: topology.relation
                                for topology in local_cross_topologies
                            }
                            offdiagonal_relations = {
                                topology_by_colours[(3, 6)],
                                topology_by_colours[(4, 5)],
                            }
                            if offdiagonal_relations == {"coincident_xy"}:
                                counts.hard_both_offdiagonals_coincident += 1
                            elif offdiagonal_relations == {"distinct"}:
                                counts.hard_both_offdiagonals_distinct += 1
                            else:
                                counts.hard_mixed_offdiagonal_topology += 1
                        if proposal.status == "proposed_release":
                            counts.proposed_releases += 1
                        elif proposal.status == "depth_bound_reached":
                            counts.orbit_depth_bounds += 1
                        elif proposal.status == "state_bound_reached":
                            counts.orbit_state_bounds += 1
                        else:
                            counts.orbit_exhausted_without_release += 1
                        payload = _record_payload(
                            candidate_index=counts.candidate_states_emitted,
                            vertex_profile_index=current_vertex_profile_index,
                            alpha_global_ordinal=current_alpha_global_ordinal,
                            alpha_profile_ordinal=current_alpha_profile_ordinal,
                            missing_profile_ordinal=missing_profile_ordinal,
                            vertex_colours=current_vertex_colours,
                            missing=missing,
                            state=state,
                            proposal=proposal,
                            local_terminal_releases=local_terminal_releases,
                            local_cross_terminal_releases=local_cross_terminal_releases,
                            local_cross_exits=local_cross_exits,
                            local_cross_topologies=local_cross_topologies,
                        )
                        records.write(canonical_json_bytes(payload))
                        counts.candidate_states_emitted += 1

                    choice_counts = ChoiceCounts()
                    try:
                        if config.profile_scope == "frozen":
                            for missing_profile_ordinal, missing in enumerate(
                                configured_missing_profiles(
                                    config.profile_scope,
                                    vertex_colours,
                                    config.outside_missing_sizes,
                                )
                            ):
                                if should_interrupt():
                                    raise _InterruptedStop
                                if (
                                    config.max_missing_profiles is not None
                                    and counts.explicit_missing_profiles_seen
                                    >= config.max_missing_profiles
                                ):
                                    raise _BoundedStop("max_missing_profiles")
                                counts.explicit_missing_profiles_seen += 1
                                choices = blocked_matching_choices(
                                    alpha_matching,
                                    vertex_colours,
                                    missing,
                                    choice_counts,
                                )
                                if all(choices[colour] for colour in NON_ALPHA):
                                    counts.alpha_work_units_with_all_blocking_colours += 1
                                for state in compatible_edge_states(
                                    alpha_matching,
                                    choices,
                                    choice_counts,
                                ):
                                    emit_state(state, missing, missing_profile_ordinal)
                        else:
                            choices = direct_blocked_matching_choices(
                                alpha_matching,
                                vertex_colours,
                                choice_counts,
                            )
                            if all(choices[colour] for colour in NON_ALPHA):
                                counts.alpha_work_units_with_all_blocking_colours += 1
                            for state in compatible_direct_states(
                                alpha_matching,
                                choices,
                                config.outside_missing_sizes,
                                choice_counts,
                            ):
                                emit_state(state, state_missing_sets(vertex_colours, state), None)
                    finally:
                        counts.edge_colour_matchings_generated += (
                            choice_counts.edge_colour_matchings_generated
                        )
                        counts.edge_colour_matchings_blocked += (
                            choice_counts.edge_colour_matchings_blocked
                        )
                        counts.edge_colour_matching_branches += (
                            choice_counts.edge_colour_matching_branches
                        )
                        counts.derived_distinct_missing_profiles_per_alpha_sum += len(
                            derived_profiles
                        )
                    counts.alpha_work_units_completed += 1
                    if counts.alpha_work_units_completed % config.checkpoint_interval == 0:
                        records.flush()
                        os.fsync(records.fileno())
                        atomic_json_write(
                            config.output_dir / "checkpoint.json",
                            checkpoint_payload("running", "periodic_checkpoint"),
                        )
        except _BoundedStop as stop:
            status = "bounded_generation"
            stop_reason = stop.reason
        except _InterruptedStop:
            status = "interrupted"
            stop_reason = "interrupt_requested"
        records.flush()
        os.fsync(records.fileno())

    final = checkpoint_payload(status, stop_reason)
    atomic_json_write(config.output_dir / "checkpoint.json", final)
    if status == "complete_generation":
        atomic_json_write(config.output_dir / "completion.json", final)
    return final


def _parse_sizes(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part) for part in raw.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("missing sizes must be comma-separated integers") from exc
    if (
        not values
        or tuple(sorted(set(values))) != values
        or any(value < 2 or value > 5 for value in values)
    ):
        raise argparse.ArgumentTypeError("missing sizes must be increasing values from 2,3,4,5")
    return values


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--profile-scope",
        choices=("frozen", "canonical-fan"),
        default="canonical-fan",
    )
    parser.add_argument("--outside-missing-sizes", type=_parse_sizes, default=(2,))
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--max-alpha-work-units", type=int)
    parser.add_argument("--max-missing-profiles", type=int)
    parser.add_argument("--max-initial-states", type=int)
    parser.add_argument("--max-candidate-states", type=int)
    parser.add_argument("--orbit-max-depth", type=int, default=8)
    parser.add_argument("--orbit-max-states", type=int, default=100_000)
    parser.add_argument("--checkpoint-interval", type=int, default=100)
    return parser.parse_args(argv)


_INTERRUPTED = False


def _request_interrupt(_signum: int, _frame: object) -> None:
    global _INTERRUPTED
    _INTERRUPTED = True


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    config = SearchConfig(
        output_dir=arguments.output_dir,
        profile_scope=arguments.profile_scope,
        outside_missing_sizes=arguments.outside_missing_sizes,
        shard_index=arguments.shard_index,
        shard_count=arguments.shard_count,
        max_alpha_work_units=arguments.max_alpha_work_units,
        max_missing_profiles=arguments.max_missing_profiles,
        max_initial_states=arguments.max_initial_states,
        max_candidate_states=arguments.max_candidate_states,
        orbit_max_depth=arguments.orbit_max_depth,
        orbit_max_states=arguments.orbit_max_states,
        checkpoint_interval=arguments.checkpoint_interval,
    )
    signal.signal(signal.SIGINT, _request_interrupt)
    signal.signal(signal.SIGTERM, _request_interrupt)
    final = run_search(config, should_interrupt=lambda: _INTERRUPTED)
    print(json.dumps(final, allow_nan=False, sort_keys=True))
    return 130 if final["status"] == "interrupted" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"paired-hole orbit error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
