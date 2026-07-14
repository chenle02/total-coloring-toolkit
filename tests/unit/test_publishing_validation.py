from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from test_publishing import _config, _make_destination, _make_source, _write_json

import total_coloring.publishing as publishing
from total_coloring.publishing import (
    BundleVerificationError,
    PublicationConfig,
    PublishingError,
    RepositoryStateError,
    _format_matches,
    _load_json,
    _parse_checksums,
    _parse_semver,
    _safe_relative_path,
    _type_matches,
    _validate_document,
    plan_promotion,
)


def test_bundle_json_loader_fails_closed_on_resource_exhaustion_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge_integer = tmp_path / "huge-integer.json"
    huge_integer.write_text('{"value":' + "1" * 5_000 + "}", encoding="utf-8")
    with pytest.raises(BundleVerificationError, match="integer exceeds"):
        _load_json(huge_integer, "fixture")

    nested = tmp_path / "nested.json"
    nested.write_text("[" * 129 + "0" + "]" * 129, encoding="utf-8")
    with pytest.raises(BundleVerificationError, match="nesting exceeds"):
        _load_json(nested, "fixture")

    monkeypatch.setattr(publishing, "_MAX_BUNDLE_JSON_BYTES", 4)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"12345")
    with pytest.raises(BundleVerificationError, match="JSON exceeds 4 bytes"):
        _load_json(oversized, "fixture")


@pytest.mark.parametrize("value", ["", "../escape", "/absolute", "a\\b", "a/./b", "a\nb"])
def test_safe_relative_path_rejects_ambiguous_values(value: str) -> None:
    assert _safe_relative_path(value) is None


def test_safe_relative_path_accepts_normalized_path() -> None:
    assert _safe_relative_path("results/run-01.json") == PurePosixPath("results/run-01.json")


def test_config_rejects_unsafe_control_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="manifest_path"):
        PublicationConfig(
            source_root=tmp_path,
            destination_root=tmp_path,
            manifest_path=PurePosixPath("../manifest.json"),
        )


@pytest.mark.parametrize(
    ("instance", "expected", "answer"),
    [
        ({}, "object", True),
        ([], "array", True),
        ("x", "string", True),
        (1, "integer", True),
        (True, "integer", False),
        (1.5, "number", True),
        (False, "boolean", True),
        (None, "null", True),
    ],
)
def test_supported_json_schema_types(instance: object, expected: str, answer: bool) -> None:
    assert _type_matches(instance, expected) is answer


def test_unknown_json_schema_type_fails_closed() -> None:
    with pytest.raises(BundleVerificationError, match="unsupported JSON Schema type"):
        _type_matches("x", "mystery")


@pytest.mark.parametrize(
    ("value", "format_name", "answer"),
    [
        ("a" * 64, "sha256", True),
        ("A" * 64, "sha256", False),
        ("results/a.json", "relative-path", True),
        ("../a.json", "relative-path", False),
        ("https://example.org/data", "uri", True),
        ("file:///tmp/data", "uri", False),
        ("https://[bad/data", "uri", False),
        ("https://example.org/archive.tar.gz", "https-uri", True),
        ("HTTPS://example.org/archive.tar.gz", "https-uri", False),
        ("http://example.org/archive.tar.gz", "https-uri", False),
        ("https://user@example.org/archive.tar.gz", "https-uri", False),
        ("https://example.org:443/archive.tar.gz", "https-uri", False),
        ("https://Example.org/archive.tar.gz", "https-uri", False),
        ("https://example.org/a/../archive.tar.gz", "https-uri", False),
        ("https://example.org/a/%2e%2e/archive.tar.gz", "https-uri", False),
        ("https://example.org/a/%41/archive.tar.gz", "https-uri", False),
        ("https://example.org/a/%2F/archive.tar.gz", "https-uri", False),
        ("https://example.org/a/%zz/archive.tar.gz", "https-uri", False),
        ("https://example.org/a/%ff/archive.tar.gz", "https-uri", False),
        ("https://example.org//archive.tar.gz", "https-uri", False),
        ("https://example.org/archive.tar.gz?download=1", "https-uri", False),
        ("https://example.org/a b/archive.tar.gz", "https-uri", False),
        ("https://localhost/archive.tar.gz", "https-uri", False),
        ("https://-bad.example/archive.tar.gz", "https-uri", False),
        ("https://[bad/archive.tar.gz", "https-uri", False),
        ("2026-07-14T03:00:00Z", "date-time", True),
        ("2026-07-14", "date-time", False),
        ("not-a-date", "date-time", False),
    ],
)
def test_supported_json_schema_formats(value: str, format_name: str, answer: bool) -> None:
    assert _format_matches(value, format_name) is answer


def test_unknown_json_schema_format_fails_closed() -> None:
    with pytest.raises(BundleVerificationError, match="unsupported JSON Schema format"):
        _format_matches("x", "hostname")


