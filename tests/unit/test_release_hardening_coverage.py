from __future__ import annotations

import copy
import errno
import io
import json
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast

import pytest
from test_universal_release import make_runs, release_config

import total_coloring.publishing as publishing
import total_coloring.universal_census as census
import total_coloring.universal_release as release
from total_coloring.census import CensusFormatError
from total_coloring.graph import SimpleGraph, canonical_json_bytes
from total_coloring.publishing import BundleVerificationError, PublicationFile
from total_coloring.universal_census import (
    validate_completed_universal_transcript,
)
from total_coloring.universal_release import ArchiveMemberReceipt, UniversalReleaseError

Mutation = Callable[[dict[str, Any]], None]


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _export_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, release.UniversalReleaseResult, dict[str, Any]]:
    run = make_runs(tmp_path / "runs", monkeypatch, (2,))[0]
    exported = release.export_universal_release(
        (run,),
        release_config(tmp_path),
        executable="geng",
    )
    return run, exported, _json_object(exported.summary_path)


@pytest.mark.parametrize(
    ("instance", "schema", "message"),
    [
        (0, {"unsupported": True}, "unsupported schema keyword"),
        (0, {"enum": []}, "enum must be a nonempty array"),
        ([], {"maxItems": True}, "maxItems must be a nonnegative integer"),
        (0, {"minimum": True}, "minimum must be a JSON number"),
        (0, {"maximum": False}, "maximum must be a JSON number"),
        (0, {"minimum": 2, "maximum": 1}, "minimum may not exceed maximum"),
        (17, {"type": "integer", "maximum": 16}, "maximum is 16"),
    ],
)
def test_schema_inspection_rejects_invalid_limits_and_definitions(
    instance: object, schema: object, message: str
) -> None:
    with pytest.raises(BundleVerificationError, match=message):
        publishing._validate_document(instance, schema, "candidate")


def test_publisher_summary_semantics_fail_closed_independently_of_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run, _exported, summary = _export_one(tmp_path, monkeypatch)
    producer = summary["producer"]
    replay = summary["replay_archive"]
    assert isinstance(producer, dict)
    assert isinstance(replay, dict)
    repository = producer["repository"]
    commit = producer["commit"]
    created_utc = summary["created_utc"]
    external_name = replay["external_artifact"]
    assert isinstance(repository, str)
    assert isinstance(commit, str)
    assert isinstance(created_utc, str)
    assert isinstance(external_name, str)
    external: Mapping[str, Mapping[str, object]] = {external_name: replay}

    receipts = publishing._validate_universal_summary_semantics(
        summary,
        "summary",
        expected_repository=repository,
        expected_commit=commit,
        release_created_utc=created_utc,
        external_artifacts=external,
    )
    assert tuple(receipt.path.name for receipt in receipts) == (
        "completion.json",
        "manifest.json",
        "records.jsonl",
    )

    with pytest.raises(BundleVerificationError, match="must be an object"):
        publishing._validate_universal_summary_semantics(
            [],
            "summary",
            expected_repository=repository,
            expected_commit=commit,
            release_created_utc=created_utc,
            external_artifacts=external,
        )

    def undeclared_archive(value: dict[str, Any]) -> None:
        value["replay_archive"]["external_artifact"] = "archives/undeclared.tar.gz"

    def duplicate_order(value: dict[str, Any]) -> None:
        value["runs"].append(copy.deepcopy(value["runs"][0]))

    def duplicate_fingerprint(value: dict[str, Any]) -> None:
        run = copy.deepcopy(value["runs"][0])
        run["order"] = 3
        run["generator_arguments"] = ["-q", "3"]
        for member in run["members"].values():
            basename = PurePosixPath(member["path"]).name
            member["path"] = f"order-03/{basename}"
        value["runs"].append(run)

    def vacuous_claim(value: dict[str, Any]) -> None:
        run = value["runs"][0]
        record_count = run["record_count"]
        run["counts"] = {
            "candidate_unsat": 0,
            "error": 0,
            "skipped": record_count,
            "unknown": 0,
            "verified_all": 0,
        }
        run["partition_count"] = 0
        run["check_evaluations"] = 0
        value["totals"]["counts"] = copy.deepcopy(run["counts"])
        value["totals"]["partition_count"] = 0
        value["totals"]["check_evaluations"] = 0

    cases: tuple[tuple[Mutation, str], ...] = (
        (lambda value: value.update(producer=[]), "producer: expected object"),
        (lambda value: value.update(replay_archive=[]), "invalid external binding"),
        (undeclared_archive, "not externally declared"),
        (lambda value: value["replay_archive"].update(bytes=-1), "does not match"),
        (lambda value: value.update(checks=[]), "checks: expected nonempty array"),
        (lambda value: value["checks"].__setitem__(0, []), "invalid check"),
        (lambda value: value.update(runs=[]), "runs: expected nonempty array"),
        (lambda value: value["runs"].__setitem__(0, []), r"runs\[0\]: expected object"),
        (lambda value: value["runs"][0].update(order=17), "between 1 and 16"),
        (
            lambda value: value["runs"][0]["counts"].update(verified_all=True),
            "expected nonnegative integer",
        ),
        (
            lambda value: value["runs"][0]["members"].update(completion=[]),
            "members.completion: expected object",
        ),
        (
            lambda value: value["runs"][0]["members"]["completion"].update(bytes=True),
            "invalid completion receipt",
        ),
        (duplicate_order, "orders must be unique and sorted"),
        (duplicate_fingerprint, "fingerprints must be unique"),
        (lambda value: value["totals"].update(record_count=99), "sum over runs"),
        (lambda value: value.update(claims=[]), "exactly one finite claim"),
        (lambda value: value["claims"].__setitem__(0, []), "invalid claim"),
        (vacuous_claim, "verified claim is vacuous"),
    )
    for mutate, message in cases:
        candidate = copy.deepcopy(summary)
        mutate(candidate)
        with pytest.raises(BundleVerificationError, match=message):
            publishing._validate_universal_summary_semantics(
                candidate,
                "summary",
                expected_repository=repository,
                expected_commit=commit,
                release_created_utc=created_utc,
                external_artifacts=external,
            )


