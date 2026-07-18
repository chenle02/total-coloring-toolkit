"""Read the toolkit's versioned JSON schemas from source or an installed wheel.

The repository-level ``schemas/`` directory is the canonical source.  Hatch
copies that directory to ``total_coloring/_schemas`` when it builds a wheel.
Source checkouts use the canonical directory directly; installed distributions
use :mod:`importlib.resources` and never derive a caller-controlled path.
"""

from __future__ import annotations

import json
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import TypeAlias, cast

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class SchemaName(StrEnum):
    """The versioned schemas that form the supported public data contract."""

    CENSUS_COMPLETION_V1 = "census-completion-v1.schema.json"
    CENSUS_MANIFEST_V1 = "census-manifest-v1.schema.json"
    CENSUS_RECORD_V1 = "census-record-v1.schema.json"
    DATASET_MANIFEST_V2 = "dataset-manifest-v2.schema.json"
    GRAPH_V1 = "graph-v1.schema.json"
    PAIRED_HOLE_STATE_V1 = "paired-hole-state-v1.schema.json"
    TOTAL_COLORING_CERTIFICATE_V1 = "total-coloring-certificate-v1.schema.json"
    UNIVERSAL_CENSUS_COMPLETION_V1 = "universal-census-completion-v1.schema.json"
    UNIVERSAL_CENSUS_MANIFEST_V1 = "universal-census-manifest-v1.schema.json"
    UNIVERSAL_CENSUS_RECORD_V1 = "universal-census-record-v1.schema.json"
    UNIVERSAL_CENSUS_SUMMARY_V1 = "universal-census-summary-v1.schema.json"


_SCHEMA_NAMES = tuple(sorted(SchemaName, key=lambda name: name.value))
_RESOURCE_DIRECTORY = "_schemas"


def schema_names() -> tuple[SchemaName, ...]:
    """Return every supported schema name in stable lexical order."""

    return _SCHEMA_NAMES


def read_schema_bytes(name: SchemaName | str) -> bytes:
    """Return one supported schema as immutable UTF-8 JSON bytes.

    ``name`` is resolved through :class:`SchemaName`, so absolute paths,
    traversal segments, and unversioned filenames are rejected before any
    filesystem or package-resource lookup.
    """

    schema_name = _coerce_schema_name(name)
    source_path = _source_schema_path(schema_name)
    if source_path is not None:
        return source_path.read_bytes()

    resource = files("total_coloring").joinpath(_RESOURCE_DIRECTORY, schema_name.value)
    if not resource.is_file():
        raise FileNotFoundError(
            f"installed distribution is missing declared schema resource {schema_name.value!r}"
        )
    return resource.read_bytes()


def read_schema_json(name: SchemaName | str) -> dict[str, JSONValue]:
    """Parse one supported schema and return its top-level JSON object."""

    schema_name = _coerce_schema_name(name)
    try:
        value = json.loads(read_schema_bytes(schema_name))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - build corruption
        raise ValueError(f"schema resource {schema_name.value!r} is not valid JSON") from exc
    if not isinstance(value, dict):  # pragma: no cover - guarded by source and artifact tests
        raise ValueError(f"schema resource {schema_name.value!r} is not a JSON object")
    return cast(dict[str, JSONValue], value)


def _coerce_schema_name(name: SchemaName | str) -> SchemaName:
    if isinstance(name, SchemaName):
        return name
    if not isinstance(name, str):
        raise TypeError("schema name must be a SchemaName or str")
    try:
        return SchemaName(name)
    except ValueError as exc:
        raise ValueError(f"unknown schema name: {name!r}") from exc


def _source_schema_path(name: SchemaName) -> Path | None:
    """Locate the canonical tree only when running from a source checkout."""

    module_path = Path(__file__).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    expected_module = repository_root / "src" / "total_coloring" / "schema_resources.py"
    if (
        not (repository_root / "pyproject.toml").is_file()
        or expected_module.resolve() != module_path
    ):
        return None
    candidate = repository_root / "schemas" / name.value
    return candidate if candidate.is_file() else None


__all__ = [
    "JSONValue",
    "SchemaName",
    "read_schema_bytes",
    "read_schema_json",
    "schema_names",
]
