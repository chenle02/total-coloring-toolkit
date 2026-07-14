from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

import total_coloring.census as census
from total_coloring.auxiliary import AuxiliarySearchResult
from total_coloring.census import CensusConfig, ToolkitIdentity, run_census
from total_coloring.geng import GengIdentity, GengSpec
from total_coloring.graph import SimpleGraph
from total_coloring.schema_resources import read_schema_json
from total_coloring.solver import SearchLimits, SolveStatus

SCHEMA_NAMES = (
    "census-record-v1.schema.json",
    "census-manifest-v1.schema.json",
    "census-completion-v1.schema.json",
)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_census_schema_is_valid(name: str) -> None:
    Draft202012Validator.check_schema(read_schema_json(name))


def test_published_artifacts_validate_against_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (1, 2), (2, 3), (0, 3)])

    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def fake_stream(spec: GengSpec, *, executable: str = "geng") -> tuple[SimpleGraph, ...]:
        del spec, executable
        return (graph,)

    def fake_search(
        graph: SimpleGraph,
        color_count: int,
        *,
        limits_per_partition: SearchLimits | None = None,
        max_partitions: int | None = None,
    ) -> AuxiliarySearchResult:
        del limits_per_partition, max_partitions
        return AuxiliarySearchResult(
            SolveStatus.CANDIDATE_UNSAT,
            graph.fingerprint,
            color_count,
            1,
            1,
            1,
            0,
            None,
            "schema test candidate",
        )

    monkeypatch.setattr(census, "geng_identity", fake_identity)
    monkeypatch.setattr(census, "stream_geng", fake_stream)
    monkeypatch.setattr(census, "search_auxiliary_extensions", fake_search)
    toolkit = ToolkitIdentity("test", "b" * 64, "CPython", "3.13.0")
    result = run_census(CensusConfig(GengSpec(4)), tmp_path, toolkit_identity=toolkit)

    record_schema = Draft202012Validator(read_schema_json("census-record-v1.schema.json"))
    for raw in result.records_path.read_text(encoding="utf-8").splitlines():
        record_schema.validate(json.loads(raw))
    Draft202012Validator(read_schema_json("census-manifest-v1.schema.json")).validate(
        json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    Draft202012Validator(read_schema_json("census-completion-v1.schema.json")).validate(
        json.loads(result.completion_path.read_text(encoding="utf-8"))
    )