def test_release_summary_archive_contract_rejects_malformed_inventory_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run, _exported, summary = _export_one(tmp_path, monkeypatch)
    receipts, runs, archive_bytes, archive_sha256 = release._summary_archive_contract(summary)
    assert len(receipts) == 3
    assert len(runs) == 1
    assert archive_bytes > 0
    assert len(archive_sha256) == 64

    def too_many_runs(value: dict[str, Any]) -> None:
        value["runs"] = [copy.deepcopy(value["runs"][0]) for _ in range(257)]

    def incomplete_members(value: dict[str, Any]) -> None:
        value["runs"][0]["members"].pop("records")

    def duplicate_receipts(value: dict[str, Any]) -> None:
        value["runs"].append(copy.deepcopy(value["runs"][0]))

    cases: tuple[tuple[Mutation, str], ...] = (
        (lambda value: value.update(replay_archive=None), "complete summary"),
        (lambda value: value["replay_archive"].update(bytes=True), "receipt is malformed"),
        (lambda value: value.update(runs=[]), "between 1 and 256 runs"),
        (too_many_runs, "between 1 and 256 runs"),
        (lambda value: value["runs"].__setitem__(0, None), "run 0 must be an object"),
        (lambda value: value["runs"][0].update(order=True), "contract is malformed"),
        (incomplete_members, "member map is incomplete"),
        (
            lambda value: value["runs"][0]["members"].update(completion=None),
            "receipt is malformed",
        ),
        (
            lambda value: value["runs"][0]["members"]["records"].update(path="wrong/records.jsonl"),
            "receipt is noncanonical",
        ),
        (duplicate_receipts, "receipts must be unique"),
    )
    for mutate, message in cases:
        candidate = copy.deepcopy(summary)
        mutate(candidate)
        with pytest.raises(UniversalReleaseError, match=message):
            release._summary_archive_contract(candidate)

    run = cast(Mapping[str, object], summary["runs"][0])
    malformed_provenance = copy.deepcopy(summary)
    malformed_provenance["scope"] = None
    with pytest.raises(UniversalReleaseError, match="provenance objects"):
        release._expected_summary_descriptor(malformed_provenance, run)
    malformed_checks = copy.deepcopy(summary)
    malformed_checks["checks"] = [None]
    with pytest.raises(UniversalReleaseError, match="check matrix"):
        release._expected_summary_descriptor(malformed_checks, run)


