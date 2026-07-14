from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import total_coloring.universal_census as universal
from total_coloring.auxiliary import (
    EquitablePartition,
    construct_auxiliary_graph,
    iter_equitable_partitions,
)
from total_coloring.backends import SolverBackend, solve_with_backend
from total_coloring.census import (
    CensusError,
    CensusFormatError,
    CensusLockError,
    CensusResumeError,
    ToolkitIdentity,
    _atomic_write,
)
from total_coloring.geng import GengError, GengIdentity, GengSpec
from total_coloring.graph import GraphFormatError, SimpleGraph, canonical_json_bytes
from total_coloring.solver import SearchLimits, SolveResult, SolveStatus
from total_coloring.universal_census import (
    DEFAULT_UNIVERSAL_CHECKS,
    DeterministicSearchStats,
    UniversalCensusConfig,
    UniversalCensusCounts,
    UniversalCensusRecord,
    UniversalCensusRunResult,
    UniversalCensusStatus,
    UniversalCheckResult,
    UniversalCheckSpec,
    UniversalPartitionResult,
    count_equitable_partitions_dp,
    run_universal_census,
    validate_completed_universal_census,
    validate_completed_universal_transcript,
)

TEST_TOOLKIT = ToolkitIdentity("test", "b" * 64, "CPython", "3.13.0")


class _MalformedRecordStream:
    def __init__(self, value: object) -> None:
        self.value = value

    def readline(self, _limit: int) -> object:
        return self.value


def cycle(order: int) -> SimpleGraph:
    return SimpleGraph.from_edges(
        order, ((vertex, (vertex + 1) % order) for vertex in range(order))
    )


def complete(order: int) -> SimpleGraph:
    return SimpleGraph.from_edges(
        order,
        ((left, right) for left in range(order) for right in range(left + 1, order)),
    )


@pytest.fixture(autouse=True)
def isolate_from_installed_geng(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        universal,
        "resolve_geng",
        lambda executable="geng": Path("/synthetic") / Path(executable).name,
    )


def patch_generator(
    monkeypatch: pytest.MonkeyPatch,
    graphs: tuple[SimpleGraph, ...],
) -> None:
    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def fake_stream(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
        del spec, executable
        yield from graphs

    monkeypatch.setattr(universal, "geng_identity", fake_identity)
    monkeypatch.setattr(universal, "stream_geng", fake_stream)


def read_records(path: Path) -> list[UniversalCensusRecord]:
    return [UniversalCensusRecord.from_json(line) for line in path.read_bytes().splitlines()]


def test_default_universal_record_contains_every_partition_and_replays() -> None:
    graph = cycle(4)
    config = UniversalCensusConfig(GengSpec(4))

    record = universal._process_graph(
        config=config,
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
    )

    assert record.status is UniversalCensusStatus.VERIFIED_ALL
    assert record.partition_count == len(record.partitions) == 2
    assert record.check_specs == DEFAULT_UNIVERSAL_CHECKS
    assert all(
        check.status is SolveStatus.WITNESS and check.auxiliary_edge_colors is not None
        for partition in record.partitions
        for check in partition.checks
    )
    assert "elapsed" not in record.to_json()
    assert UniversalCensusRecord.from_json(record.to_json()) == record


def test_record_parser_rejects_partition_and_witness_tampering() -> None:
    record = universal._process_graph(
        config=UniversalCensusConfig(GengSpec(4)),
        run_fingerprint="c" * 64,
        index=0,
        graph=cycle(4),
    )
    baseline = cast(dict[str, Any], record.to_dict())

    missing_partition = copy.deepcopy(baseline)
    missing_partition["partitions"].pop()
    missing_partition["partition_count"] = 1
    with pytest.raises(
        CensusFormatError, match=r"independent complement-matching DP|every canonical"
    ):
        UniversalCensusRecord.from_dict(missing_partition)

    reordered = copy.deepcopy(baseline)
    reordered["partitions"].reverse()
    with pytest.raises(CensusFormatError, match=r"reordered|discontinuous"):
        UniversalCensusRecord.from_dict(reordered)

    duplicated = copy.deepcopy(baseline)
    duplicated["partitions"][1] = copy.deepcopy(duplicated["partitions"][0])
    duplicated["partitions"][1]["index"] = 1
    with pytest.raises(CensusFormatError, match=r"duplicated|reordered|unique"):
        UniversalCensusRecord.from_dict(duplicated)

    bad_digest = copy.deepcopy(baseline)
    bad_digest["partitions"][0]["checks"][0]["problem_digest"] = "d" * 64
    with pytest.raises(CensusFormatError, match="problem digest"):
        UniversalCensusRecord.from_dict(bad_digest)

    bad_assignment = copy.deepcopy(baseline)
    assignment = bad_assignment["partitions"][0]["checks"][0]["auxiliary_edge_colors"]
    assignment[:] = [0] * len(assignment)
    with pytest.raises(CensusFormatError, match="witness"):
        UniversalCensusRecord.from_dict(bad_assignment)

    wrong_outcome = copy.deepcopy(baseline)
    wrong_outcome["outcome_code"] = "candidate_partition_nonextension"
    with pytest.raises(CensusFormatError, match="outcome_code"):
        UniversalCensusRecord.from_dict(wrong_outcome)

    with pytest.raises(CensusFormatError, match="duplicate"):
        UniversalCensusRecord.from_json('{"schema_version":1,"schema_version":2}')


def test_candidate_and_limited_transcripts_never_claim_verified_all() -> None:
    graph = cycle(4)
    candidate = universal._process_graph(
        config=UniversalCensusConfig(
            GengSpec(4), checks=(UniversalCheckSpec(SolverBackend.DSATUR, 0),)
        ),
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
    )
    limited = universal._process_graph(
        config=UniversalCensusConfig(
            GengSpec(4),
            limits_per_check=SearchLimits(max_nodes=1),
        ),
        run_fingerprint="d" * 64,
        index=0,
        graph=graph,
    )

    assert candidate.status is UniversalCensusStatus.CANDIDATE_UNSAT
    assert all(
        check.auxiliary_edge_colors is None
        for partition in candidate.partitions
        for check in partition.checks
    )
    assert limited.status in {UniversalCensusStatus.UNKNOWN, UniversalCensusStatus.ERROR}


def test_cross_backend_offset_one_disagreement_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_solve = solve_with_backend

    def disagree(problem: object, *, backend: SolverBackend, limits: object) -> SolveResult:
        solved = real_solve(problem, backend=backend, limits=limits)  # type: ignore[arg-type]
        if backend is SolverBackend.STATIC:
            return SolveResult(
                SolveStatus.CANDIDATE_UNSAT,
                solved.problem_digest,
                None,
                solved.stats,
                "synthetic independent exhaustion",
            )
        return solved

    monkeypatch.setattr(universal, "solve_with_backend", disagree)
    record = universal._process_graph(
        config=UniversalCensusConfig(GengSpec(4)),
        run_fingerprint="c" * 64,
        index=0,
        graph=cycle(4),
    )

    assert record.status is UniversalCensusStatus.ERROR
    assert record.outcome_code == "backend_status_disagreement"
    assert UniversalCensusRecord.from_json(record.to_json()) == record

    falsely_verified = record.to_dict()
    falsely_verified["status"] = "verified_all"
    falsely_verified["outcome_code"] = "verified_all_partitions"
    with pytest.raises(CensusFormatError, match="status"):
        UniversalCensusRecord.from_dict(falsely_verified)


def test_run_accounts_for_every_graph_and_publishes_replayable_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphs = (cycle(4), SimpleGraph.from_edges(4, ()))
    patch_generator(monkeypatch, graphs)

    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        tmp_path,
        toolkit_identity=TEST_TOOLKIT,
    )

    assert result.record_count == 2
    assert result.partition_count == 2
    assert result.counts.verified_all == 1
    assert result.counts.skipped == 1
    records = read_records(result.records_path)
    assert [record.index for record in records] == [0, 1]
    assert records[1].eligible is False
    assert records[1].partitions == ()
    assert result.manifest_path.is_file() and result.completion_path.is_file()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["provenance"]["objective"] == "universal_auxiliary_extension"
    assert manifest["partition_count"] == 2


