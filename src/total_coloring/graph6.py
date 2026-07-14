"""Strict dependency-free graph6 encoding and decoding.

Only one finite simple undirected graph record is accepted.  The decoder
permits the standard optional header and one trailing LF or CRLF, but rejects
other whitespace, extra records, nonzero padding bits, and noncanonical order
length encodings.
"""

from __future__ import annotations

from typing import Final

from total_coloring.graph import GraphFormatError, SimpleGraph

GRAPH6_HEADER: Final = ">>graph6<<"
_MIN_CHAR: Final = 63
_MAX_CHAR: Final = 126
_SHORT_ORDER_LIMIT: Final = 62
_MEDIUM_ORDER_LIMIT: Final = 258_047
_MAX_ORDER: Final = (1 << 36) - 1


class Graph6Error(GraphFormatError):
    """Raised when a graph6 record is invalid or non-canonical."""


def _encode_six_bits(value: int) -> str:
    return chr(value + _MIN_CHAR)


def _encode_order(order: int) -> str:
    if order <= _SHORT_ORDER_LIMIT:
        return _encode_six_bits(order)
    if order <= _MEDIUM_ORDER_LIMIT:
        return "~" + "".join(_encode_six_bits((order >> shift) & 0x3F) for shift in (12, 6, 0))
    if order <= _MAX_ORDER:
        return "~~" + "".join(
            _encode_six_bits((order >> shift) & 0x3F) for shift in (30, 24, 18, 12, 6, 0)
        )
    raise Graph6Error(f"graph6 cannot encode order greater than {_MAX_ORDER}")


def _decode_order(record: str) -> tuple[int, int]:
    if not record:
        raise Graph6Error("empty graph6 record")
    first = ord(record[0]) - _MIN_CHAR
    if first != 63:
        return first, 1
    if len(record) < 4:
        raise Graph6Error("truncated medium graph6 order")
    second = ord(record[1]) - _MIN_CHAR
    if second != 63:
        order = (second << 12) | ((ord(record[2]) - _MIN_CHAR) << 6) | (ord(record[3]) - _MIN_CHAR)
        if order <= _SHORT_ORDER_LIMIT:
            raise Graph6Error("noncanonical graph6 order encoding")
        return order, 4
    if len(record) < 8:
        raise Graph6Error("truncated long graph6 order")
    order = 0
    for character in record[2:8]:
        order = (order << 6) | (ord(character) - _MIN_CHAR)
    if order <= _MEDIUM_ORDER_LIMIT:
        raise Graph6Error("noncanonical graph6 order encoding")
    return order, 8


def _as_ascii_record(data: str | bytes) -> str:
    if isinstance(data, bytes):
        try:
            record = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise Graph6Error("graph6 input must contain ASCII bytes") from exc
    elif isinstance(data, str):
        record = data
    else:
        raise Graph6Error("graph6 input must be str or bytes")

    if record.endswith("\r\n"):
        record = record[:-2]
    elif record.endswith("\n"):
        record = record[:-1]
    if "\n" in record or "\r" in record:
        raise Graph6Error("expected exactly one graph6 record")
    if record.startswith(GRAPH6_HEADER):
        record = record[len(GRAPH6_HEADER) :]
    if not record:
        raise Graph6Error("empty graph6 record")
    if any(not _MIN_CHAR <= ord(character) <= _MAX_CHAR for character in record):
        raise Graph6Error("graph6 characters must be in ASCII range 63..126")
    return record


def encode_graph6(graph: SimpleGraph, *, include_header: bool = False) -> str:
    """Encode one numbered simple graph as canonical graph6 text."""

    prefix = GRAPH6_HEADER if include_header else ""
    output = [prefix, _encode_order(graph.order)]
    edge_set = set(graph.edges)
    value = 0
    width = 0

    for upper in range(1, graph.order):
        for lower in range(upper):
            value = (value << 1) | int((lower, upper) in edge_set)
            width += 1
            if width == 6:
                output.append(_encode_six_bits(value))
                value = 0
                width = 0

    if width:
        output.append(_encode_six_bits(value << (6 - width)))
    return "".join(output)


def decode_graph6(data: str | bytes) -> SimpleGraph:
    """Decode one strict graph6 record into a :class:`SimpleGraph`."""

    record = _as_ascii_record(data)
    order, offset = _decode_order(record)
    bit_count = order * (order - 1) // 2
    payload_length = (bit_count + 5) // 6
    actual_length = len(record) - offset
    if actual_length != payload_length:
        raise Graph6Error(
            f"graph6 payload length mismatch: expected {payload_length}, got {actual_length}"
        )

    payload = record[offset:]
    if payload and bit_count % 6:
        unused_bits = 6 - bit_count % 6
        if (ord(payload[-1]) - _MIN_CHAR) & ((1 << unused_bits) - 1):
            raise Graph6Error("graph6 padding bits must be zero")

    edges: list[tuple[int, int]] = []
    bit_index = 0
    for upper in range(1, order):
        for lower in range(upper):
            chunk = ord(payload[bit_index // 6]) - _MIN_CHAR
            bit = (chunk >> (5 - bit_index % 6)) & 1
            if bit:
                edges.append((lower, upper))
            bit_index += 1
    return SimpleGraph.from_edges(order, edges)


__all__ = ["GRAPH6_HEADER", "Graph6Error", "decode_graph6", "encode_graph6"]
