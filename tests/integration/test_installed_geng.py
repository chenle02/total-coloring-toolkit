from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from total_coloring.geng import GengSpec, geng_identity, resolve_geng, stream_geng


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
