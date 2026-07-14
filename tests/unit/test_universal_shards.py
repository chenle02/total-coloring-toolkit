from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import total_coloring.cli as cli
import total_coloring.universal_shards as shards
from total_coloring.census import ToolkitIdentity
from total_coloring.geng import GengIdentity, GengSpec
from total_coloring.graph import SimpleGraph, canonical_json_bytes
from total_coloring.universal_census import (
    UniversalCensusConfig,
    UniversalCensusCounts,
    UniversalCensusRunResult,
    UniversalCensusValidation,
)
from total_coloring.universal_shards import UniversalShardSetError


def _graph(size: int) -> SimpleGraph:
    edges = tuple((left, right) for left in range(4) for right in range(left + 1, 4))[:size]
    return SimpleGraph.from_edges(4, edges)


def _write_artifacts(directory: Path, *, index: int) -> UniversalCensusRunResult:
    directory.mkdir()
    records = directory / "records.jsonl"
    records.write_bytes(f"shard-{index}\n".encode("ascii"))
    records_sha256 = hashlib.sha256(records.read_bytes()).hexdigest()
    manifest_value = {
        "artifacts": {
            "records_bytes": records.stat().st_size,
            "records_path": "records.jsonl",
            "records_sha256": records_sha256,
        }
    }
    manifest = directory / "manifest.json"
    manifest.write_bytes(canonical_json_bytes(manifest_value) + b"\n")
    completion = directory / "completion.json"
    completion.write_bytes(canonical_json_bytes({"complete": True}) + b"\n")
    counts = UniversalCensusCounts(skipped=1)
    return UniversalCensusRunResult(
        run_fingerprint=f"{index + 1:064x}",
        record_count=1,
        partition_count=0,
        counts=counts,
        resumed_records=1,
        records_path=records,
        manifest_path=manifest,
        completion_path=completion,
    )


def _validation(
    directory: Path,
    *,
    index: int,
    split_depth: int = 2,
    toolkit: ToolkitIdentity | None = None,
) -> UniversalCensusValidation:
    spec = GengSpec(4, shard_index=index, shard_count=2, split_depth=split_depth)
    return UniversalCensusValidation(
        result=_write_artifacts(directory, index=index),
        config=UniversalCensusConfig(spec),
        generator=GengIdentity("geng", "a" * 64, spec.arguments()),
        toolkit=toolkit or ToolkitIdentity("test", "b" * 64, "CPython", "3.13.0"),
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    validations: dict[str, UniversalCensusValidation],
    streams: dict[int | None, tuple[SimpleGraph, ...]],
) -> None:
    def fake_validate(
        directory: str | Path, *, executable: str = "geng"
    ) -> UniversalCensusValidation:
        assert executable == "/synthetic/geng"
        return validations[Path(directory).name]

    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        assert executable == "/synthetic/geng"
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def fake_stream(spec: GengSpec, *, executable: str = "geng") -> Any:
        assert executable == "/synthetic/geng"
        yield from streams[spec.shard_index]

    monkeypatch.setattr(shards, "resolve_geng", lambda executable="geng": Path("/synthetic/geng"))
    monkeypatch.setattr(shards, "validate_completed_universal_census", fake_validate)
    monkeypatch.setattr(shards, "geng_identity", fake_identity)
    monkeypatch.setattr(shards, "stream_geng", fake_stream)


def _completed_shard_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[shards.UniversalShardSetValidation, list[Path]]:
    first = _validation(tmp_path / "shard-0", index=0)
    second = _validation(tmp_path / "shard-1", index=1)
    validations = {"shard-0": first, "shard-1": second}
    streams = {0: (_graph(0),), 1: (_graph(1),), None: (_graph(0), _graph(1))}
    _install_fakes(monkeypatch, validations, streams)
    directories = [tmp_path / "shard-0", tmp_path / "shard-1"]
    result = shards.validate_completed_universal_shard_set(directories)
    return result, directories


def test_complete_shard_set_is_replayed_sorted_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _validation(tmp_path / "shard-0", index=0)
    second = _validation(tmp_path / "shard-1", index=1)
    validations = {"shard-0": first, "shard-1": second}
    streams = {0: (_graph(0),), 1: (_graph(1),), None: (_graph(0), _graph(1))}
    _install_fakes(monkeypatch, validations, streams)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = shards.validate_completed_universal_shard_set(
        [tmp_path / "shard-1", tmp_path / "shard-0"],
        executable="requested-geng",
        max_union_graphs=2,
    )

    assert result.order == 4
    assert result.shard_count == 2
    assert result.split_depth == 2
    assert result.record_count == result.counts.total == 2
    assert result.partition_count == result.check_evaluations == 0
    assert [receipt.shard_index for receipt in result.receipts] == [0, 1]
    assert result.to_dict()["totals"] == {
        "check_evaluations": 0,
        "counts": {
            "candidate_unsat": 0,
            "error": 0,
            "skipped": 2,
            "unknown": 0,
            "verified_all": 0,
        },
        "partition_count": 0,
        "record_count": 2,
        "records_bytes": 16,
    }
    assert before == {path: path.read_bytes() for path in before}