@pytest.mark.parametrize(
    "value",
    (
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3-rc..1",
        "1.2.3-",
        "1.2.3+build.1",
        "1.2.3\n",
        "1.2.3\r",
    ),
)
def test_release_semver_is_canonical_and_forbids_build_metadata(value: str) -> None:
    assert _parse_semver("1.2.3-rc.1") == (("1", "2", "3"), ("rc", "1"))
    with pytest.raises(BundleVerificationError, match="canonical SemVer"):
        _parse_semver(value)


def test_release_semver_rejects_long_near_miss_without_backtracking_blowup() -> None:
    with pytest.raises(BundleVerificationError, match="canonical SemVer"):
        _parse_semver("1.2.3-" + "a" * 20_000 + "+")


@pytest.mark.parametrize(
    ("instance", "schema", "message"),
    [
        ("x", [], "schema node must be an object"),
        ("x", {"$schema": "draft-unknown"}, "unsupported \\$schema"),
        ("x", {"properties": []}, "properties must be an object"),
        ("x", {"items": []}, "schema node must be an object"),
        ("x", {"type": 7}, "type must be a string or string array"),
        ("x", {"type": "integer"}, "expected integer"),
        ("x", {"const": "y"}, "expected constant"),
        ("x", {"enum": ["y"]}, "is not permitted"),
        ("x", {"type": "string", "minLength": 2}, "minimum length"),
        ("x", {"type": "string", "pattern": "^y$"}, "does not match"),
        ("http://", {"type": "string", "format": "uri"}, "invalid uri"),
        (-1, {"type": "integer", "minimum": 0}, "minimum is 0"),
        ({}, {"type": "object", "required": ["x"]}, "missing required"),
        ({}, {"type": "object", "required": "x"}, "required must be a string array"),
        (
            {"extra": 1},
            {"type": "object", "properties": {}, "additionalProperties": False},
            "property is not allowed",
        ),
        (["x"], {"type": "array", "items": {"type": "integer"}}, "expected integer"),
        ([], {"type": "array", "minItems": 1}, "minimum item count"),
        ([1, 2], {"type": "array", "maxItems": 1}, "maximum item count"),
        ([], {"type": "array", "minItems": -1}, "must be a nonnegative integer"),
        ([], {"type": "array", "minItems": 2, "maxItems": 1}, "may not exceed"),
    ],
)
def test_schema_subset_rejects_invalid_documents(
    instance: object, schema: object, message: str
) -> None:
    with pytest.raises(BundleVerificationError, match=message):
        _validate_document(instance, schema, "fixture")


def test_schema_subset_accepts_union_type_and_nested_property() -> None:
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": ["string", "null"]}},
        "additionalProperties": False,
    }
    _validate_document({"value": None}, schema, "fixture")


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("not-a-checksum\n", "expected '<sha256>  <path>'"),
        (f"{'a' * 64}  ../escape\n", "unsafe checksum path"),
        (f"{'a' * 64}  z\n{'b' * 64}  a\n", "path-sorted"),
        (f"{'a' * 64}  a\n{'b' * 64}  a\n", "duplicate checksum path"),
    ],
)
def test_checksum_parser_fails_closed(tmp_path: Path, contents: str, message: str) -> None:
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(contents, encoding="utf-8")
    with pytest.raises(BundleVerificationError, match=message):
        _parse_checksums(checksums)


def test_checksum_parser_rejects_invalid_utf8_and_oversized_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_bytes(b"\xff")
    with pytest.raises(BundleVerificationError, match="valid UTF-8"):
        _parse_checksums(checksums)

    monkeypatch.setattr(publishing, "_MAX_CHECKSUM_FILE_BYTES", 4)
    checksums.write_bytes(b"12345")
    with pytest.raises(BundleVerificationError, match="exceeds 4 bytes"):
        _parse_checksums(checksums)

    monkeypatch.setattr(publishing, "_MAX_CHECKSUM_FILE_BYTES", 16)
    monkeypatch.setattr(publishing, "_MAX_CHECKSUM_LINE_BYTES", 4)
    checksums.write_bytes(b"1234\n")
    with pytest.raises(BundleVerificationError, match="line exceeding 4 bytes including LF"):
        _parse_checksums(checksums)


def test_invalid_manifest_json_is_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "manifests/dataset-manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(BundleVerificationError, match="cannot read JSON"):
        plan_promotion(_config(source, destination))