def test_same_inputs_produce_byte_identical_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4), complete(4)))
    config = UniversalCensusConfig(GengSpec(4, shard_index=1, shard_count=3))

    first = run_universal_census(config, tmp_path / "first", toolkit_identity=TEST_TOOLKIT)
    second = run_universal_census(config, tmp_path / "second", toolkit_identity=TEST_TOOLKIT)

    assert first.run_fingerprint == second.run_fingerprint
    assert first.records_path.read_bytes() == second.records_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.completion_path.read_bytes() == second.completion_path.read_bytes()


def test_public_completed_run_validation_reconstructs_provenance_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4), complete(4)))
    run_directory = tmp_path / "run"
    expected = run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        run_directory,
        toolkit_identity=TEST_TOOLKIT,
    )
    before = {path.name: path.read_bytes() for path in run_directory.iterdir()}

    validated = validate_completed_universal_census(run_directory)

    assert validated.result == replace(expected, resumed_records=expected.record_count)
    assert validated.config == UniversalCensusConfig(GengSpec(4))
    assert validated.generator == GengIdentity("geng", "a" * 64, ("-q", "4"))
    assert validated.toolkit == TEST_TOOLKIT
    assert {path.name: path.read_bytes() for path in run_directory.iterdir()} == before
    assert not (run_directory / ".census.lock").exists()


def test_split_depth_round_trips_through_v1_generator_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4), complete(4)))
    run_directory = tmp_path / "run"
    config = UniversalCensusConfig(GengSpec(4, shard_index=1, shard_count=3, split_depth=2))
    expected = run_universal_census(config, run_directory, toolkit_identity=TEST_TOOLKIT)
    manifest = json.loads(expected.manifest_path.read_text(encoding="utf-8"))

    assert "split_depth" not in manifest["provenance"]["config"]["generator_spec"]
    assert manifest["provenance"]["generator"]["arguments"] == [
        "-q",
        "-X2",
        "4",
        "1/3",
    ]

    validated = validate_completed_universal_census(run_directory)

    assert validated.config == config
    assert validated.generator.arguments == ("-q", "-X2", "4", "1/3")
    assert validated.result == replace(expected, resumed_records=expected.record_count)