def test_artifact_inventory_recheck_is_read_only_and_does_not_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, directories = _completed_shard_set(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def forbidden_replay(*args: object, **kwargs: object) -> Any:
        raise AssertionError("artifact recheck must not invoke geng")

    monkeypatch.setattr(shards, "stream_geng", forbidden_replay)
    monkeypatch.setattr(shards, "geng_identity", forbidden_replay)

    assert shards.recheck_universal_shard_artifact_inventory(result, directories) is result
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("case", ["extra", "symlink"])
def test_artifact_inventory_recheck_requires_exact_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    result, directories = _completed_shard_set(tmp_path, monkeypatch)
    if case == "extra":
        (directories[0] / "unexpected.txt").write_text("extra\n", encoding="utf-8")
        message = "artifact inventory must be exactly"
    else:
        records = directories[0] / "records.jsonl"
        target = tmp_path / "saved-records.jsonl"
        target.write_bytes(records.read_bytes())
        records.unlink()
        records.symlink_to(target)
        message = "regular non-symlink file"

    with pytest.raises(UniversalShardSetError, match=message):
        shards.recheck_universal_shard_artifact_inventory(result, directories)


@pytest.mark.parametrize("case", ["in-place", "atomic-replace"])
def test_artifact_inventory_recheck_detects_mutation_after_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    result, directories = _completed_shard_set(tmp_path, monkeypatch)
    original_hash = shards._hash_bound_artifact
    changed = False

    def mutate_after_hash(
        directory: shards._OpenShardDirectory, name: str
    ) -> shards._ArtifactSnapshot:
        nonlocal changed
        snapshot = original_hash(directory, name)
        if directory.shard_index == 0 and name == "records.jsonl" and not changed:
            path = directory.path / name
            changed = True
            if case == "in-place":
                status = path.stat()
                time.sleep(0.01)
                path.write_bytes(b"mutate!\n")
                os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))
            else:
                replacement = directory.path / "replacement.tmp"
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(path.stat().st_mode)
                os.replace(replacement, path)
        return snapshot

    monkeypatch.setattr(shards, "_hash_bound_artifact", mutate_after_hash)

    with pytest.raises(UniversalShardSetError, match="changed after hashing"):
        shards.recheck_universal_shard_artifact_inventory(result, directories)


@pytest.mark.parametrize("case", ["missing", "duplicate", "split", "toolkit"])
def test_shard_set_rejects_incomplete_or_mixed_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    first = _validation(tmp_path / "shard-0", index=0)
    second_index = 0 if case == "duplicate" else 1
    second_split = 3 if case == "split" else 2
    second_toolkit = (
        ToolkitIdentity("foreign", "c" * 64, "CPython", "3.13.0") if case == "toolkit" else None
    )
    second = _validation(
        tmp_path / "shard-1",
        index=second_index,
        split_depth=second_split,
        toolkit=second_toolkit,
    )
    validations = {"shard-0": first, "shard-1": second}
    streams = {0: (_graph(0),), 1: (_graph(1),), None: (_graph(0), _graph(1))}
    _install_fakes(monkeypatch, validations, streams)
    directories = [tmp_path / "shard-0"] if case == "missing" else list(validations)

    with pytest.raises(UniversalShardSetError):
        shards.validate_completed_universal_shard_set(directories)


