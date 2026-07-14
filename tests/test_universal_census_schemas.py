from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

import total_coloring.universal_census as universal
from total_coloring.census import ToolkitIdentity
from total_coloring.geng import GengIdentity, GengSpec
from total_coloring.graph import SimpleGraph
from total_coloring.schema_resources import read_schema_json
from total_coloring.universal_census import UniversalCensusConfig, run_universal_census

SCHEMA_NAMES = (
    "universal-census-record-v1.schema.json",
    "universal-census-manifest-v1.schema.json",
    "universal-census-completion-v1.schema.json",
)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_universal_census_schema_is_valid(name: str) -> None:
    Draft202012Validator.check_schema(read_schema_json(name))


def test_universal_artifacts_validate_against_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (1, 2), (2, 3), (0, 3)])

    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def fake_stream(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
        del spec, executable
        yield graph

    monkeypatch.setattr(universal, "geng_identity", fake_identity)
    monkeypatch.setattr(universal, "stream_geng", fake_stream)
    monkeypatch.setattr(
        universal,
        "resolve_geng",
        lambda executable="geng": Path("/synthetic") / Path(executable).name,
    )
    toolkit = ToolkitIdentity("test", "b" * 64, "CPython", "3.13.0")
    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4)), tmp_path, toolkit_identity=toolkit
    )

    record_validator = Draft202012Validator(
        read_schema_json("universal-census-record-v1.schema.json")
    )
    for raw in result.records_path.read_text(encoding="utf-8").splitlines():
        record_validator.validate(json.loads(raw))
    Draft202012Validator(read_schema_json("universal-census-manifest-v1.schema.json")).validate(
        json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    Draft202012Validator(read_schema_json("universal-census-completion-v1.schema.json")).validate(
        json.loads(result.completion_path.read_text(encoding="utf-8"))
    )
