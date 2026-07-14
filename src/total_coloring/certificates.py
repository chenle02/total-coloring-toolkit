"""Independent semantic verification for total-coloring certificates.

This module intentionally imports no model or solver code.  It checks a color
assignment directly against :class:`~total_coloring.graph.SimpleGraph`, making
solver output an untrusted proposal rather than part of the trust boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from total_coloring.graph import (
    GraphFormatError,
    SimpleGraph,
    canonical_json_bytes,
    sha256_hex,
    strict_json_loads,
)

CERTIFICATE_SCHEMA_VERSION: Final = "total-coloring.certificate.v1"
CERTIFICATE_KIND: Final = "total-coloring"
_CERTIFICATE_KEYS: Final = frozenset(
    {
        "schema_version",
        "kind",
        "graph_fingerprint",
        "palette_size",
        "vertex_colors",
        "edge_colors",
    }
)
_HEX_DIGITS: Final = frozenset("0123456789abcdef")


class CertificateError(ValueError):
    """Base class for malformed or invalid certificate errors."""


class CertificateFormatError(CertificateError):
    """Raised when a certificate cannot be parsed canonically."""


class CertificateVerificationError(CertificateError):
    """Raised by :meth:`VerificationResult.require_valid`."""

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        summary = "; ".join(f"{issue.code} at {issue.path}" for issue in result.issues)
        super().__init__(f"total-coloring certificate is invalid: {summary}")


def _require_nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CertificateFormatError(f"{name} must be an integer")
    if value < 0:
        raise CertificateFormatError(f"{name} must be nonnegative")
    return value


def _normalize_colors(values: Iterable[int], *, name: str, palette_size: int) -> tuple[int, ...]:
    if isinstance(values, str | bytes):
        raise CertificateFormatError(f"{name} must be an array of integers")
    try:
        raw_colors = tuple(cast(Iterable[object], values))
    except TypeError as exc:
        raise CertificateFormatError(f"{name} must be an array of integers") from exc

    colors: list[int] = []
    for index, raw_color in enumerate(raw_colors):
        color = _require_nonnegative_int(raw_color, name=f"{name}[{index}]")
        if color >= palette_size:
            raise CertificateFormatError(
                f"{name}[{index}]={color} is outside palette 0..{palette_size - 1}"
            )
        colors.append(color)
    return tuple(colors)


@dataclass(frozen=True, slots=True)
class TotalColoringCertificate:
    """A deterministic color assignment aligned with canonical graph order.

    ``vertex_colors[v]`` colors vertex ``v``.  ``edge_colors[i]`` colors
    ``graph.edges[i]``.  The graph itself remains a separate artifact and is
    bound cryptographically through ``graph_fingerprint``.
    """

    graph_fingerprint: str
    palette_size: int
    vertex_colors: tuple[int, ...]
    edge_colors: tuple[int, ...]
    _fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.graph_fingerprint, str)
            or len(self.graph_fingerprint) != 64
            or any(character not in _HEX_DIGITS for character in self.graph_fingerprint)
        ):
            raise CertificateFormatError(
                "graph_fingerprint must be a 64-character lowercase SHA-256 hex digest"
            )
        palette_size = _require_nonnegative_int(self.palette_size, name="palette_size")
        vertex_colors = _normalize_colors(
            self.vertex_colors, name="vertex_colors", palette_size=palette_size
        )
        edge_colors = _normalize_colors(
            self.edge_colors, name="edge_colors", palette_size=palette_size
        )
        object.__setattr__(self, "palette_size", palette_size)
        object.__setattr__(self, "vertex_colors", vertex_colors)
        object.__setattr__(self, "edge_colors", edge_colors)
        object.__setattr__(self, "_fingerprint", sha256_hex(self.to_dict()))

    @classmethod
    def create(
        cls,
        graph: SimpleGraph,
        palette_size: int,
        vertex_colors: Iterable[int],
        edge_colors: Iterable[int],
    ) -> TotalColoringCertificate:
        """Create a certificate bound to ``graph`` in canonical element order."""

        return cls(
            graph_fingerprint=graph.fingerprint,
            palette_size=palette_size,
            vertex_colors=tuple(vertex_colors),
            edge_colors=tuple(edge_colors),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TotalColoringCertificate:
        """Parse a strict schema-v1 certificate object."""

        if not isinstance(value, Mapping):
            raise CertificateFormatError("certificate document must be a JSON object")
        keys = set(value)
        if keys != _CERTIFICATE_KEYS:
            missing = sorted(_CERTIFICATE_KEYS - keys)
            extra = sorted(keys - _CERTIFICATE_KEYS)
            details: list[str] = []
            if missing:
                details.append(f"missing keys {missing}")
            if extra:
                details.append(f"unknown keys {extra}")
            raise CertificateFormatError("invalid certificate object: " + "; ".join(details))
        if value["schema_version"] != CERTIFICATE_SCHEMA_VERSION:
            raise CertificateFormatError("unsupported certificate schema_version")
        if value["kind"] != CERTIFICATE_KIND:
            raise CertificateFormatError("unsupported certificate kind")
        graph_fingerprint = value["graph_fingerprint"]
        if not isinstance(graph_fingerprint, str):
            raise CertificateFormatError("graph_fingerprint must be a string")
        palette_size = _require_nonnegative_int(value["palette_size"], name="palette_size")
        raw_vertex_colors = value["vertex_colors"]
        raw_edge_colors = value["edge_colors"]
        if isinstance(raw_vertex_colors, str | bytes) or not isinstance(
            raw_vertex_colors, Sequence
        ):
            raise CertificateFormatError("vertex_colors must be a JSON array")
        if isinstance(raw_edge_colors, str | bytes) or not isinstance(raw_edge_colors, Sequence):
            raise CertificateFormatError("edge_colors must be a JSON array")
        return cls(
            graph_fingerprint=graph_fingerprint,
            palette_size=palette_size,
            vertex_colors=tuple(cast(Sequence[int], raw_vertex_colors)),
            edge_colors=tuple(cast(Sequence[int], raw_edge_colors)),
        )

    @classmethod
    def from_json(cls, data: str | bytes) -> TotalColoringCertificate:
        """Parse strict JSON, including rejection of duplicate object keys."""

        try:
            value = strict_json_loads(data)
        except GraphFormatError as exc:
            raise CertificateFormatError(str(exc)) from exc
        if not isinstance(value, Mapping):
            raise CertificateFormatError("certificate document must be a JSON object")
        return cls.from_dict(cast(Mapping[str, object], value))

    @property
    def fingerprint(self) -> str:
        """SHA-256 of this canonical certificate JSON."""

        return self._fingerprint

    def to_dict(self) -> dict[str, object]:
        """Return the canonical schema-v1 certificate object."""

        return {
            "schema_version": CERTIFICATE_SCHEMA_VERSION,
            "kind": CERTIFICATE_KIND,
            "graph_fingerprint": self.graph_fingerprint,
            "palette_size": self.palette_size,
            "vertex_colors": list(self.vertex_colors),
            "edge_colors": list(self.edge_colors),
        }

    def to_json(self) -> str:
        """Return canonical JSON without a trailing newline."""

        return canonical_json_bytes(self.to_dict()).decode("utf-8")

    def verify(self, graph: SimpleGraph) -> VerificationResult:
        """Verify this certificate directly against ``graph``."""

        return verify_total_coloring(graph, self)


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    """One deterministic semantic certificate violation."""

    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Complete result from independent semantic verification."""

    issues: tuple[VerificationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues

    def require_valid(self) -> None:
        """Raise :class:`CertificateVerificationError` unless valid."""

        if self.issues:
            raise CertificateVerificationError(self)


def verify_total_coloring(
    graph: SimpleGraph, certificate: TotalColoringCertificate
) -> VerificationResult:
    """Check every total-coloring constraint without invoking a solver."""

    issues: list[VerificationIssue] = []
    if certificate.graph_fingerprint != graph.fingerprint:
        issues.append(
            VerificationIssue(
                code="graph_fingerprint_mismatch",
                path="graph_fingerprint",
                message="certificate is bound to a different numbered graph",
            )
        )
    if graph.order + graph.size > 0 and certificate.palette_size == 0:
        issues.append(
            VerificationIssue(
                code="empty_palette",
                path="palette_size",
                message="a nonempty set of graph elements requires at least one color",
            )
        )
    if len(certificate.vertex_colors) != graph.order:
        issues.append(
            VerificationIssue(
                code="vertex_assignment_count",
                path="vertex_colors",
                message=(
                    f"expected {graph.order} vertex colors, got {len(certificate.vertex_colors)}"
                ),
            )
        )
    if len(certificate.edge_colors) != graph.size:
        issues.append(
            VerificationIssue(
                code="edge_assignment_count",
                path="edge_colors",
                message=f"expected {graph.size} edge colors, got {len(certificate.edge_colors)}",
            )
        )

    vertex_count = min(graph.order, len(certificate.vertex_colors))
    edge_count = min(graph.size, len(certificate.edge_colors))

    for edge_index, (u, v) in enumerate(graph.edges):
        if (
            u < vertex_count
            and v < vertex_count
            and certificate.vertex_colors[u] == certificate.vertex_colors[v]
        ):
            issues.append(
                VerificationIssue(
                    code="adjacent_vertices_same_color",
                    path=f"edges[{edge_index}]",
                    message=f"adjacent vertices {u} and {v} share a color",
                )
            )
        if edge_index >= edge_count:
            continue
        edge_color = certificate.edge_colors[edge_index]
        issues.extend(
            VerificationIssue(
                code="incident_vertex_edge_same_color",
                path=f"edge_colors[{edge_index}]",
                message=f"edge {(u, v)} shares a color with endpoint {endpoint}",
            )
            for endpoint in (u, v)
            if endpoint < vertex_count and edge_color == certificate.vertex_colors[endpoint]
        )

    first_incident_edge_by_color: list[dict[int, int]] = [dict() for _ in range(graph.order)]
    for edge_index, (u, v) in enumerate(graph.edges[:edge_count]):
        color = certificate.edge_colors[edge_index]
        for endpoint in (u, v):
            previous = first_incident_edge_by_color[endpoint].get(color)
            if previous is not None:
                issues.append(
                    VerificationIssue(
                        code="adjacent_edges_same_color",
                        path=f"edge_colors[{edge_index}]",
                        message=(
                            f"incident edges {graph.edges[previous]} and {(u, v)} "
                            f"share color {color} at vertex {endpoint}"
                        ),
                    )
                )
            else:
                first_incident_edge_by_color[endpoint][color] = edge_index

    return VerificationResult(tuple(issues))


__all__ = [
    "CERTIFICATE_KIND",
    "CERTIFICATE_SCHEMA_VERSION",
    "CertificateError",
    "CertificateFormatError",
    "CertificateVerificationError",
    "TotalColoringCertificate",
    "VerificationIssue",
    "VerificationResult",
    "verify_total_coloring",
]
