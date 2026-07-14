from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from total_coloring.graph import SimpleGraph
from total_coloring.graph6 import (
    GRAPH6_HEADER,
    Graph6Error,
    _decode_order,
    _encode_order,
    decode_graph6,
    encode_graph6,
)


@pytest.mark.parametrize(
    ("graph", "record"),
    [
        (SimpleGraph.from_edges(0, []), "?"),
        (SimpleGraph.from_edges(1, []), "@"),
        (SimpleGraph.from_edges(2, []), "A?"),
        (SimpleGraph.from_edges(2, [(0, 1)]), "A_"),
        (SimpleGraph.from_edges(3, [(0, 1), (0, 2), (1, 2)]), "Bw"),
        (SimpleGraph.from_edges(4, [(0, 1)]), "C_"),
    ],
)
def test_known_graph6_records(graph: SimpleGraph, record: str) -> None:
    assert encode_graph6(graph) == record
    assert decode_graph6(record) == graph
    assert graph.to_graph6() == record
    assert SimpleGraph.from_graph6(record) == graph


def test_graph6_header_bytes_and_line_endings() -> None:
    graph = SimpleGraph.from_edges(3, [(0, 2)])
    encoded = encode_graph6(graph, include_header=True)

    assert encoded.startswith(GRAPH6_HEADER)
    assert decode_graph6(encoded.encode("ascii") + b"\n") == graph
    assert decode_graph6((encoded + "\r\n").encode("ascii")) == graph


def test_medium_order_encoding() -> None:
    graph = SimpleGraph.from_edges(63, [])
    encoded = encode_graph6(graph)

    assert encoded.startswith("~??~")
    assert decode_graph6(encoded) == graph


def test_order_prefix_boundaries_without_allocating_large_graphs() -> None:
    long_prefix = _encode_order(258_048)

    assert long_prefix.startswith("~~")
    assert _decode_order(long_prefix) == (258_048, 8)
    with pytest.raises(Graph6Error, match="greater"):
        _encode_order(1 << 36)
    with pytest.raises(Graph6Error, match="empty"):
        _decode_order("")


@pytest.mark.parametrize(
    "record",
    [
        "",
        GRAPH6_HEADER,
        " ",
        "A",
        "A??",
        "A@",  # nonzero padding bit
        "~??",  # truncated medium order
        "~??@",  # noncanonical medium encoding of order 1
        "~~????",  # truncated long order
        "~~?????~",  # noncanonical long encoding of order 63
        "A?\nA?",
        "A?\n\n",
        "A? ",
    ],
)
def test_graph6_rejects_malformed_records(record: str) -> None:
    with pytest.raises(Graph6Error):
        decode_graph6(record)


def test_graph6_rejects_non_ascii_and_non_string_input() -> None:
    with pytest.raises(Graph6Error, match="ASCII"):
        decode_graph6(b"\xff")
    with pytest.raises(Graph6Error, match="str or bytes"):
        decode_graph6(bytearray(b"?"))  # type: ignore[arg-type]


@st.composite
def small_graphs(draw: st.DrawFn) -> SimpleGraph:
    order = draw(st.integers(min_value=0, max_value=12))
    candidates = [(u, v) for v in range(1, order) for u in range(v)]
    flags = draw(st.lists(st.booleans(), min_size=len(candidates), max_size=len(candidates)))
    return SimpleGraph.from_edges(
        order, (edge for edge, include in zip(candidates, flags, strict=True) if include)
    )


@given(small_graphs())
def test_graph6_property_round_trip(graph: SimpleGraph) -> None:
    assert decode_graph6(encode_graph6(graph)) == graph
