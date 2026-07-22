"""Dependency-free primitives for the independent protected-transfer audit.

This module intentionally does not import the producer kernel. Variable
allocation, clause normalization, DIMACS parsing, and endpoint orientations
are reconstructed here so producer/checker agreement is a differential check,
not shared implementation.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Iterable
from pathlib import Path
from typing import TypeAlias

Edge: TypeAlias = tuple[int, int]
Clause: TypeAlias = tuple[int, ...]

C = frozenset(range(6))
T0 = frozenset((0, 1, 2))
T1 = frozenset((3, 4, 5))
FACTORS = tuple(range(6))
OWNERS = tuple(range(6, 12))
X = 12
INACTIVE = tuple(range(13, 31))
OUTSIDE = frozenset((*OWNERS, X, *INACTIVE))


def edge(left: int, right: int) -> Edge:
    if left == right:
        raise ValueError("an edge must have distinct endpoints")
    return (left, right) if left < right else (right, left)


def owner(factor: int) -> int:
    return OWNERS[factor]


CORE_EDGES = tuple(itertools.combinations(sorted(C), 2))
CORE_OUTSIDE_EDGES = tuple(edge(head, tail) for head in sorted(C) for tail in sorted(OUTSIDE))
PHYSICAL_EDGES = tuple(sorted((*CORE_EDGES, *CORE_OUTSIDE_EDGES)))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def orientations(current: Edge) -> tuple[tuple[int, int], ...]:
    """Orient an incident edge at each of its core endpoints."""

    return tuple(
        (head, next(vertex for vertex in current if vertex != head))
        for head in sorted(set(current) & C)
    )


class IndependentCNF:
    """Checker-owned deterministic CNF with no producer dependency."""

    def __init__(self) -> None:
        self.var_of: dict[object, int] = {}
        self.key_of: list[object | None] = [None]
        self.clauses: set[Clause] = set()

    def var(self, key: object) -> int:
        if key not in self.var_of:
            self.var_of[key] = len(self.key_of)
            self.key_of.append(key)
        return self.var_of[key]

    @staticmethod
    def normalize(literals: Iterable[int]) -> Clause | None:
        clause = tuple(sorted(set(literals), key=lambda literal: (abs(literal), literal)))
        if any(-literal in clause for literal in clause):
            return None
        if not clause:
            raise ValueError("empty clauses are not accepted by this finite encoding")
        return clause

    def add(self, literals: Iterable[int]) -> bool:
        clause = self.normalize(literals)
        if clause is None:
            return False
        before = len(self.clauses)
        self.clauses.add(clause)
        return len(self.clauses) != before

    def at_most_one(self, variables: Iterable[int]) -> None:
        for left, right in itertools.combinations(sorted(set(variables)), 2):
            self.add((-left, -right))

    def exactly_one(self, variables: Iterable[int]) -> None:
        stable = tuple(sorted(set(variables)))
        if not stable:
            raise ValueError("exactly-one requires at least one variable")
        self.add(stable)
        self.at_most_one(stable)

    def at_most_k(self, variables: Iterable[int], limit: int) -> None:
        stable = tuple(sorted(set(variables)))
        if not 0 <= limit <= len(stable):
            raise ValueError("invalid at-most-k limit")
        for chosen in itertools.combinations(stable, limit + 1):
            self.add(-variable for variable in chosen)

    def at_least_k(self, variables: Iterable[int], limit: int) -> None:
        stable = tuple(sorted(set(variables)))
        if not 0 <= limit <= len(stable):
            raise ValueError("invalid at-least-k limit")
        for chosen in itertools.combinations(stable, len(stable) - limit + 1):
            self.add(chosen)

    def dimacs_bytes(self) -> bytes:
        clauses = sorted(self.clauses, key=lambda clause: (len(clause), clause))
        lines = [f"p cnf {len(self.key_of) - 1} {len(clauses)}"]
        lines.extend(" ".join(map(str, clause)) + " 0" for clause in clauses)
        return ("\n".join(lines) + "\n").encode("ascii")


def parse_dimacs(path: Path) -> tuple[tuple[int, int], set[Clause]]:
    """Parse the strict clause-set DIMACS subset emitted by the producer."""

    header: tuple[int, int] | None = None
    clauses: set[Clause] = set()
    for line in path.read_text(encoding="ascii").splitlines():
        if not line or line.startswith("c"):
            continue
        if line.startswith("p "):
            fields = line.split()
            if len(fields) != 4 or fields[:2] != ["p", "cnf"] or header is not None:
                raise ValueError("malformed or repeated DIMACS header")
            header = (int(fields[2]), int(fields[3]))
            continue
        values = tuple(int(token) for token in line.split())
        if not values or values[-1] != 0 or 0 in values[:-1]:
            raise ValueError("malformed DIMACS clause")
        clause = IndependentCNF.normalize(values[:-1])
        if clause is None or clause in clauses:
            raise ValueError("tautological or duplicate DIMACS clause")
        clauses.add(clause)
    if header is None or header[1] != len(clauses):
        raise ValueError("DIMACS header/count mismatch")
    if any(abs(literal) > header[0] for clause in clauses for literal in clause):
        raise ValueError("DIMACS literal exceeds declared variable count")
    return header, clauses