@pytest.mark.parametrize("case", ["overlap", "direct", "cap", "mutation"])
def test_shard_set_rejects_bad_union_limits_and_concurrent_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    first = _validation(tmp_path / "shard-0", index=0)
    second = _validation(tmp_path / "shard-1", index=1)
    validations = {"shard-0": first, "shard-1": second}
    streams = {
        0: (_graph(0),),
        1: (_graph(0),) if case == "overlap" else (_graph(1),),
        None: (_graph(0), _graph(2)) if case == "direct" else (_graph(0), _graph(1)),
    }
    _install_fakes(monkeypatch, validations, streams)
    if case == "mutation":
        original_status = first.result.records_path.stat()

        def mutate(spec: GengSpec, *, executable: str = "geng") -> Any:
            assert executable == "/synthetic/geng"
            if spec.shard_index is None:
                first.result.records_path.write_bytes(b"mutated\n")
                first.result.records_path.touch()
                first.result.records_path.chmod(original_status.st_mode)
                first.result.records_path.parent.touch()
                os.utime(
                    first.result.records_path,
                    ns=(original_status.st_atime_ns, original_status.st_mtime_ns),
                )
            yield from streams[spec.shard_index]

        monkeypatch.setattr(shards, "stream_geng", mutate)

    with pytest.raises(UniversalShardSetError):
        shards.validate_completed_universal_shard_set(
            list(validations),
            max_union_graphs=1 if case == "cap" else 10,
        )


def test_shard_set_rejects_mutation_immediately_after_final_records_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _validation(tmp_path / "shard-0", index=0)
    second = _validation(tmp_path / "shard-1", index=1)
    validations = {"shard-0": first, "shard-1": second}
    streams = {0: (_graph(0),), 1: (_graph(1),), None: (_graph(0), _graph(1))}
    _install_fakes(monkeypatch, validations, streams)
    original_hash = shards._sha256_path
    records = first.result.records_path
    original_status = records.stat()
    mutated = False

    def mutate_after_hash(path: Path) -> str:
        nonlocal mutated
        digest = original_hash(path)
        if path == records and not mutated:
            mutated = True
            time.sleep(0.05)
            records.write_bytes(b"changed\n")
            records.chmod(original_status.st_mode)
            os.utime(records, ns=(original_status.st_atime_ns, original_status.st_mtime_ns))
        return digest

    monkeypatch.setattr(shards, "_sha256_path", mutate_after_hash)

    with pytest.raises(UniversalShardSetError, match="changed during final hashing"):
        shards.validate_completed_universal_shard_set([tmp_path / name for name in validations])


@pytest.mark.parametrize("directories,cap", [([], 1), (["run"], 0), ("run", 1)])
def test_shard_set_rejects_invalid_api_inputs(directories: Any, cap: int) -> None:
    with pytest.raises(ValueError):
        shards.validate_completed_universal_shard_set(directories, max_union_graphs=cap)


def test_shard_validation_cli_emits_canonical_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    class Result:
        counts = UniversalCensusCounts()

        def to_dict(self) -> dict[str, object]:
            return {"order": 8, "shard_count": 64}

    def fake_validate(directories: list[str], *, executable: str, max_union_graphs: int) -> Result:
        seen.update(
            directories=directories,
            executable=executable,
            max_union_graphs=max_union_graphs,
        )
        return Result()

    monkeypatch.setattr(cli, "validate_completed_universal_shard_set", fake_validate)
    exit_code = cli.main(
        [
            "universal-validate-shards",
            "--run",
            str(tmp_path / "one"),
            "--run",
            str(tmp_path / "two"),
            "--geng",
            "pinned-geng",
            "--max-union-graphs",
            "123",
        ]
    )

    assert exit_code == cli.EXIT_SUCCESS
    assert seen == {
        "directories": [str(tmp_path / "one"), str(tmp_path / "two")],
        "executable": "pinned-geng",
        "max_union_graphs": 123,
    }
    assert json.loads(capsys.readouterr().out) == {
        "order": 8,
        "shard_count": 64,
        "status": "complete",
    }


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (UniversalCensusCounts(candidate_unsat=1), cli.EXIT_NO_WITNESS),
        (UniversalCensusCounts(unknown=1), cli.EXIT_UNKNOWN),
        (UniversalCensusCounts(error=1), cli.EXIT_ERROR),
    ],
)
def test_shard_validation_cli_propagates_adverse_status_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    counts: UniversalCensusCounts,
    expected: int,
) -> None:
    class Result:
        def __init__(self, result_counts: UniversalCensusCounts) -> None:
            self.counts = result_counts

        def to_dict(self) -> dict[str, object]:
            return {"counts": self.counts.to_dict()}

    result = Result(counts)
    monkeypatch.setattr(
        cli, "validate_completed_universal_shard_set", lambda *args, **kwargs: result
    )

    assert cli.main(["universal-validate-shards", "--run", "one"]) == expected
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_uniform_config_ignores_only_shard_index() -> None:
    first = UniversalCensusConfig(GengSpec(4, shard_index=0, shard_count=2, split_depth=2))
    second = replace(first, geng=replace(first.geng, shard_index=1))

    assert shards._uniform_config(first) == shards._uniform_config(second)
