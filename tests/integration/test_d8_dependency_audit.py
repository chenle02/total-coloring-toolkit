from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DIRECTORY = ROOT / "tests/reference"
sys.path.insert(0, str(REFERENCE_DIRECTORY))
import d8_dependency_reference as reference  # noqa: E402

CPP_SOURCE = ROOT / "auditors/d8_dependency_audit.cpp"
GOLDEN_RECEIPT = ROOT / "tests/fixtures/d8-dependency-counts-v1.json"
RECEIPT_SCHEMA = ROOT / "schemas/d8-dependency-pivot-audit-v1.schema.json"
SMALL_PROFILES = (
    "d8-a-w5",
    "d8-b-w6",
    "d8-c-frozen-w5",
    "d8-c-frozen-w6",
    "d8-c-mobile-w6",
)

# These were reproduced with ``reference.audit_profile(reference.PROFILES[id])``
# before being admitted as C++ regression constants.  Set
# ``TOTAL_COLORING_RECOMPUTE_D8_LARGE=1`` to repeat that independent Python census.
LARGE_PROFILE_GOLDENS: dict[str, dict[str, object]] = {
    "d8-c-frozen-w7": {
        "candidate_assignments": 3_240_000,
        "root_outdegree_at_least_two": 2_140_000,
        "root_reachable": 2_791_925,
        "dependency_admissible": 2_062_011,
        "incidence_admissible": 232_049,
        "initial_all_mobile_triples_fragile": 9_368,
        "minimum_pivot_depth_histogram": {
            "0": 222_681,
            "1": 7_856,
            "2": 1_212,
            "3": 160,
        },
        "pivot_resolved": 9_228,
        "pivot_unresolved": 140,
    },
    "d8-c-mobile-w7": {
        "candidate_assignments": 4_320_000,
        "root_outdegree_at_least_two": 3_095_000,
        "root_reachable": 3_871_131,
        "dependency_admissible": 2_999_927,
        "incidence_admissible": 193_713,
        "initial_all_mobile_triples_fragile": 246,
        "minimum_pivot_depth_histogram": {"0": 193_467, "1": 246},
        "pivot_resolved": 246,
        "pivot_unresolved": 0,
    },
}


@pytest.fixture(scope="session")
def d8_dependency_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is required for the independent C++ audit integration test")
    assert CPP_SOURCE.is_file(), f"missing C++ auditor: {CPP_SOURCE}"
    binary = tmp_path_factory.mktemp("d8-dependency-audit") / "d8_dependency_audit"
    completed = subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            "-Wconversion",
            "-Wsign-conversion",
            "-Wshadow",
            str(CPP_SOURCE),
            "-o",
            str(binary),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return binary


def _run(binary: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _canonical_json(text: str) -> dict[str, Any]:
    assert text.endswith("\n")
    assert text.count("\n") == 1
    payload = cast(dict[str, Any], json.loads(text))
    assert text == json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    return payload


def _successful_audit(binary: Path, *arguments: str) -> dict[str, Any]:
    completed = _run(binary, *arguments)
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = _canonical_json(completed.stdout)
    assert payload["kind"] == "d8_dependency_pivot_audit"
    assert payload["schema_version"] == 1
    assert payload["auditor_version"] == "1.0.0"
    assert payload["semantics_version"] == "exact-incidence-root-pivot-v1"
    assert payload["complete"] is True
    return payload


def _profiles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], payload["profiles"])