class _FailingRecordStream:
    def readline(self, _limit: int = -1) -> bytes:
        raise ValueError("stream is closed")


def test_transcript_validation_rejects_metadata_stream_and_record_protocol_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_runs(tmp_path / "runs", monkeypatch, (2,))[0]
    manifest_bytes = (run / "manifest.json").read_bytes()
    completion_bytes = (run / "completion.json").read_bytes()
    records_bytes = (run / "records.jsonl").read_bytes()
    manifest = _json_object(run / "manifest.json")
    completion = _json_object(run / "completion.json")
    record = json.loads(records_bytes)
    assert isinstance(record, dict)

    with pytest.raises(ValueError, match="must be bytes"):
        validate_completed_universal_transcript(
            cast(bytes, bytearray(manifest_bytes)),
            completion_bytes,
            io.BytesIO(records_bytes),
        )
    with pytest.raises(ValueError, match="binary readable stream"):
        validate_completed_universal_transcript(
            manifest_bytes,
            completion_bytes,
            cast(IO[bytes], object()),
        )
    with pytest.raises(CensusFormatError, match="record stream failed"):
        validate_completed_universal_transcript(
            manifest_bytes,
            completion_bytes,
            cast(IO[bytes], _FailingRecordStream()),
        )

    metadata_cases = (
        (b"{\n", "invalid universal manifest"),
        (b"[]\n", "must be a JSON object"),
        (b'{ "x": 1 }\n', "not canonical JSON"),
    )
    for malformed, message in metadata_cases:
        with pytest.raises(CensusFormatError, match=message):
            validate_completed_universal_transcript(
                malformed,
                completion_bytes,
                io.BytesIO(records_bytes),
            )
    with monkeypatch.context() as bounded:
        bounded.setattr(census, "MAX_CENSUS_METADATA_BYTES", 2)
        with pytest.raises(CensusFormatError, match="metadata limit"):
            validate_completed_universal_transcript(
                b"{}\n",
                completion_bytes,
                io.BytesIO(records_bytes),
            )

    bad_manifest_version = copy.deepcopy(manifest)
    bad_manifest_version["schema_version"] = 99
    with pytest.raises(CensusFormatError, match="manifest version"):
        validate_completed_universal_transcript(
            canonical_json_bytes(bad_manifest_version) + b"\n",
            completion_bytes,
            io.BytesIO(records_bytes),
        )
    bad_completion_version = copy.deepcopy(completion)
    bad_completion_version["schema_version"] = 99
    with pytest.raises(CensusFormatError, match="completion schema_version"):
        validate_completed_universal_transcript(
            manifest_bytes,
            canonical_json_bytes(bad_completion_version) + b"\n",
            io.BytesIO(records_bytes),
        )
    bad_provenance = copy.deepcopy(manifest)
    bad_provenance["provenance"] = []
    with pytest.raises(CensusFormatError, match="provenance must be an object"):
        validate_completed_universal_transcript(
            canonical_json_bytes(bad_provenance) + b"\n",
            completion_bytes,
            io.BytesIO(records_bytes),
        )
    oversized_order = copy.deepcopy(manifest)
    oversized_order["provenance"]["config"]["generator_spec"]["order"] = 17
    oversized_order["provenance"]["generator"]["arguments"] = ["-q", "17"]
    with pytest.raises(CensusFormatError, match="orders 1 through 16"):
        validate_completed_universal_transcript(
            canonical_json_bytes(oversized_order) + b"\n",
            completion_bytes,
            io.BytesIO(records_bytes),
        )

    with pytest.raises(CensusFormatError, match="record 0 must be an object"):
        validate_completed_universal_transcript(
            manifest_bytes,
            completion_bytes,
            io.BytesIO(b"[]\n"),
        )
    wrong_order = copy.deepcopy(record)
    wrong_order["order"] = 3
    with pytest.raises(CensusFormatError, match="order must equal the run order"):
        validate_completed_universal_transcript(
            manifest_bytes,
            completion_bytes,
            io.BytesIO(canonical_json_bytes(wrong_order) + b"\n"),
        )
    pretty_record = (
        json.dumps(record, separators=(", ", ": "), sort_keys=True).encode("utf-8") + b"\n"
    )
    with pytest.raises(CensusFormatError, match="completed JSONL is not canonical"):
        validate_completed_universal_transcript(
            manifest_bytes,
            completion_bytes,
            io.BytesIO(pretty_record),
        )

    with pytest.raises(CensusFormatError, match="generator ended before"):
        census._validate_universal_census_transcript(
            manifest_bytes,
            completion_bytes,
            io.BytesIO(records_bytes),
            regenerated=iter(()),
        )
    non_graphs = cast(Iterator[SimpleGraph], iter((object(),)))
    with pytest.raises(CensusFormatError, match="yielded a non-graph"):
        census._validate_universal_census_transcript(
            manifest_bytes,
            completion_bytes,
            io.BytesIO(records_bytes),
            regenerated=non_graphs,
        )
    with pytest.raises(CensusFormatError, match="disagrees with completed record"):
        census._validate_universal_census_transcript(
            manifest_bytes,
            completion_bytes,
            io.BytesIO(records_bytes),
            regenerated=iter((SimpleGraph.from_edges(2, ()),)),
        )