def test_split_depth_provenance_rejects_noncanonical_or_misplaced_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4, shard_index=1, shard_count=3, split_depth=2)),
        tmp_path,
        toolkit_identity=TEST_TOOLKIT,
    )
    pristine = json.loads(result.manifest_path.read_text(encoding="utf-8"))["provenance"]
    malformed_arguments = (
        ("-q", "-X02", "4", "1/3"),
        ("-q", "-X+2", "4", "1/3"),
        ("-q", "-X-2", "4", "1/3"),
        ("-q", "-X", "4", "1/3"),
        ("-q", "-X2", "-X2", "4", "1/3"),
        ("-q", "4", "-X2", "1/3"),
        ("-q", "-X2", "4", "2/3"),
        ("-q", "-X2", "4"),
    )

    for arguments in malformed_arguments:
        candidate = copy.deepcopy(pristine)
        candidate["generator"]["arguments"] = list(arguments)
        with pytest.raises(CensusFormatError, match="generator arguments do not match"):
            universal._parse_completed_provenance(candidate)


def test_public_completed_run_validation_rejects_provenance_and_generator_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    run_directory = tmp_path / "run"
    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        run_directory,
        toolkit_identity=TEST_TOOLKIT,
    )

    monkeypatch.setattr(
        universal,
        "geng_identity",
        lambda spec, *, executable="geng": GengIdentity("geng", "f" * 64, spec.arguments()),
    )
    with pytest.raises(CensusFormatError, match="local geng identity"):
        validate_completed_universal_census(run_directory)

    patch_generator(monkeypatch, (cycle(4),))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["provenance"]["config"]["fix_distinguished_colors"] = False
    result.manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    with pytest.raises(CensusFormatError, match="must fix distinguished"):
        validate_completed_universal_census(run_directory)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["provenance"].update(objective="wrong"), "objective"),
        (lambda value: value["provenance"].update(config=[]), "components"),
        (
            lambda value: value["provenance"]["config"].pop("checkpoint_interval"),
            "universal config",
        ),
        (
            lambda value: value["provenance"]["config"].update(partition_enumerator="wrong"),
            "partition enumerator",
        ),
        (
            lambda value: value["provenance"]["config"].update(generator_spec=[]),
            "components",
        ),
        (lambda value: value["provenance"]["config"].update(checks="bad"), "checks"),
        (
            lambda value: value["provenance"]["config"]["generator_spec"].pop("order"),
            "generator spec",
        ),
        (
            lambda value: value["provenance"]["config"]["filters"].update(extra=True),
            "filters",
        ),
        (
            lambda value: value["provenance"]["config"]["search_limits"].update(extra=None),
            "search limits",
        ),
        (
            lambda value: value["provenance"]["config"].update(checks=[1]),
            r"checks\[0\]",
        ),
        (
            lambda value: value["provenance"]["config"]["search_limits"].update(
                max_nodes_per_check=0
            ),
            "max_nodes_per_check",
        ),
        (
            lambda value: value["provenance"]["config"]["search_limits"].update(
                timeout_seconds_per_check="bad"
            ),
            "timeout_seconds",
        ),
        (
            lambda value: value["provenance"]["config"]["generator_spec"].update(connected=1),
            "connected",
        ),
        (lambda value: value["provenance"]["generator"].update(name="wrong"), "name"),
        (
            lambda value: value["provenance"]["generator"].update(arguments="bad"),
            "arguments",
        ),
        (
            lambda value: value["provenance"]["generator"].update(arguments=[1]),
            "arguments must be strings",
        ),
        (
            lambda value: value["provenance"]["generator"].update(executable="a/b"),
            "portable basename",
        ),
        (
            lambda value: value["provenance"]["generator"].update(arguments=["-q", "5"]),
            "do not match",
        ),
        (lambda value: value["provenance"]["shard"].update(count=2), "shard envelope"),
        (
            lambda value: value["provenance"]["toolkit"].pop("python_version"),
            "toolkit identity",
        ),
        (
            lambda value: value["provenance"]["toolkit"].update(source_sha256="bad"),
            "source_sha256",
        ),
    ],
)
def test_public_completed_run_validation_rejects_malformed_provenance_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    pristine = tmp_path / "pristine"
    run_universal_census(
        UniversalCensusConfig(GengSpec(4)), pristine, toolkit_identity=TEST_TOOLKIT
    )
    candidate = tmp_path / "candidate"
    shutil.copytree(pristine, candidate)
    manifest_path = candidate / "manifest.json"
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    mutation(manifest)
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    with pytest.raises(CensusFormatError, match=message):
        validate_completed_universal_census(candidate)