def _receipt_validator() -> Draft202012Validator:
    schema = cast(dict[str, Any], json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _golden_payload() -> dict[str, Any]:
    return _canonical_json(GOLDEN_RECEIPT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile_id", SMALL_PROFILES)
def test_cpp_matches_independent_python_reference(
    d8_dependency_binary: Path,
    profile_id: str,
) -> None:
    payload = _successful_audit(d8_dependency_binary, "--profile", profile_id)
    profiles = _profiles(payload)
    assert len(profiles) == 1
    actual = profiles[0]
    profile = reference.PROFILES[profile_id]
    expected = reference.audit_profile(profile)
    assert actual["profile_id"] == profile_id
    assert actual["vertex_count"] == profile.order
    assert actual["active_indegrees"] == [role.multiplicity for role in profile.active_roles]
    assert actual["mobile_triple_columns"] == [
        index for index, role in enumerate(profile.active_roles) if role.mobile_triple
    ]
    assert actual["inert_multiplicity"] == profile.inert_multiplicity
    assert actual["counts"] == expected.counts_dict()


def test_suite_profile_order_and_independently_reproduced_large_goldens(
    d8_dependency_binary: Path,
) -> None:
    completed = _run(d8_dependency_binary, "--suite")
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.encode("utf-8") == GOLDEN_RECEIPT.read_bytes()
    payload = _canonical_json(completed.stdout)
    _receipt_validator().validate(payload)
    profiles = _profiles(payload)
    assert [profile["profile_id"] for profile in profiles] == list(reference.PROFILES)
    by_id = {cast(str, profile["profile_id"]): profile for profile in profiles}
    for profile_id, expected_subset in LARGE_PROFILE_GOLDENS.items():
        counts = cast(dict[str, object], by_id[profile_id]["counts"])
        for key, expected in expected_subset.items():
            assert counts[key] == expected


def test_golden_receipt_accounting_identities() -> None:
    payload = _golden_payload()
    _receipt_validator().validate(payload)
    for profile in _profiles(payload):
        counts = cast(dict[str, Any], profile["counts"])
        fragile = cast(int, counts["initial_all_mobile_triples_fragile"])
        resolved = cast(int, counts["pivot_resolved"])
        unresolved = cast(int, counts["pivot_unresolved"])
        incidence = cast(int, counts["incidence_admissible"])
        histogram = cast(dict[str, int], counts["minimum_pivot_depth_histogram"])
        assert resolved + unresolved == fragile
        assert sum(histogram.values()) + unresolved == incidence


def test_schema_rejects_profile_descriptor_drift() -> None:
    drifted = cast(dict[str, Any], json.loads(json.dumps(_golden_payload())))
    _profiles(drifted)[0]["active_indegrees"] = [3, 3, 2, 2]
    assert not _receipt_validator().is_valid(drifted)


@pytest.mark.skipif(
    os.environ.get("TOTAL_COLORING_RECOMPUTE_D8_LARGE") != "1",
    reason="set TOTAL_COLORING_RECOMPUTE_D8_LARGE=1 to rerun the large Python census",
)
@pytest.mark.parametrize("profile_id", tuple(LARGE_PROFILE_GOLDENS))
def test_independent_python_reference_reproduces_large_goldens(profile_id: str) -> None:
    expected = LARGE_PROFILE_GOLDENS[profile_id]
    assert reference.audit_profile(reference.PROFILES[profile_id]).counts_dict() == expected


@pytest.mark.skipif(
    os.environ.get("TOTAL_COLORING_RECOMPUTE_D8_LARGE") != "1",
    reason="set TOTAL_COLORING_RECOMPUTE_D8_LARGE=1 to classify frozen-w7 pivot orbits",
)
def test_frozen_w7_unresolved_pivot_orbit_classification() -> None:
    classification = reference.classify_unresolved_pivot_orbits(
        reference.PROFILES["d8-c-frozen-w7"]
    )

    assert classification.unresolved_normalized_initial_states == 140
    assert classification.colored_pivot_orbits == 140
    assert classification.colored_pivot_orbit_size_histogram == ((56, 140),)
    assert classification.pivot_isomorphism_classes == (
        reference.PivotIsomorphismClass(
            certificate=reference.PivotOrbitCertificate(
                predecessor_masks=(13, 97, 18, 10, 1, 4),
                deficit_mask=112,
            ),
            normalized_initial_states=56,
            colored_pivot_orbits=56,
        ),
        reference.PivotIsomorphismClass(
            certificate=reference.PivotOrbitCertificate(
                predecessor_masks=(37, 88, 17, 9, 2, 4),
                deficit_mask=98,
            ),
            normalized_initial_states=56,
            colored_pivot_orbits=56,
        ),
        reference.PivotIsomorphismClass(
            certificate=reference.PivotOrbitCertificate(
                predecessor_masks=(41, 97, 18, 10, 1, 4),
                deficit_mask=84,
            ),
            normalized_initial_states=28,
            colored_pivot_orbits=28,
        ),
    )


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    (
        ((), "choose exactly one of --suite, --profile, --list-profiles, or --help"),
        (("--profile",), "--profile requires a profile ID"),
        (("--profile", ""), "profile IDs must be nonempty and unique"),
        (
            ("--profile", "d8-a-w5", "--profile", "d8-a-w5"),
            "profile IDs must be nonempty and unique",
        ),
        (("--profile", "not-a-profile"), "unknown profile ID: not-a-profile"),
        (("--not-an-option",), "unknown option: --not-an-option"),
        (
            ("--suite", "--profile", "d8-a-w5"),
            "choose exactly one of --suite, --profile, --list-profiles, or --help",
        ),
    ),
)
def test_malformed_cli_is_rejected_with_one_canonical_json_object(
    d8_dependency_binary: Path,
    arguments: tuple[str, ...],
    expected_error: str,
) -> None:
    completed = _run(d8_dependency_binary, *arguments)
    assert completed.returncode == 2
    assert completed.stdout == ""
    payload = _canonical_json(completed.stderr)
    assert payload == {
        "kind": "d8_dependency_pivot_audit_error",
        "schema_version": 1,
        "error": expected_error,
    }


@pytest.mark.parametrize("arguments", (("--help",), ("--list-profiles",)))
def test_informational_cli_is_deterministic_canonical_json(
    d8_dependency_binary: Path,
    arguments: tuple[str, ...],
) -> None:
    first = _run(d8_dependency_binary, *arguments)
    second = _run(d8_dependency_binary, *arguments)
    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    _canonical_json(first.stdout)


def test_repeatable_profile_option_uses_deterministic_builtin_order(
    d8_dependency_binary: Path,
) -> None:
    requested = ("d8-c-mobile-w6", "d8-a-w5", "d8-c-frozen-w5")
    arguments = tuple(item for profile_id in requested for item in ("--profile", profile_id))
    payload = _successful_audit(d8_dependency_binary, *arguments)
    expected = [profile_id for profile_id in reference.PROFILES if profile_id in requested]
    assert [profile["profile_id"] for profile in _profiles(payload)] == expected