@pytest.mark.parametrize(
    "receipt",
    [
        ArchiveMemberReceipt(PurePosixPath("../escape"), 0, "a" * 64),
        ArchiveMemberReceipt(PurePosixPath(".hidden/member"), 0, "a" * 64),
        ArchiveMemberReceipt(PurePosixPath("order-02/records.jsonl"), True, "a" * 64),
        ArchiveMemberReceipt(PurePosixPath("order-02/records.jsonl"), 0, "bad"),
    ],
)
def test_archive_layout_rejects_malformed_receipts(receipt: ArchiveMemberReceipt) -> None:
    with pytest.raises(UniversalReleaseError, match="receipt is malformed"):
        release._canonical_tar_layout((receipt,))


def test_archive_structure_rejects_unencodable_receipts_and_nonfiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unencodable = ArchiveMemberReceipt(PurePosixPath("x" * 101), 0, "a" * 64)
    with pytest.raises(UniversalReleaseError, match="cannot be encoded as USTAR"):
        release._canonical_tar_layout((unencodable,))

    with monkeypatch.context() as bounded:
        bounded.setattr(release, "MAX_UNIVERSAL_RECORD_BYTES", 4)
        with pytest.raises(UniversalReleaseError, match="JSONL record exceeds"):
            release._update_jsonl_line_length(3, b"xx")

    archive_directory = tmp_path / "archive-directory"
    archive_directory.mkdir()
    with pytest.raises(UniversalReleaseError, match="regular non-symlink file"):
        release._validate_replay_archive_structure(archive_directory, ())

    target = tmp_path / "target.tar.gz"
    target.write_bytes(b"payload")
    link = tmp_path / "link.tar.gz"
    link.symlink_to(target)
    with pytest.raises(UniversalReleaseError, match="cannot safely open replay archive"):
        release._validate_replay_archive_structure(link, ())


