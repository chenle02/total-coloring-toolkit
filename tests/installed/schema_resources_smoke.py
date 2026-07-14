"""Standard-library smoke for an installed wheel, run from a foreign directory."""

from __future__ import annotations

import json
from importlib.resources import files

import total_coloring
from total_coloring.schema_resources import (
    SchemaName,
    read_schema_bytes,
    read_schema_json,
    schema_names,
)

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


def main() -> None:
    package_root = files(total_coloring)
    resource_root = package_root.joinpath("_schemas")
    assert resource_root.is_dir(), "wheel does not contain total_coloring/_schemas"
    assert tuple(name.value for name in schema_names()) == EXPECTED_NAMES

    packaged_names = tuple(
        sorted(item.name for item in resource_root.iterdir() if item.name.endswith(".json"))
    )
    assert packaged_names == EXPECTED_NAMES
    for name in SchemaName:
        raw = read_schema_bytes(name)
        assert raw == resource_root.joinpath(name.value).read_bytes()
        assert json.loads(raw) == read_schema_json(name)


if __name__ == "__main__":
    main()
