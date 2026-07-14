from __future__ import annotations

import json

import pytest

from total_coloring.certificates import (
    CERTIFICATE_KIND,
    CERTIFICATE_SCHEMA_VERSION,
    CertificateFormatError,
    CertificateVerificationError,
    TotalColoringCertificate,
    VerificationResult,
    verify_total_coloring,
)
from total_coloring.graph import SimpleGraph


def triangle() -> SimpleGraph:
    return SimpleGraph.from_edges(3, [(0, 1), (0, 2), (1, 2)])


def triangle_certificate() -> TotalColoringCertificate:
    return TotalColoringCertificate.create(
        triangle(), palette_size=3, vertex_colors=[0, 1, 2], edge_colors=[2, 1, 0]
    )


def test_valid_total_coloring_certificate() -> None:
    graph = triangle()
    certificate = triangle_certificate()

    result = verify_total_coloring(graph, certificate)
    assert result.valid
    assert result.issues == ()
    assert certificate.verify(graph) == result
    result.require_valid()


def test_certificate_json_is_canonical_and_round_trips() -> None:
    certificate = triangle_certificate()
    document = certificate.to_json()

    assert document == (
        '{"edge_colors":[2,1,0],'
        f'"graph_fingerprint":"{triangle().fingerprint}",'
        f'"kind":"{CERTIFICATE_KIND}","palette_size":3,'
        f'"schema_version":"{CERTIFICATE_SCHEMA_VERSION}",'
        '"vertex_colors":[0,1,2]}'
    )
    assert TotalColoringCertificate.from_json(document) == certificate
    assert TotalColoringCertificate.from_dict(certificate.to_dict()) == certificate
    assert json.loads(document) == certificate.to_dict()
    assert len(certificate.fingerprint) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"graph_fingerprint": "A" * 64},
        {"graph_fingerprint": "0" * 63},
        {"palette_size": -1},
        {"palette_size": True},
        {"palette_size": 2, "vertex_colors": [0, 1, 2]},
        {"vertex_colors": [0, True, 2]},
        {"edge_colors": [2, -1, 0]},
        {"vertex_colors": "012"},
        {"edge_colors": "210"},
    ],
)
def test_certificate_rejects_malformed_fields(changes: dict[str, object]) -> None:
    value = triangle_certificate().to_dict()
    value.update(changes)

    with pytest.raises(CertificateFormatError):
        TotalColoringCertificate.from_dict(value)


def test_certificate_rejects_noniterable_colors_and_nonmapping_document() -> None:
    with pytest.raises(CertificateFormatError, match="array"):
        TotalColoringCertificate(
            graph_fingerprint="0" * 64,
            palette_size=1,
            vertex_colors=(),
            edge_colors=1,  # type: ignore[arg-type]
        )
    with pytest.raises(CertificateFormatError, match="JSON object"):
        TotalColoringCertificate.from_dict([])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "wrong"),
        ("kind", "wrong"),
        ("graph_fingerprint", 0),
    ],
)
def test_certificate_rejects_wrong_discriminators(field: str, value: object) -> None:
    document = triangle_certificate().to_dict()
    document[field] = value

    with pytest.raises(CertificateFormatError):
        TotalColoringCertificate.from_dict(document)


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        "{}",
        (
            f'{{"schema_version":"{CERTIFICATE_SCHEMA_VERSION}",'
            f'"kind":"{CERTIFICATE_KIND}","graph_fingerprint":"{"0" * 64}",'
            '"palette_size":0,"vertex_colors":[],"edge_colors":[],"extra":1}'
        ),
        (
            f'{{"schema_version":"{CERTIFICATE_SCHEMA_VERSION}",'
            f'"kind":"{CERTIFICATE_KIND}","graph_fingerprint":"{"0" * 64}",'
            '"palette_size":0,"palette_size":1,"vertex_colors":[],"edge_colors":[]}'
        ),
        "{bad-json}",
    ],
)
def test_certificate_json_rejects_noncanonical_documents(document: str) -> None:
    with pytest.raises(CertificateFormatError):
        TotalColoringCertificate.from_json(document)


def test_verifier_reports_graph_binding_and_assignment_counts() -> None:
    graph = triangle()
    certificate = TotalColoringCertificate(
        graph_fingerprint="0" * 64,
        palette_size=3,
        vertex_colors=(0, 1),
        edge_colors=(2,),
    )

    result = verify_total_coloring(graph, certificate)
    codes = {issue.code for issue in result.issues}
    assert not result.valid
    assert codes >= {
        "graph_fingerprint_mismatch",
        "vertex_assignment_count",
        "edge_assignment_count",
    }
    with pytest.raises(CertificateVerificationError) as caught:
        result.require_valid()
    assert caught.value.result is result


@pytest.mark.parametrize(
    ("vertex_colors", "edge_colors", "expected_code"),
    [
        ([0, 0, 2], [2, 1, 0], "adjacent_vertices_same_color"),
        ([0, 1, 2], [0, 1, 2], "incident_vertex_edge_same_color"),
        ([0, 1, 2], [2, 2, 0], "adjacent_edges_same_color"),
    ],
)
def test_verifier_detects_each_total_coloring_conflict(
    vertex_colors: list[int], edge_colors: list[int], expected_code: str
) -> None:
    graph = triangle()
    certificate = TotalColoringCertificate.create(graph, 3, vertex_colors, edge_colors)

    result = verify_total_coloring(graph, certificate)
    assert expected_code in {issue.code for issue in result.issues}


def test_empty_and_isolated_graph_certificates() -> None:
    empty = SimpleGraph.from_edges(0, [])
    isolated = SimpleGraph.from_edges(1, [])

    assert TotalColoringCertificate.create(empty, 0, [], []).verify(empty).valid
    assert TotalColoringCertificate.create(isolated, 1, [0], []).verify(isolated).valid

    no_assignment = TotalColoringCertificate.create(isolated, 0, [], [])
    assert {issue.code for issue in no_assignment.verify(isolated).issues} == {
        "empty_palette",
        "vertex_assignment_count",
    }


def test_verification_result_defaults_to_valid() -> None:
    assert VerificationResult().valid