def test_release_stage_cleanup_and_rollback_preserve_foreign_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = tmp_path / "bundle"
    archive_path = tmp_path / "archive.tar.gz"
    with release._open_output_targets(bundle_path, archive_path) as (bundle, archive):
        directory_stage = tmp_path / ".bundle-stage"
        (directory_stage / "nested").mkdir(parents=True)
        (directory_stage / "nested" / "artifact").write_text("staged\n", encoding="utf-8")
        directory_identity = release._EntryIdentity.from_stat(os.lstat(directory_stage))
        assert (
            release._cleanup_owned_staging(bundle, directory_stage.name, directory_identity) is None
        )
        assert not directory_stage.exists()

        foreign_stage = tmp_path / ".foreign-stage"
        foreign_stage.write_text("owned\n", encoding="utf-8")
        held = os.open(foreign_stage, os.O_RDONLY)
        try:
            expected = release._EntryIdentity.from_stat(os.fstat(held))
            foreign_stage.unlink()
            foreign_stage.write_text("foreign\n", encoding="utf-8")
            error = release._cleanup_owned_staging(bundle, foreign_stage.name, expected)
            assert error is not None and "foreign staging replacement preserved" in error
            assert foreign_stage.read_text(encoding="utf-8") == "foreign\n"
            with pytest.raises(UniversalReleaseError, match="identity changed before install"):
                release._install_output_noreplace(
                    bundle,
                    staging_name=foreign_stage.name,
                    expected=expected,
                )
        finally:
            os.close(held)

        assert (
            release._rollback_installed_output(
                archive,
                staging_name=".unused-rollback",
                expected=directory_identity,
            )
            is None
        )
        archive_path.write_text("installed\n", encoding="utf-8")
        archive_identity = release._EntryIdentity.from_stat(os.lstat(archive_path))
        with monkeypatch.context() as failed_rename:
            failed_rename.setattr(
                release,
                "_rename_noreplace",
                lambda **_kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "injected")),
            )
            error = release._rollback_installed_output(
                archive,
                staging_name=".archive-rollback",
                expected=archive_identity,
            )
        assert error is not None and "renameat2 failed" in error
        assert archive_path.read_text(encoding="utf-8") == "installed\n"

    translations = (
        (errno.ENOSYS, "unsupported"),
        (errno.EXDEV, "crossed filesystems"),
        (errno.EIO, "renameat2 failed"),
    )
    for error_number, message in translations:
        translated = release._translate_noreplace_error(
            OSError(error_number, "injected"), action="rollback"
        )
        assert message in str(translated)


def test_publisher_stage_and_rollback_preserve_foreign_entries(tmp_path: Path) -> None:
    root = tmp_path / "destination"
    root.mkdir()
    with publishing._pinned_destination_hierarchy(root) as directories:
        parent = directories[()]
        item = PublicationFile(
            path=PurePosixPath("reports/artifact.json"),
            kind="artifact",
            bytes=1,
            sha256="a" * 64,
            destination_sha256=None,
        )

        foreign_stage = root / ".artifact-stage"
        foreign_stage.write_text("owned\n", encoding="utf-8")
        held = os.open(foreign_stage, os.O_RDONLY)
        try:
            expected = publishing._EntryIdentity.from_stat(os.fstat(held))
            prepared = publishing._PreparedPublication(
                item=item,
                parent=parent,
                leaf="artifact.json",
                stage_name=foreign_stage.name,
                stage_descriptor=-1,
                stage_identity=expected,
            )
            foreign_stage.unlink()
            foreign_stage.write_text("foreign\n", encoding="utf-8")
            error = publishing._cleanup_owned_stage(prepared, expected)
            assert error is not None and "foreign stage replacement preserved" in error
            assert foreign_stage.read_text(encoding="utf-8") == "foreign\n"
        finally:
            os.close(held)

        final_path = root / "installed.json"
        final_path.write_text("installed\n", encoding="utf-8")
        final_identity = publishing._EntryIdentity.from_stat(os.lstat(final_path))
        rollback_path = root / ".occupied-rollback"
        rollback_path.write_text("foreign rollback target\n", encoding="utf-8")
        installed = publishing._PreparedPublication(
            item=item,
            parent=parent,
            leaf=final_path.name,
            stage_name=rollback_path.name,
            stage_descriptor=-1,
            stage_identity=final_identity,
            installed=True,
        )
        error = publishing._rollback_prepared(installed)
        assert error is not None and "foreign rollback destination preserved" in error
        assert final_path.read_text(encoding="utf-8") == "installed\n"
        assert rollback_path.read_text(encoding="utf-8") == "foreign rollback target\n"