def test_generator_interruption_resumes_without_rechecking_completed_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphs = (cycle(4), complete(4))
    attempts = 0
    solve_calls = 0
    real_solve = solve_with_backend

    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def flaky_stream(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
        nonlocal attempts
        del spec, executable
        attempts += 1
        yield graphs[0]
        if attempts == 1:
            raise GengError("synthetic generator failure")
        yield graphs[1]

    def counted_solve(problem: object, *, backend: SolverBackend, limits: object) -> SolveResult:
        nonlocal solve_calls
        solve_calls += 1
        return real_solve(problem, backend=backend, limits=limits)  # type: ignore[arg-type]

    monkeypatch.setattr(universal, "geng_identity", fake_identity)
    monkeypatch.setattr(universal, "stream_geng", flaky_stream)
    monkeypatch.setattr(universal, "solve_with_backend", counted_solve)
    config = UniversalCensusConfig(GengSpec(4))

    with pytest.raises(GengError, match="synthetic"):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert not (tmp_path / "completion.json").exists()
    result = run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)

    assert result.resumed_records == 1
    assert result.record_count == 2
    assert solve_calls == 9  # C4: 2 partitions x 3 checks; K4: 1 x 3.


def test_torn_checkpoint_line_is_truncated_before_resume(tmp_path: Path) -> None:
    fingerprint = "c" * 64
    record = universal._process_graph(
        config=UniversalCensusConfig(GengSpec(4)),
        run_fingerprint=fingerprint,
        index=0,
        graph=cycle(4),
    )
    partial = tmp_path / ".records.jsonl.partial"
    complete_line = record.to_json().encode("utf-8") + b"\n"
    partial.write_bytes(complete_line + b'{"torn":')

    count, partition_count, counts = universal._scan_partial(
        partial,
        run_fingerprint=fingerprint,
        config=UniversalCensusConfig(GengSpec(4)),
    )

    assert count == 1
    assert partition_count == 2
    assert counts.verified_all == 1
    assert partial.read_bytes() == complete_line


def test_checkpoint_record_size_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(universal, "MAX_UNIVERSAL_RECORD_BYTES", 32)
    partial = tmp_path / ".records.jsonl.partial"
    partial.write_bytes(b"x" * 33)
    with pytest.raises(CensusFormatError, match="exceeds 32 bytes"):
        universal._scan_partial(
            partial,
            run_fingerprint="c" * 64,
            config=UniversalCensusConfig(GengSpec(4)),
        )


def test_resume_rejects_changed_check_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def interrupted(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
        del spec, executable
        yield cycle(4)
        raise GengError("stop after checkpoint")

    monkeypatch.setattr(universal, "geng_identity", fake_identity)
    monkeypatch.setattr(universal, "stream_geng", interrupted)
    with pytest.raises(GengError):
        run_universal_census(
            UniversalCensusConfig(GengSpec(4)), tmp_path, toolkit_identity=TEST_TOOLKIT
        )

    changed = UniversalCensusConfig(
        GengSpec(4), checks=(UniversalCheckSpec(SolverBackend.DSATUR, 1),)
    )
    with pytest.raises(CensusResumeError, match="different run configuration"):
        run_universal_census(changed, tmp_path, toolkit_identity=TEST_TOOLKIT)


def test_publication_is_atomic_completion_last_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    real_atomic_write = _atomic_write
    real_solve = solve_with_backend
    solve_calls = 0
    write_order: list[str] = []
    fail_manifest_once = True

    def counted_solve(problem: object, *, backend: SolverBackend, limits: object) -> SolveResult:
        nonlocal solve_calls
        solve_calls += 1
        return real_solve(problem, backend=backend, limits=limits)  # type: ignore[arg-type]

    def observed_atomic_write(path: Path, data: bytes) -> None:
        nonlocal fail_manifest_once
        write_order.append(path.name)
        assert (path.parent / "records.jsonl").is_file()
        if path.name == "completion.json":
            assert (path.parent / "manifest.json").is_file()
        if path.name == "manifest.json" and fail_manifest_once:
            fail_manifest_once = False
            raise OSError("synthetic publication interruption")
        real_atomic_write(path, data)

    monkeypatch.setattr(universal, "solve_with_backend", counted_solve)
    monkeypatch.setattr(universal, "_atomic_write", observed_atomic_write)
    config = UniversalCensusConfig(GengSpec(4))

    with pytest.raises(OSError, match="publication interruption"):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert (tmp_path / "records.jsonl").is_file()
    assert not (tmp_path / "completion.json").exists()

    result = run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert result.resumed_records == 1
    assert solve_calls == 6
    assert write_order == ["manifest.json", "manifest.json", "completion.json"]
    assert result.completion_path.is_file()


def test_eligible_structural_failure_aborts_without_false_ineligible_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = cycle(4)
    failing = complete(4)
    patch_generator(monkeypatch, (first, failing))
    real_iter = iter_equitable_partitions

    def fail_on_second(graph: SimpleGraph) -> Iterator[EquitablePartition]:
        if graph == failing:
            raise RuntimeError("synthetic partition enumerator failure")
        yield from real_iter(graph)

    monkeypatch.setattr(universal, "iter_equitable_partitions", fail_on_second)

    with pytest.raises(RuntimeError, match="enumerator failure"):
        run_universal_census(
            UniversalCensusConfig(GengSpec(4)), tmp_path, toolkit_identity=TEST_TOOLKIT
        )

    assert not (tmp_path / "completion.json").exists()
    records = read_records(tmp_path / ".records.jsonl.partial")
    assert len(records) == 1
    assert records[0].eligible is True


def test_resume_rejects_changed_generator_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def changed_stream(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
        nonlocal attempts
        del spec, executable
        attempts += 1
        yield cycle(4) if attempts == 1 else complete(4)
        if attempts == 1:
            raise GengError("stop")

    monkeypatch.setattr(universal, "geng_identity", fake_identity)
    monkeypatch.setattr(universal, "stream_geng", changed_stream)
    config = UniversalCensusConfig(GengSpec(4))
    with pytest.raises(GengError):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    with pytest.raises(CensusResumeError, match="does not match"):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)


def test_duplicate_generator_graph_aborts_without_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = cycle(4)
    patch_generator(monkeypatch, (graph, graph))
    with pytest.raises(CensusError, match="duplicate graph6"):
        run_universal_census(
            UniversalCensusConfig(GengSpec(4)), tmp_path, toolkit_identity=TEST_TOOLKIT
        )
    assert not (tmp_path / "completion.json").exists()
    assert len(read_records(tmp_path / ".records.jsonl.partial")) == 1


def test_completed_artifact_tampering_and_lock_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    config = UniversalCensusConfig(GengSpec(4))
    result = run_universal_census(config, tmp_path / "run", toolkit_identity=TEST_TOOLKIT)
    with result.records_path.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(CensusFormatError, match="digest"):
        run_universal_census(config, tmp_path / "run", toolkit_identity=TEST_TOOLKIT)

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / ".census.lock").write_text("pid=123\n", encoding="ascii")
    with pytest.raises(CensusLockError, match="confirming no writer"):
        run_universal_census(config, locked, toolkit_identity=TEST_TOOLKIT)


