from __future__ import annotations

from pathlib import Path

import pytest

from total_coloring.geng import GengSpec, geng_identity, stream_geng


def test_geng_arguments_are_deterministic_and_shell_free() -> None:
    spec = GengSpec(
        order=8,
        connected=True,
        min_degree=1,
        max_degree=5,
        shard_index=2,
        shard_count=7,
    )

    assert spec.arguments() == ("-q", "-c", "-d1", "-D5", "8", "2/7")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"order": -1},
        {"order": True},
        {"order": 4, "min_degree": 4},
        {"order": 4, "min_degree": 3, "max_degree": 2},
        {"order": 4, "shard_index": 0},
        {"order": 4, "shard_index": 1, "shard_count": 1},
        {"order": 4, "shard_index": 0, "shard_count": 0},
    ],
)
def test_geng_spec_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        GengSpec(**kwargs)  # type: ignore[arg-type]


def test_installed_geng_stream_and_identity() -> None:
    spec = GengSpec(order=4, connected=True)

    graphs = list(stream_geng(spec))
    identity = geng_identity(spec)

    assert len(graphs) == 6
    assert all(graph.order == 4 for graph in graphs)
    assert identity.executable == "geng"
    assert not Path(identity.executable).is_absolute()
    assert len(identity.sha256) == 64
    assert identity.arguments == spec.arguments()
