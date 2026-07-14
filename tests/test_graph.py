from __future__ import annotations

import json

import pytest

from total_coloring.graph import (
    GRAPH_SCHEMA_VERSION,
    GraphFormatError,
    GraphValidationError,
    SimpleGraph,
    canonical_json_bytes,
    strict_json_loads,
)


def test_graph_normalizes_edges_and_computes_invariants() -> None:
    graph = SimpleGraph.from_edges(4, [(2, 0), (3, 2), (1, 0)])

    assert graph.order == 4
    assert graph.size == 3
    assert graph.edges == ((0, 1), (0, 2), (2, 3))
    assert graph.degrees == (2, 1, 2, 1)
    assert graph.max_degree == graph.maximum_degree == 2
    assert graph.min_degree == graph.minimum_degree == 1
    assert graph.degree(2) == 2
    assert graph.neighbors(0) == frozenset({1, 2})
    assert graph.incident_edges(2) == ((0, 2), (2, 3))
    assert graph.has_edge(2, 0)
    assert not graph.has_edge(1, 1)
    assert not graph.is_regular


def test_empty_graph_conventions() -> None:
    graph = SimpleGraph.from_edges(0, [])

    assert graph.degrees == ()
    assert graph.max_degree == 0
    assert graph.min_degree == 0
    assert graph.is_regular


@pytest.mark.parametrize(
    ("order", "edges", "message"),
    [
        (-1, [], "order must be at least 0"),
        (True, [], "order must be an integer"),
        (2, [(0, 0)], "loop"),
        (2, [(0, 1), (1, 0)], "duplicate edge"),
        (2, [(0, 2)], "outside"),
        (2, [(-1, 1)], "at least 0"),
        (2, [(False, 1)], "must be an integer"),
        (2, [(0,)], "exactly two"),
        (2, [(0, 1, 2)], "exactly two"),
        (2, ["01"], "two-item sequence"),
        (2, [1], "two-item sequence"),
    ],
)
def test_graph_rejects_malformed_edges(order: int, edges: object, message: str) -> None:
    with pytest.raises(GraphValidationError, match=message):
        SimpleGraph.from_edges(order, edges)  # type: ignore[arg-type]


def test_graph_rejects_noniterable_edges() -> None:
    with pytest.raises(GraphValidationError, match="iterable"):
        SimpleGraph.from_edges(2, 3)  # type: ignore[arg-type]
    with pytest.raises(GraphValidationError, match="endpoint pairs"):
        SimpleGraph(order=2, edges=3)  # type: ignore[arg-type]


def test_vertex_queries_reject_invalid_vertices() -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])

    for vertex in (-1, 2, True):
        with pytest.raises(GraphValidationError):
            graph.neighbors(vertex)


def test_numbered_graph_fingerprint_is_canonical_and_stable() -> None:
    first = SimpleGraph.from_edges(3, [(1, 2), (1, 0), (2, 0)])
    second = SimpleGraph.from_edges(3, [(0, 1), (0, 2), (1, 2)])

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint == "3ef1e526159a0ccf1519c6c659cd49533b110361f1e07be74aeb866e9c788083"
    assert len(first.fingerprint) == 64


def test_graph_json_round_trip_is_canonical() -> None:
    graph = SimpleGraph.from_edges(3, [(1, 2), (0, 1)])
    expected = f'{{"edges":[[0,1],[1,2]],"order":3,"schema_version":"{GRAPH_SCHEMA_VERSION}"}}'

    assert graph.to_json() == expected
    assert SimpleGraph.from_json(expected) == graph
    assert SimpleGraph.from_dict(graph.to_dict()) == graph


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        "{}",
        '{"schema_version":"wrong","order":0,"edges":[]}',
        (f'{{"schema_version":"{GRAPH_SCHEMA_VERSION}","order":0,"edges":[],"unknown":1}}'),
        f'{{"schema_version":"{GRAPH_SCHEMA_VERSION}","order":true,"edges":[]}}',
        f'{{"schema_version":"{GRAPH_SCHEMA_VERSION}","order":2,"edges":"01"}}',
        (f'{{"schema_version":"{GRAPH_SCHEMA_VERSION}","order":2,"edges":[[1,0]]}}'),
        (f'{{"schema_version":"{GRAPH_SCHEMA_VERSION}","order":3,"edges":[[1,2],[0,1]]}}'),
        (f'{{"schema_version":"{GRAPH_SCHEMA_VERSION}","order":0,"order":1,"edges":[]}}'),
        "{not-json}",
    ],
)
def test_graph_json_rejects_malformed_or_noncanonical_documents(document: str) -> None:
    with pytest.raises((GraphFormatError, GraphValidationError)):
        SimpleGraph.from_json(document)


def test_strict_json_and_canonical_json_reject_nonstandard_values() -> None:
    with pytest.raises(GraphFormatError, match="non-finite"):
        strict_json_loads('{"x": NaN}')
    with pytest.raises(GraphFormatError, match="duplicate"):
        strict_json_loads('{"x": 1, "x": 2}')
    with pytest.raises(GraphFormatError, match="canonical-JSON"):
        canonical_json_bytes({"not-json": object()})
    with pytest.raises(GraphFormatError, match="str or bytes"):
        strict_json_loads(1)  # type: ignore[arg-type]
    with pytest.raises(GraphFormatError, match="JSON object"):
        SimpleGraph.from_dict([])  # type: ignore[arg-type]


def test_canonical_json_is_independent_of_mapping_insertion_order() -> None:
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert strict_json_loads(b'{"a":1}') == {"a": 1}


def test_relabel_requires_a_bijection() -> None:
    graph = SimpleGraph.from_edges(3, [(0, 1), (1, 2)])

    assert graph.relabel([2, 0, 1]).edges == ((0, 1), (0, 2))
    for permutation in ([0, 1], [0, 0, 2], [0, 1, 3], [0, True, 2]):
        with pytest.raises(GraphValidationError):
            graph.relabel(permutation)
    with pytest.raises(GraphValidationError, match="sequence"):
        graph.relabel("012")  # type: ignore[arg-type]


def test_json_output_is_standard_json() -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])

    assert json.loads(graph.to_json()) == graph.to_dict()


def test_json_parser_enforces_configurable_resource_limit_before_allocation() -> None:
    document = f'{{"edges":[],"order":1000,"schema_version":"{GRAPH_SCHEMA_VERSION}"}}'

    with pytest.raises(GraphValidationError, match="resource limit"):
        SimpleGraph.from_json(document, max_order=10)
    assert SimpleGraph.from_json(document, max_order=None).order == 1000
    with pytest.raises(GraphValidationError, match="max_order"):
        SimpleGraph.from_json(document, max_order=True)