@pytest.mark.parametrize(
    "stream",
    [io.StringIO("not bytes\n"), _MalformedRecordStream(1), _MalformedRecordStream(None)],
)
def test_embedded_transcript_rejects_nonbinary_stream_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: object,
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        tmp_path / "binary-run",
        toolkit_identity=TEST_TOOLKIT,
    )
    with pytest.raises(CensusFormatError, match="exact bytes"):
        validate_completed_universal_transcript(
            result.manifest_path.read_bytes(),
            result.completion_path.read_bytes(),
            stream,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("target", "mutation", "message"),
    [
        ("manifest", lambda value: value.update(schema_version="bad"), "manifest version"),
        ("manifest", lambda value: value.update(complete=False), "completion state"),
        (
            "completion",
            lambda value: value.update(schema_version="bad"),
            "completion schema_version",
        ),
        ("manifest", lambda value: value.update(provenance=[]), "provenance must be an object"),
        (
            "manifest",
            lambda value: value.update(run_fingerprint="f" * 64),
            "run_fingerprint",
        ),
        ("manifest", lambda value: value.update(artifacts=[]), "artifacts and counts"),
        ("manifest", lambda value: value.update(counts=[]), "artifacts and counts"),
        (
            "manifest",
            lambda value: value["artifacts"].update(records_bytes=999),
            "artifact size or digest",
        ),
        ("manifest", lambda value: value.update(record_count=2), "manifest counts"),
        ("manifest", lambda value: value.update(partition_count=99), "manifest counts"),
        (
            "manifest",
            lambda value: value["counts"].update(verified_all=0, skipped=1),
            "manifest counts",
        ),
        (
            "completion",
            lambda value: value.update(records_sha256="f" * 64),
            "completion marker",
        ),
    ],
)
def test_embedded_transcript_rejects_each_envelope_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    mutation: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        tmp_path / "envelope-run",
        toolkit_identity=TEST_TOOLKIT,
    )
    manifest = cast(dict[str, Any], json.loads(result.manifest_path.read_text(encoding="utf-8")))
    completion = cast(
        dict[str, Any], json.loads(result.completion_path.read_text(encoding="utf-8"))
    )
    mutation(manifest if target == "manifest" else completion)

    with pytest.raises(CensusFormatError, match=message):
        validate_completed_universal_transcript(
            canonical_json_bytes(manifest) + b"\n",
            canonical_json_bytes(completion) + b"\n",
            io.BytesIO(result.records_path.read_bytes()),
        )


@pytest.mark.parametrize(
    ("records_factory", "message"),
    [
        (lambda _record: b"{\n", "invalid universal record"),
        (lambda _record: b"[]\n", "must be an object"),
        (lambda _record: b"{}\n", "order must equal"),
        (lambda record: canonical_json_bytes(record), "unterminated record"),
        (
            lambda record: json.dumps(record, sort_keys=True).encode("utf-8") + b"\n",
            "not canonical",
        ),
        (
            lambda record: canonical_json_bytes({**record, "index": 1}) + b"\n",
            "discontinuous or foreign",
        ),
    ],
)
def test_embedded_transcript_rejects_malformed_record_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records_factory: Callable[[dict[str, Any]], bytes],
    message: str,
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        tmp_path / "record-run",
        toolkit_identity=TEST_TOOLKIT,
    )
    record = cast(
        dict[str, Any],
        json.loads(result.records_path.read_text(encoding="utf-8").splitlines()[0]),
    )
    with pytest.raises(CensusFormatError, match=message):
        validate_completed_universal_transcript(
            result.manifest_path.read_bytes(),
            result.completion_path.read_bytes(),
            io.BytesIO(records_factory(record)),
        )


