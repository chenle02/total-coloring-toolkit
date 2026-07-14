from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from total_coloring.geng import GengSpec, geng_identity, resolve_geng, stream_geng
from total_coloring.graph6 import encode_graph6
from total_coloring.universal_census import UniversalCensusConfig, run_universal_census
from total_coloring.universal_shards import validate_completed_universal_shard_set


def test_installed_geng_stream_and_identity() -> None:
    if shutil.which("geng") is None and shutil.which("nauty-geng") is None:
        pytest.skip("install nauty geng to run the external generator integration test")

    spec = GengSpec(order=4, connected=True)
    resolved = resolve_geng()
    graphs = list(stream_geng(spec))
    identity = geng_identity(spec)

    assert len(graphs) == 6
    assert all(graph.order == 4 for graph in graphs)
    assert identity.executable == resolved.name
    assert not Path(identity.executable).is_absolute()
    assert len(identity.sha256) == 64
    assert identity.arguments == spec.arguments()


def test_installed_geng_split_depth_shards_equal_the_direct_stream() -> None:
    if shutil.which("geng") is None and shutil.which("nauty-geng") is None:
        pytest.skip("install nauty geng to run the external generator integration test")

    direct = {encode_graph6(graph) for graph in stream_geng(GengSpec(6))}
    union: set[str] = set()
    for index in range(4):
        shard = {
            encode_graph6(graph)
            for graph in stream_geng(GengSpec(6, shard_index=index, shard_count=4, split_depth=2))
        }
        assert union.isdisjoint(shard)
        union.update(shard)

    assert len(direct) == 156
    assert union == direct


def test_installed_geng_completed_shards_pass_exact_union_validation(tmp_path: Path) -> None:
    if shutil.which("geng") is None and shutil.which("nauty-geng") is None:
        pytest.skip("install nauty geng to run the external generator integration test")

    directories = []
    for index in range(2):
        directory = tmp_path / f"shard-{index}"
        run_universal_census(
            UniversalCensusConfig(
                GengSpec(4, shard_index=index, shard_count=2, split_depth=2),
                checkpoint_interval=2,
            ),
            directory,
        )
        directories.append(directory)

    result = validate_completed_universal_shard_set(directories)

    assert result.order == 4
    assert result.shard_count == 2
    assert result.split_depth == 2
    assert result.record_count == 11
    assert result.counts.total == 11
