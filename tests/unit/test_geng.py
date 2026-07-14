from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from total_coloring.geng import (
    GengError,
    GengSpec,
    geng_identity,
    resolve_geng,
    stream_geng,
)


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


def test_default_resolution_accepts_debian_prefixed_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def fake_which(executable: str) -> str | None:
        requested.append(executable)
        return "/usr/bin/nauty-geng" if executable == "nauty-geng" else None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert resolve_geng() == Path("/usr/bin/nauty-geng")
    assert requested == ["geng", "nauty-geng"]


def test_default_resolution_prefers_upstream_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def fake_which(executable: str) -> str:
        requested.append(executable)
        return f"/opt/nauty/{executable}"

    monkeypatch.setattr(shutil, "which", fake_which)

    assert resolve_geng() == Path("/opt/nauty/geng")
    assert requested == ["geng"]


def test_explicit_missing_executable_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def fake_which(executable: str) -> None:
        requested.append(executable)
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    with pytest.raises(GengError, match="custom-geng"):
        resolve_geng("custom-geng")
    assert requested == ["custom-geng"]


def test_default_missing_error_names_both_portable_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda executable: None)

    with pytest.raises(GengError, match=r"geng, nauty-geng"):
        resolve_geng()


def test_stream_and_identity_with_hermetic_executable(tmp_path: Path) -> None:
    executable = tmp_path / "synthetic-geng"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' 'C~'\n", encoding="ascii")
    executable.chmod(0o755)
    spec = GengSpec(order=4)

    graphs = list(stream_geng(spec, executable=str(executable)))
    identity = geng_identity(spec, executable=str(executable))

    assert len(graphs) == 1
    assert graphs[0].order == 4
    assert graphs[0].size == 6
    assert identity.executable == executable.name
    assert identity.sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert identity.arguments == spec.arguments()