@pytest.mark.parametrize(
    ("regenerated", "message"),
    [
        ((), "ended before"),
        ((cast(SimpleGraph, object()),), "non-graph item"),
        ((complete(4),), "disagrees with completed record"),
        ((cycle(4), complete(4)), "extra graph"),
    ],
)
def test_embedded_transcript_rejects_generator_coverage_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    regenerated: tuple[SimpleGraph, ...],
    message: str,
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    result = run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        tmp_path / "generator-run",
        toolkit_identity=TEST_TOOLKIT,
    )
    with pytest.raises(CensusFormatError, match=message):
        universal._validate_universal_census_transcript(
            result.manifest_path.read_bytes(),
            result.completion_path.read_bytes(),
            io.BytesIO(result.records_path.read_bytes()),
            regenerated=iter(regenerated),
        )


def test_completed_validation_regenerates_stream_through_exact_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = cycle(4)
    patch_generator(monkeypatch, (first,))
    config = UniversalCensusConfig(GengSpec(4))
    run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)

    # Records, manifest, and completion remain mutually hash-consistent, but
    # the configured stream now has an additional graph. Identity is held
    # fixed deliberately so the independent coverage comparison must catch it.
    patch_generator(monkeypatch, (first, complete(4)))
    with pytest.raises(CensusFormatError, match="extra graph"):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)


def test_completed_validation_rejects_self_consistent_forged_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = cycle(4)
    patch_generator(monkeypatch, (graph,))
    config = UniversalCensusConfig(GengSpec(4))
    result = run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)

    record = json.loads(result.records_path.read_text(encoding="utf-8"))
    record.update(
        {
            "eligible": False,
            "status": "skipped",
            "outcome_code": "outside_high_degree_filter",
            "detail": "forged skip",
            "partition_count": 0,
            "partitions": [],
        }
    )
    records_bytes = canonical_json_bytes(record) + b"\n"
    result.records_path.write_bytes(records_bytes)
    records_sha256 = hashlib.sha256(records_bytes).hexdigest()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["records_bytes"] = len(records_bytes)
    manifest["artifacts"]["records_sha256"] = records_sha256
    manifest["counts"] = {
        "candidate_unsat": 0,
        "error": 0,
        "skipped": 1,
        "unknown": 0,
        "verified_all": 0,
    }
    manifest["partition_count"] = 0
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    result.manifest_path.write_bytes(manifest_bytes)

    completion = json.loads(result.completion_path.read_text(encoding="utf-8"))
    completion["records_sha256"] = records_sha256
    completion["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    result.completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")

    with pytest.raises(CensusFormatError, match="eligibility"):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)


def test_completed_validation_rejects_partial_coexistence_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (cycle(4),))
    config = UniversalCensusConfig(GengSpec(4))
    result = run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)

    partial = tmp_path / ".records.jsonl.partial"
    partial.write_bytes(b"")
    with pytest.raises(CensusFormatError, match="must not coexist"):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    partial.unlink()

    real_manifest = tmp_path / "manifest.real.json"
    result.manifest_path.rename(real_manifest)
    try:
        result.manifest_path.symlink_to(real_manifest.name)
    except OSError as exc:  # pragma: no cover - POSIX is the supported platform
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(CensusFormatError, match=r"non-symlink manifest\.json"):
        run_universal_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)


def test_independent_partition_dp_counts_and_catches_enumerator_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = cycle(4)
    assert count_equitable_partitions_dp(graph) == 2
    assert count_equitable_partitions_dp(complete(4)) == 1
    for edge_mask in range(1 << 6):
        edges = tuple(
            edge
            for bit, edge in enumerate(((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)))
            if edge_mask >> bit & 1
        )
        candidate = SimpleGraph.from_edges(4, edges)
        degree_parameter = candidate.max_degree + 1
        if degree_parameter <= candidate.order <= 2 * degree_parameter:
            assert count_equitable_partitions_dp(candidate) == sum(
                1 for _partition in iter_equitable_partitions(candidate)
            )

    first_only = (next(iter_equitable_partitions(graph)),)
    monkeypatch.setattr(universal, "iter_equitable_partitions", lambda _graph: iter(first_only))
    with pytest.raises(ValueError, match="independent complement-matching DP"):
        universal._process_graph(
            config=UniversalCensusConfig(GengSpec(4)),
            run_fingerprint="c" * 64,
            index=0,
            graph=graph,
        )

    duplicated = (first_only[0], first_only[0])
    monkeypatch.setattr(universal, "iter_equitable_partitions", lambda _graph: iter(duplicated))
    with pytest.raises(ValueError, match="must be unique"):
        universal._process_graph(
            config=UniversalCensusConfig(GengSpec(4)),
            run_fingerprint="c" * 64,
            index=0,
            graph=graph,
        )


def test_config_normalizes_checks_and_rejects_duplicates() -> None:
    config = UniversalCensusConfig(
        GengSpec(4),
        checks=(
            UniversalCheckSpec(SolverBackend.STATIC, 1),
            UniversalCheckSpec(SolverBackend.DSATUR, 1),
        ),
    )
    assert config.checks == (
        UniversalCheckSpec(SolverBackend.DSATUR, 1),
        UniversalCheckSpec(SolverBackend.STATIC, 1),
    )
    with pytest.raises(ValueError, match="unique"):
        UniversalCensusConfig(
            GengSpec(4),
            checks=(
                UniversalCheckSpec(SolverBackend.DSATUR, 1),
                UniversalCheckSpec(SolverBackend.DSATUR, 1),
            ),
        )


