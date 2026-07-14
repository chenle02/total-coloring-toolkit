from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from total_coloring.certificates import TotalColoringCertificate
from total_coloring.graph import SimpleGraph


@st.composite
def small_graphs(draw: st.DrawFn) -> SimpleGraph:
    order = draw(st.integers(min_value=0, max_value=12))
    candidates = [(u, v) for v in range(1, order) for u in range(v)]
    flags = draw(st.lists(st.booleans(), min_size=len(candidates), max_size=len(candidates)))
    return SimpleGraph.from_edges(
        order, (edge for edge, include in zip(candidates, flags, strict=True) if include)
    )


@given(small_graphs())
def test_edge_input_orientation_and_order_do_not_change_graph(graph: SimpleGraph) -> None:
    reversed_input = [(v, u) for u, v in reversed(graph.edges)]

    rebuilt = SimpleGraph.from_edges(graph.order, reversed_input)
    assert rebuilt == graph
    assert rebuilt.fingerprint == graph.fingerprint


@given(small_graphs())
def test_degree_handshake_property(graph: SimpleGraph) -> None:
    assert sum(graph.degrees) == 2 * graph.size


@given(small_graphs())
def test_graph_json_property_round_trip(graph: SimpleGraph) -> None:
    assert SimpleGraph.from_json(graph.to_json()) == graph


@given(small_graphs())
def test_unique_color_per_element_is_always_a_valid_total_coloring(
    graph: SimpleGraph,
) -> None:
    palette_size = graph.order + graph.size
    certificate = TotalColoringCertificate.create(
        graph,
        palette_size,
        vertex_colors=range(graph.order),
        edge_colors=range(graph.order, palette_size),
    )

    assert certificate.verify(graph).valid