def test_manifest_must_be_an_object(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    _write_json(source / "manifests/dataset-manifest.json", [])
    with pytest.raises(BundleVerificationError, match="manifest must be a JSON object"):
        plan_promotion(_config(source, destination))


def test_manifest_schema_must_be_safe_and_present(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["$schema"] = "../manifest.json"
    _write_json(manifest_path, manifest)
    with pytest.raises(BundleVerificationError, match="safe path under schemas"):
        plan_promotion(_config(source, destination))


def test_missing_and_symlinked_control_files_are_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "SHA256SUMS").unlink()
    with pytest.raises(BundleVerificationError, match="regular file not found"):
        plan_promotion(_config(source, destination))

    (source / "SHA256SUMS").symlink_to(source / "schemas/result-v1.schema.json")
    with pytest.raises(BundleVerificationError, match="symlink is forbidden"):
        plan_promotion(_config(source, destination))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.__setitem__("managed_roots", "results"), "expected array"),
        (
            lambda manifest: manifest.__setitem__("managed_roots", ["reports", "a/b"]),
            "expected constant",
        ),
        (
            lambda manifest: manifest.__setitem__("managed_roots", ["results", "reports"]),
            "expected constant",
        ),
        (lambda manifest: manifest.__setitem__("artifacts", {}), "expected array"),
        (lambda manifest: manifest.__setitem__("artifacts", ["bad"]), "expected object"),
    ],
)
def test_manifest_structural_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    _write_json(manifest_path, manifest)
    monkeypatch.setattr(
        publishing,
        "_managed_files",
        lambda *_args, **_kwargs: pytest.fail(
            "managed-root traversal must not run for a schema-invalid manifest"
        ),
    )
    with pytest.raises(BundleVerificationError, match=message):
        plan_promotion(_config(source, destination))


def test_candidate_cannot_weaken_the_trusted_manifest_schema(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    schema_path = source / "schemas/dataset-manifest-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"] = {}
    _write_json(schema_path, schema)
    with pytest.raises(BundleVerificationError, match="trusted canonical schema"):
        plan_promotion(_config(source, destination))


@pytest.mark.parametrize(
    "version",
    (
        "01.0.0",
        "1.00.0",
        "1.0.00",
        "1.0.0-01",
        "1.0.0-rc..1",
        "1.0.0+build.1",
        "1.0.0\n",
        "1.0.0\r",
    ),
)
def test_v1_manifest_rejects_noncanonical_release_versions(tmp_path: Path, version: str) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release"]["version"] = version
    _write_json(manifest_path, manifest)
    with pytest.raises(BundleVerificationError, match="does not match"):
        plan_promotion(_config(source, destination))


def test_artifact_byte_count_and_record_count_are_enforced(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    manifest_path = source / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["bytes"] += 1
    _write_json(manifest_path, manifest)
    with pytest.raises(BundleVerificationError, match="byte count mismatch"):
        plan_promotion(_config(source, destination))

    manifest["artifacts"][0]["bytes"] -= 1
    manifest["artifacts"][0]["records"] = 2
    _write_json(manifest_path, manifest)
    with pytest.raises(BundleVerificationError, match="record count mismatch"):
        plan_promotion(_config(source, destination))


def test_unlisted_source_artifact_is_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "reports/scratch.txt").write_text("not reviewed\n", encoding="utf-8")
    with pytest.raises(BundleVerificationError, match="managed inventory mismatch"):
        plan_promotion(_config(source, destination))


def test_missing_managed_root_and_symlinked_child_are_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "reports").rmdir()
    with pytest.raises(BundleVerificationError, match="managed root"):
        plan_promotion(_config(source, destination))

    (source / "reports").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "reports/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BundleVerificationError, match="directory symlink"):
        plan_promotion(_config(source, destination))


def test_checksum_manifest_disagreement_is_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    (source / "SHA256SUMS").write_text("# missing artifact\n", encoding="utf-8")
    with pytest.raises(BundleVerificationError, match="does not match the manifest"):
        plan_promotion(_config(source, destination))


def test_source_and_destination_must_differ_and_exist(tmp_path: Path) -> None:
    same = tmp_path / "same"
    same.mkdir()
    with pytest.raises(PublishingError, match="must differ"):
        plan_promotion(PublicationConfig(source_root=same, destination_root=same))
    with pytest.raises(PublishingError, match="cannot resolve"):
        plan_promotion(
            PublicationConfig(
                source_root=tmp_path / "missing-source",
                destination_root=tmp_path / "missing-destination",
            )
        )


def test_destination_must_be_git_worktree_root(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    nested = destination / "nested"
    nested.mkdir()
    with pytest.raises(RepositoryStateError, match="worktree root"):
        plan_promotion(_config(source, nested))


def test_destination_must_have_a_committed_head(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = tmp_path / "destination"
    destination.mkdir()
    for directory in ("results", "reports"):
        (destination / directory).mkdir()
    subprocess_result = subprocess.run(
        ["git", "-C", str(destination), "init", "-b", "main"],
        check=True,
        capture_output=True,
    )
    assert subprocess_result.returncode == 0
    with pytest.raises(RepositoryStateError, match="rev-parse HEAD failed"):
        plan_promotion(_config(source, destination))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_destination_target_parent_may_not_be_a_symlink(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    destination = _make_destination(tmp_path / "destination")
    outside = tmp_path / "outside-schemas"
    outside.mkdir()
    (destination / "schemas").symlink_to(outside, target_is_directory=True)
    subprocess.run(
        ["git", "-C", str(destination), "add", "schemas"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(destination), "commit", "-m", "track unsafe symlink"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(RepositoryStateError, match="contains a symlink"):
        plan_promotion(_config(source, destination))