def test_public_value_objects_and_parsers_fail_closed_on_malformed_inputs(tmp_path: Path) -> None:
    graph = cycle(4)
    record = universal._process_graph(
        config=UniversalCensusConfig(GengSpec(4)),
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
    )
    partition = record.partitions[0]
    check = partition.checks[0]

    assert UniversalCheckSpec.from_mapping(check.spec.to_dict()) == check.spec
    assert check.spec.identifier.endswith(":D+1")
    with pytest.raises(CensusFormatError, match="unsupported solver"):
        UniversalCheckSpec.from_mapping({"backend_id": "unknown", "palette_offset": 1})
    with pytest.raises(ValueError, match="SolverBackend"):
        UniversalCheckSpec("dsatur", 1)  # type: ignore[arg-type]

    invalid_configs: tuple[Callable[[], object], ...] = (
        lambda: UniversalCensusConfig("bad"),  # type: ignore[arg-type]
        lambda: UniversalCensusConfig(GengSpec(4), checks=1),  # type: ignore[arg-type]
        lambda: UniversalCensusConfig(GengSpec(4), checks=()),
        lambda: UniversalCensusConfig(GengSpec(4), require_high_degree=1),  # type: ignore[arg-type]
        lambda: UniversalCensusConfig(GengSpec(4), limits_per_check="bad"),  # type: ignore[arg-type]
        lambda: UniversalCensusConfig(GengSpec(4), checkpoint_interval=0),
    )
    for make_invalid_config in invalid_configs:
        with pytest.raises(ValueError):
            make_invalid_config()

    assert UniversalCheckResult.from_mapping(check.to_dict()) == check
    invalid_checks: tuple[Callable[[], object], ...] = (
        lambda: replace(check, backend="bad"),  # type: ignore[arg-type]
        lambda: replace(check, status="bad"),  # type: ignore[arg-type]
        lambda: replace(check, stats="bad"),  # type: ignore[arg-type]
        lambda: replace(check, detail=""),
        lambda: replace(check, auxiliary_edge_colors=1),  # type: ignore[arg-type]
        lambda: replace(check, auxiliary_edge_colors=None),
    )
    for make_invalid_check in invalid_checks:
        with pytest.raises(ValueError):
            make_invalid_check()
    check_dict = cast(dict[str, Any], check.to_dict())
    bad_backend = copy.deepcopy(check_dict)
    bad_backend["backend_id"] = "bad"
    with pytest.raises(CensusFormatError, match="unsupported"):
        UniversalCheckResult.from_mapping(bad_backend)
    bad_stats = copy.deepcopy(check_dict)
    bad_stats["stats"] = "bad"
    with pytest.raises(CensusFormatError, match="stats"):
        UniversalCheckResult.from_mapping(bad_stats)
    bad_color = copy.deepcopy(check_dict)
    bad_color["color_count"] = -1
    with pytest.raises(CensusFormatError, match="invalid universal check"):
        UniversalCheckResult.from_mapping(bad_color)
    for field in ("palette_offset", "color_count"):
        for invalid_number in (True, 1.0):
            malformed_check = copy.deepcopy(check_dict)
            malformed_check[field] = invalid_number
            with pytest.raises(CensusFormatError, match="invalid universal check"):
                UniversalCheckResult.from_mapping(malformed_check)

    assert UniversalPartitionResult.from_mapping(partition.to_dict()) == partition
    invalid_partitions: tuple[Callable[[], object], ...] = (
        lambda: replace(partition, auxiliary_graph_fingerprint="d" * 64),
        lambda: replace(partition, checks=()),
        lambda: replace(partition, checks=(check, check)),
        lambda: replace(partition, pairs=((2, 0),)),
    )
    for make_invalid_partition in invalid_partitions:
        with pytest.raises((GraphFormatError, ValueError)):
            make_invalid_partition()
    partition_dict = cast(dict[str, Any], partition.to_dict())
    bad_container = copy.deepcopy(partition_dict)
    bad_container["auxiliary"] = "bad"
    with pytest.raises(CensusFormatError, match="must be objects"):
        UniversalPartitionResult.from_mapping(bad_container)
    bad_checks = copy.deepcopy(partition_dict)
    bad_checks["checks"] = [1]
    with pytest.raises(CensusFormatError, match="must be an object"):
        UniversalPartitionResult.from_mapping(bad_checks)

    invalid_records: tuple[Callable[[], object], ...] = (
        lambda: replace(record, eligible=1),  # type: ignore[arg-type]
        lambda: replace(record, status="bad"),  # type: ignore[arg-type]
        lambda: replace(record, outcome_code="Bad-Code"),
        lambda: replace(record, detail=""),
        lambda: replace(record, partition_count=0),
        lambda: replace(record, graph_fingerprint="d" * 64),
        lambda: replace(record, eligible=False),
        lambda: replace(record, status=UniversalCensusStatus.SKIPPED),
    )
    for make_invalid_record in invalid_records:
        with pytest.raises((GraphFormatError, ValueError)):
            make_invalid_record()
    record_dict = cast(dict[str, Any], record.to_dict())
    bad_record_partitions = copy.deepcopy(record_dict)
    bad_record_partitions["partitions"] = "bad"
    with pytest.raises(CensusFormatError, match="partitions must be an array"):
        UniversalCensusRecord.from_dict(bad_record_partitions)
    bad_record_status = copy.deepcopy(record_dict)
    bad_record_status["status"] = "bad"
    with pytest.raises(CensusFormatError, match="invalid universal census status"):
        UniversalCensusRecord.from_dict(bad_record_status)
    for field in ("index", "order"):
        for invalid_number in (True, 4.0):
            malformed_record = copy.deepcopy(record_dict)
            malformed_record[field] = invalid_number
            with pytest.raises(CensusFormatError, match="invalid universal census record"):
                UniversalCensusRecord.from_dict(malformed_record)
    wrong_color_relation = copy.deepcopy(record_dict)
    nested_check = wrong_color_relation["partitions"][0]["checks"][0]
    nested_check["color_count"] += 1
    with pytest.raises(CensusFormatError, match="color count does not equal D"):
        UniversalCensusRecord.from_dict(wrong_color_relation)
    with pytest.raises(CensusFormatError, match="JSON object"):
        UniversalCensusRecord.from_json("[]")
    with pytest.raises(CensusFormatError, match="invalid JSON"):
        UniversalCensusRecord.from_json("{")

    with pytest.raises(ValueError, match="nonnegative"):
        DeterministicSearchStats(-1, 0)
    assert DeterministicSearchStats.from_mapping({"nodes": 1, "backtracks": 2}).nodes == 1
    with pytest.raises(ValueError, match="nonnegative"):
        UniversalCensusCounts(error=-1)
    with pytest.raises(CensusFormatError, match="unknown keys"):
        UniversalCensusCounts.from_mapping(
            {
                "verified_all": 0,
                "candidate_unsat": 0,
                "unknown": 0,
                "error": 0,
                "skipped": 0,
                "extra": 0,
            }
        )
    with pytest.raises(ValueError, match="record_count"):
        UniversalCensusRunResult(
            "a" * 64,
            1,
            0,
            UniversalCensusCounts(),
            0,
            tmp_path / "records",
            tmp_path / "manifest",
            tmp_path / "completion",
        )


