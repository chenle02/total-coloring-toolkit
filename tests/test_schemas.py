from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]

from total_coloring.certificates import TotalColoringCertificate
from total_coloring.graph import SimpleGraph
from total_coloring.schema_resources import read_schema_json


@pytest.mark.parametrize(
    "name",
    [
        "d8-dependency-pivot-audit-v1.schema.json",
        "graph-v1.schema.json",
        "paired-hole-state-v1.schema.json",
        "total-coloring-certificate-v1.schema.json",
    ],
)
def test_schema_itself_is_valid(name: str) -> None:
    Draft202012Validator.check_schema(read_schema_json(name))


def test_graph_schema_accepts_canonical_graph_and_rejects_extra_fields() -> None:
    schema = read_schema_json("graph-v1.schema.json")
    graph_value = SimpleGraph.from_edges(2, [(0, 1)]).to_dict()
    Draft202012Validator(schema).validate(graph_value)

    graph_value["extra"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(graph_value)


def test_certificate_schema_accepts_certificate_and_rejects_bad_digest() -> None:
    schema = read_schema_json("total-coloring-certificate-v1.schema.json")
    graph = SimpleGraph.from_edges(2, [(0, 1)])
    certificate_value = TotalColoringCertificate.create(graph, 3, [0, 1], [2]).to_dict()
    Draft202012Validator(schema).validate(certificate_value)

    certificate_value["graph_fingerprint"] = "not-a-digest"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(certificate_value)
