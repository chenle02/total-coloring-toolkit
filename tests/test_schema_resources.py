from __future__ import annotations

from pathlib import Path

import pytest

from total_coloring.schema_resources import (
    SchemaName,
    read_schema_bytes,
    read_schema_json,
    schema_names,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NAMES = (
    "census-completion-v1.schema.json",
    "census-manifest-v1.schema.json",
    "census-record-v1.schema.json",
    "graph-v1.schema.json",
    "total-coloring-certificate-v1.schema.json",
    "universal-census-completion-v1.schema.json",
    "universal-census-manifest-v1.schema.json",
    "universal-census-record-v1.schema.json",
)


def test_schema_api_enumerates_exact_public_contract() -> None:
    assert tuple(name.value for name in schema_names()) == EXPECTED_NAMES
    assert tuple(sorted(path.name for path in (REPOSITORY_ROOT / "schemas").glob("*.json"))) == (
        EXPECTED_NAMES
    )
    assert not (REPOSITORY_ROOT / "src" / "total_coloring" / "_schemas").exists()


@pytest.mark.parametrize("name", schema_names())
def test_schema_api_matches_canonical_source_bytes(name: SchemaName) -> None:
    expected = (REPOSITORY_ROOT / "schemas" / name.value).read_bytes()
    assert read_schema_bytes(name) == expected
    assert read_schema_bytes(name.value) == expected
    assert read_schema_json(name)["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize(
    "name",
    [
        "../graph-v1.schema.json",
        "/tmp/graph-v1.schema.json",
        "graph-v2.schema.json",
        "README.md",
        "",
    ],
)
def test_schema_api_rejects_unlisted_and_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="unknown schema name"):
        read_schema_bytes(name)


def test_schema_api_rejects_non_string_name() -> None:
    with pytest.raises(TypeError, match="SchemaName or str"):
        read_schema_bytes(1)  # type: ignore[arg-type]