def test_process_graph_domains_and_malformed_backend_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = cycle(4)
    wrong_order = universal._process_graph(
        config=UniversalCensusConfig(GengSpec(5)),
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
    )
    outside_regime = universal._process_graph(
        config=UniversalCensusConfig(GengSpec(4), require_high_degree=False),
        run_fingerprint="c" * 64,
        index=1,
        graph=SimpleGraph.from_edges(4, ()),
    )
    assert wrong_order.status is UniversalCensusStatus.ERROR
    assert wrong_order.eligible is False
    assert outside_regime.status is UniversalCensusStatus.SKIPPED

    partition = next(iter_equitable_partitions(graph))
    construction = construct_auxiliary_graph(graph, partition)
    spec = UniversalCheckSpec(SolverBackend.DSATUR, 1)
    limits = SearchLimits()
    real_solve = solve_with_backend

    def run_with(fake_solver: Any) -> UniversalCheckResult:
        with monkeypatch.context() as context:
            context.setattr(universal, "solve_with_backend", fake_solver)
            return universal._solve_one_check(
                graph,
                partition,
                construction.graph,
                construction.distinguished_edges,
                spec,
                limits,
            )

    def raising_solver(*_args: object, **_kwargs: object) -> SolveResult:
        raise RuntimeError("backend crashed")

    raised = run_with(raising_solver)
    assert raised.status is SolveStatus.ERROR
    assert raised.stats == DeterministicSearchStats(0, 0)

    def foreign_digest(problem: Any, **kwargs: Any) -> SolveResult:
        return replace(real_solve(problem, **kwargs), problem_digest="d" * 64)

    def missing_assignment(problem: Any, **kwargs: Any) -> SolveResult:
        return replace(real_solve(problem, **kwargs), assignment=None)

    def invalid_assignment(problem: Any, **kwargs: Any) -> SolveResult:
        solved = real_solve(problem, **kwargs)
        assert solved.assignment is not None
        return replace(solved, assignment=(0,) * len(solved.assignment))

    for fake_solver in (foreign_digest, missing_assignment, invalid_assignment):
        assert run_with(fake_solver).status is SolveStatus.ERROR

    assert run_with(lambda *_args, **_kwargs: object()).status is SolveStatus.ERROR

    def invalid_stats(problem: Any, **kwargs: Any) -> SolveResult:
        return replace(real_solve(problem, **kwargs), stats=object())  # type: ignore[arg-type]

    malformed_stats = run_with(invalid_stats)
    assert malformed_stats.status is SolveStatus.ERROR
    assert malformed_stats.stats == DeterministicSearchStats(0, 0)

    with pytest.raises(CensusError, match="construction changed"):
        universal._solve_one_check(
            graph,
            partition,
            SimpleGraph.from_edges(1, ()),
            construction.distinguished_edges,
            spec,
            limits,
        )
