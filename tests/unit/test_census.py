from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import total_coloring.census as census
from total_coloring.auxiliary import AuxiliarySearchResult, search_auxiliary_extensions
from total_coloring.census import (
    CensusConfig,
    CensusCounts,
    CensusFormatError,
    CensusLockError,
    CensusRecord,
    CensusResumeError,
    CensusStatus,
    ToolkitIdentity,
    run_census,
)
from total_coloring.certificates import TotalColoringCertificate, verify_total_coloring
from total_coloring.geng import GengError, GengIdentity, GengSpec
from total_coloring.graph import SimpleGraph, canonical_json_bytes
from total_coloring.solver import SearchLimits, SolveStatus


def complete_graph(order: int) -> SimpleGraph:
    return SimpleGraph.from_edges(
        order,
        ((left, right) for left in range(order) for right in range(left + 1, order)),
    )


def cycle_graph(order: int) -> SimpleGraph:
    edges = ((vertex, (vertex + 1) % order) for vertex in range(order))
    return SimpleGraph.from_edges(order, edges)


TEST_TOOLKIT = ToolkitIdentity(
    distribution_version="test",
    source_sha256="b" * 64,
    python_implementation="CPython",
    python_version="3.13.0",
)


@pytest.fixture(autouse=True)
def isolate_census_tests_from_installed_geng(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic census tests independent of workstation executables."""

    def fake_resolve(executable: str = "geng") -> Path:
        return Path("/synthetic") / Path(executable).name

    monkeypatch.setattr(census, "resolve_geng", fake_resolve)


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

    monkeypatch.setattr(census, "geng_identity", fake_identity)
    monkeypatch.setattr(census, "stream_geng", fake_stream)


def synthetic_search(
    graph: SimpleGraph,
    color_count: int,
    *,
    limits_per_partition: SearchLimits | None = None,
    max_partitions: int | None = None,
) -> AuxiliarySearchResult:
    del limits_per_partition, max_partitions
    return AuxiliarySearchResult(
        status=SolveStatus.CANDIDATE_UNSAT,
        graph_fingerprint=graph.fingerprint,
        color_count=color_count,
        partitions_started=2,
        partitions_completed=2,
        candidate_failures=2,
        unknown_partitions=0,
        witness=None,
        detail="synthetic exhaustive candidate result",
    )


def test_config_rejects_ambiguous_and_nonfinite_values() -> None:
    spec = GengSpec(4)
    with pytest.raises(ValueError, match="color_offset"):
        CensusConfig(spec, color_offset_from_degree_parameter=True)
    with pytest.raises(ValueError, match="require_high_degree"):
        CensusConfig(spec, require_high_degree=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        CensusConfig(spec, limits_per_partition=SearchLimits(timeout_seconds=float("nan")))
    with pytest.raises(ValueError, match="max_partitions"):
        CensusConfig(spec, max_partitions=0)
    with pytest.raises(ValueError, match="checkpoint_interval"):
        CensusConfig(spec, checkpoint_interval=0)


def test_every_yielded_graph_receives_exactly_one_explicit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    k4 = complete_graph(4)
    c4 = cycle_graph(4)
    diamond = SimpleGraph.from_edges(4, [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3)])
    star = SimpleGraph.from_edges(4, [(0, 1), (0, 2), (0, 3)])
    triangle = SimpleGraph.from_edges(4, [(0, 1), (0, 2), (1, 2)])
    empty = SimpleGraph.from_edges(4, [])
    graphs = (k4, c4, diamond, star, triangle, empty)
    patch_generator(monkeypatch, graphs)
    witness = search_auxiliary_extensions(k4, 6)
    assert witness.status is SolveStatus.WITNESS

    def fake_search(
        graph: SimpleGraph,
        color_count: int,
        *,
        limits_per_partition: SearchLimits | None = None,
        max_partitions: int | None = None,
    ) -> AuxiliarySearchResult:
        del limits_per_partition, max_partitions
        if graph == k4:
            return witness
        if graph == c4:
            return synthetic_search(graph, color_count)
        if graph == diamond:
            return AuxiliarySearchResult(
                SolveStatus.UNKNOWN,
                graph.fingerprint,
                color_count,
                2,
                1,
                1,
                1,
                None,
                "synthetic node limit",
            )
        if graph == star:
            return AuxiliarySearchResult(
                SolveStatus.ERROR,
                graph.fingerprint,
                color_count,
                0,
                0,
                0,
                0,
                None,
                "synthetic backend error",
            )
        if graph == triangle:
            raise RuntimeError("synthetic exception")
        raise AssertionError("the empty graph must be filtered before search")

    monkeypatch.setattr(census, "search_auxiliary_extensions", fake_search)
    result = run_census(
        CensusConfig(GengSpec(4)),
        tmp_path,
        toolkit_identity=TEST_TOOLKIT,
    )

    assert result.record_count == len(graphs)
    assert result.counts.to_dict() == {
        "candidate_unsat": 1,
        "error": 2,
        "skipped": 1,
        "unknown": 1,
        "witness": 1,
    }
    records = [
        CensusRecord.from_json(line) for line in result.records_path.read_bytes().splitlines()
    ]
    assert [record.index for record in records] == list(range(len(graphs)))
    assert [record.status for record in records] == [
        CensusStatus.WITNESS,
        CensusStatus.CANDIDATE_UNSAT,
        CensusStatus.UNKNOWN,
        CensusStatus.ERROR,
        CensusStatus.ERROR,
        CensusStatus.SKIPPED,
    ]
    assert records[0].certificate is not None
    assert records[4].outcome_code == "search_exception"
    assert records[5].outcome_code == "outside_high_degree_filter"
    assert result.completion_path.is_file()


def test_same_inputs_produce_byte_identical_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphs = (complete_graph(4), cycle_graph(4))
    patch_generator(monkeypatch, graphs)
    monkeypatch.setattr(census, "search_auxiliary_extensions", synthetic_search)
    config = CensusConfig(GengSpec(4, shard_index=1, shard_count=3), checkpoint_interval=2)

    first = run_census(config, tmp_path / "first", toolkit_identity=TEST_TOOLKIT)
    second = run_census(config, tmp_path / "second", toolkit_identity=TEST_TOOLKIT)

    assert first.run_fingerprint == second.run_fingerprint
    assert first.records_path.read_bytes() == second.records_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.completion_path.read_bytes() == second.completion_path.read_bytes()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["provenance"]["shard"] == {"count": 3, "index": 1}
    assert manifest["provenance"]["generator"]["sha256"] == "a" * 64
    assert manifest["provenance"]["generator"]["executable"] == "geng"
    assert manifest["provenance"]["toolkit"] == TEST_TOOLKIT.to_dict()


def test_generator_failure_leaves_resumable_prefix_without_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphs = (complete_graph(4), cycle_graph(4))
    attempts = 0
    searched: list[str] = []

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

    def tracked_search(
        graph: SimpleGraph,
        color_count: int,
        *,
        limits_per_partition: SearchLimits | None = None,
        max_partitions: int | None = None,
    ) -> AuxiliarySearchResult:
        searched.append(graph.fingerprint)
        return synthetic_search(
            graph,
            color_count,
            limits_per_partition=limits_per_partition,
            max_partitions=max_partitions,
        )

    monkeypatch.setattr(census, "geng_identity", fake_identity)
    monkeypatch.setattr(census, "stream_geng", flaky_stream)
    monkeypatch.setattr(census, "search_auxiliary_extensions", tracked_search)
    config = CensusConfig(GengSpec(4))

    with pytest.raises(GengError, match="synthetic"):
        run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert (tmp_path / ".records.jsonl.partial").read_bytes().count(b"\n") == 1
    assert not (tmp_path / "completion.json").exists()
    assert not (tmp_path / "records.jsonl").exists()

    result = run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert result.resumed_records == 1
    assert result.record_count == 2
    assert searched == [graphs[0].fingerprint, graphs[1].fingerprint]


def test_resume_rejects_a_different_regenerated_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = complete_graph(4)
    replacement = cycle_graph(4)
    attempts = 0

    def fake_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def changed_stream(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
        nonlocal attempts
        del spec, executable
        attempts += 1
        yield first if attempts == 1 else replacement
        if attempts == 1:
            raise GengError("stop after checkpoint")

    monkeypatch.setattr(census, "geng_identity", fake_identity)
    monkeypatch.setattr(census, "stream_geng", changed_stream)
    monkeypatch.setattr(census, "search_auxiliary_extensions", synthetic_search)
    config = CensusConfig(GengSpec(4))
    with pytest.raises(GengError):
        run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    with pytest.raises(CensusResumeError, match="does not match"):
        run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert not (tmp_path / "completion.json").exists()


def test_publication_interruption_is_recovered_without_research_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = complete_graph(4)
    patch_generator(monkeypatch, (graph,))
    searches = 0

    def counted_search(
        graph: SimpleGraph,
        color_count: int,
        *,
        limits_per_partition: SearchLimits | None = None,
        max_partitions: int | None = None,
    ) -> AuxiliarySearchResult:
        nonlocal searches
        searches += 1
        return synthetic_search(
            graph,
            color_count,
            limits_per_partition=limits_per_partition,
            max_partitions=max_partitions,
        )

    monkeypatch.setattr(census, "search_auxiliary_extensions", counted_search)
    real_atomic_write = census._atomic_write
    failed = False

    def fail_once(path: Path, data: bytes) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("synthetic publication interruption")
        real_atomic_write(path, data)

    monkeypatch.setattr(census, "_atomic_write", fail_once)
    config = CensusConfig(GengSpec(4))
    with pytest.raises(OSError, match="publication interruption"):
        run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert (tmp_path / "records.jsonl").is_file()
    assert not (tmp_path / "completion.json").exists()

    monkeypatch.setattr(census, "_atomic_write", real_atomic_write)
    result = run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert result.resumed_records == 1
    assert searches == 1
    assert result.completion_path.is_file()


def test_completed_run_is_revalidated_and_tampering_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (complete_graph(4),))
    monkeypatch.setattr(census, "search_auxiliary_extensions", synthetic_search)
    config = CensusConfig(GengSpec(4))
    first = run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    second = run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)
    assert second.resumed_records == second.record_count == 1

    with first.records_path.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(CensusFormatError, match="digest"):
        run_census(config, tmp_path, toolkit_identity=TEST_TOOLKIT)


def test_lock_prevents_concurrent_writers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_generator(monkeypatch, ())
    (tmp_path / ".census.lock").write_text("pid=123\n", encoding="ascii")
    with pytest.raises(CensusLockError, match="confirming no writer"):
        run_census(CensusConfig(GengSpec(4)), tmp_path, toolkit_identity=TEST_TOOLKIT)


def test_record_parser_rejects_duplicate_keys_and_canonicalizes_spacing() -> None:
    graph = complete_graph(3)
    fingerprint = "c" * 64
    record = census._record_without_search(
        run_fingerprint=fingerprint,
        index=0,
        graph=graph,
        color_count=5,
        status=CensusStatus.CANDIDATE_UNSAT,
        outcome_code="test_result",
        detail="test",
    )
    with pytest.raises(CensusFormatError, match="duplicate"):
        CensusRecord.from_json('{"schema_version":1,"schema_version":2}')

    noncanonical = json.dumps(record.to_dict(), indent=2)
    parsed = CensusRecord.from_json(noncanonical)
    assert parsed == record
    assert parsed.to_json() != noncanonical


def test_toolkit_config_count_and_run_result_validation_guards(tmp_path: Path) -> None:
    detected = census.detect_toolkit_identity()
    assert len(detected.source_sha256) == 64
    assert detected.python_implementation

    with pytest.raises(ValueError, match="distribution_version"):
        ToolkitIdentity("", "a" * 64, "CPython", "3.13")
    with pytest.raises(ValueError, match="source_sha256"):
        ToolkitIdentity("test", "bad", "CPython", "3.13")
    with pytest.raises(ValueError, match="python_implementation"):
        ToolkitIdentity("test", "a" * 64, "", "3.13")
    with pytest.raises(ValueError, match="python_version"):
        ToolkitIdentity("test", "a" * 64, "CPython", "")
    with pytest.raises(ValueError, match="GengSpec"):
        CensusConfig("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SearchLimits"):
        CensusConfig(GengSpec(4), limits_per_partition="bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonnegative"):
        CensusCounts(witness=-1)
    with pytest.raises(CensusFormatError, match="unknown keys"):
        CensusCounts.from_mapping(
            {
                "witness": 0,
                "candidate_unsat": 0,
                "unknown": 0,
                "error": 0,
                "skipped": 0,
                "extra": 0,
            }
        )
    with pytest.raises(ValueError, match="record_count"):
        census.CensusRunResult(
            "a" * 64,
            1,
            CensusCounts(),
            0,
            tmp_path / "records",
            tmp_path / "manifest",
            tmp_path / "completion",
        )
    with pytest.raises(ValueError, match="resumed_records"):
        census.CensusRunResult(
            "a" * 64,
            0,
            CensusCounts(),
            1,
            tmp_path / "records",
            tmp_path / "manifest",
            tmp_path / "completion",
        )


def test_record_semantic_validation_guards() -> None:
    graph = complete_graph(3)
    base = census._record_without_search(
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
        color_count=5,
        status=CensusStatus.CANDIDATE_UNSAT,
        outcome_code="test_result",
        detail="test",
    )

    def invalid_status() -> CensusRecord:
        return replace(base, status="bad")  # type: ignore[arg-type]

    invalid_records = (
        invalid_status,
        lambda: replace(base, outcome_code="Bad-Code"),
        lambda: replace(base, detail=""),
        lambda: replace(base, degree_parameter=99),
        lambda: replace(base, partitions_started=0, partitions_completed=1),
        lambda: replace(base, candidate_failures=1),
        lambda: replace(base, unknown_partitions=1),
        lambda: replace(base, status=CensusStatus.WITNESS),
        lambda: replace(base, status=CensusStatus.SKIPPED, partitions_started=1),
        lambda: replace(base, graph6=">>graph6<<" + base.graph6),
        lambda: replace(base, size=99),
    )
    for make_invalid in invalid_records:
        with pytest.raises(ValueError):
            make_invalid()

    witness_result = search_auxiliary_extensions(graph, 5)
    witness = census._result_record(
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
        expected_color_count=5,
        result=witness_result,
    )
    assert witness.status is CensusStatus.WITNESS
    with pytest.raises(ValueError, match="palette"):
        replace(witness, color_count=6)
    other = SimpleGraph.from_edges(2, [(0, 1)])
    foreign_certificate = TotalColoringCertificate.create(other, 5, [0, 1], [2])
    with pytest.raises(ValueError, match="invalid"):
        replace(witness, certificate=foreign_certificate)


def test_record_parser_rejects_malformed_documents() -> None:
    graph = complete_graph(3)
    record = census._record_without_search(
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
        color_count=5,
        status=CensusStatus.CANDIDATE_UNSAT,
        outcome_code="test_result",
        detail="test",
    )
    baseline = record.to_dict()

    mutations: list[tuple[str, object]] = [
        ("schema_version", "bad"),
        ("status", "proved_unsat"),
        ("certificate", "bad"),
        ("index", -1),
    ]
    for key, value in mutations:
        document = dict(baseline)
        document[key] = value
        with pytest.raises(CensusFormatError):
            CensusRecord.from_dict(document)

    bad_certificate = dict(baseline)
    bad_certificate["certificate"] = {"schema_version": "bad"}
    with pytest.raises(CensusFormatError, match="embedded certificate"):
        CensusRecord.from_dict(bad_certificate)
    extra = dict(baseline)
    extra["extra"] = True
    with pytest.raises(CensusFormatError, match="unknown keys"):
        CensusRecord.from_dict(extra)
    with pytest.raises(CensusFormatError, match="JSON object"):
        CensusRecord.from_json("[]")
    with pytest.raises(CensusFormatError, match="invalid JSON"):
        CensusRecord.from_json("{")


def test_result_binding_failures_become_explicit_error_records() -> None:
    graph = complete_graph(3)
    valid = synthetic_search(graph, 5)
    misleading = replace(valid, detail="proved unextendable")
    wrong_color = replace(valid, color_count=6)
    wrong_graph = replace(valid, graph_fingerprint="d" * 64)
    missing_witness = replace(valid, status=SolveStatus.WITNESS)

    candidate = census._result_record(
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
        expected_color_count=5,
        result=misleading,
    )
    assert candidate.status is CensusStatus.CANDIDATE_UNSAT
    assert "proved unextendable" not in candidate.detail.lower()
    assert "no independently checked unsat proof" in candidate.detail.lower()

    assert (
        census._result_record(
            run_fingerprint="c" * 64,
            index=0,
            graph=graph,
            expected_color_count=5,
            result=wrong_color,
        ).outcome_code
        == "result_color_count_mismatch"
    )
    assert (
        census._result_record(
            run_fingerprint="c" * 64,
            index=0,
            graph=graph,
            expected_color_count=5,
            result=wrong_graph,
        ).outcome_code
        == "result_graph_mismatch"
    )
    assert (
        census._result_record(
            run_fingerprint="c" * 64,
            index=0,
            graph=graph,
            expected_color_count=5,
            result=missing_witness,
        ).outcome_code
        == "invalid_search_result"
    )

    invalid_status = replace(valid, status="bad")  # type: ignore[arg-type]
    assert (
        census._result_record(
            run_fingerprint="c" * 64,
            index=0,
            graph=graph,
            expected_color_count=5,
            result=invalid_status,
        ).outcome_code
        == "invalid_search_status"
    )


def test_non_equitable_total_coloring_cannot_masquerade_as_auxiliary_witness() -> None:
    graph = cycle_graph(4)
    result = search_auxiliary_extensions(graph, 5)
    record = census._result_record(
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
        expected_color_count=5,
        result=result,
    )
    assert record.status is CensusStatus.WITNESS
    assert record.auxiliary_witness is not None

    non_equitable = TotalColoringCertificate.create(
        graph,
        5,
        vertex_colors=(0, 1, 2, 3),
        edge_colors=(2, 1, 3, 0),
    )
    assert verify_total_coloring(graph, non_equitable).valid
    with pytest.raises(ValueError, match="decoded auxiliary coloring"):
        replace(record, certificate=non_equitable)

    invalid_auxiliary = replace(
        record.auxiliary_witness,
        auxiliary_edge_colors=(0,) * len(record.auxiliary_witness.auxiliary_edge_colors),
    )
    with pytest.raises(ValueError, match="invalid edge coloring"):
        replace(record, auxiliary_witness=invalid_auxiliary)


def test_auxiliary_witness_roundtrip_and_reconstruction_guards() -> None:
    graph = cycle_graph(4)
    result = search_auxiliary_extensions(graph, 5)
    record = census._result_record(
        run_fingerprint="c" * 64,
        index=0,
        graph=graph,
        expected_color_count=5,
        result=result,
    )
    witness = record.auxiliary_witness
    assert witness is not None
    assert census.CensusAuxiliaryWitness.from_dict(witness.to_dict()) == witness

    bad_version = witness.to_dict()
    bad_version["schema_version"] = "bad"
    with pytest.raises(CensusFormatError, match="schema_version"):
        census.CensusAuxiliaryWitness.from_dict(bad_version)
    bad_partition = witness.to_dict()
    bad_partition["partition"] = []
    with pytest.raises(CensusFormatError, match="partition must be an object"):
        census.CensusAuxiliaryWitness.from_dict(bad_partition)
    duplicate_singletons = witness.to_dict()
    partition_value = duplicate_singletons["partition"]
    assert isinstance(partition_value, dict)
    partition_value["singletons"] = [0, 0]
    with pytest.raises(CensusFormatError, match="strictly increasing"):
        census.CensusAuxiliaryWitness.from_dict(duplicate_singletons)
    malformed_edge = witness.to_dict()
    malformed_edge["distinguished_edges"] = [[0]]
    with pytest.raises(CensusFormatError, match="exactly two"):
        census.CensusAuxiliaryWitness.from_dict(malformed_edge)

    wrong_singletons = replace(witness, partition_singletons=tuple(range(graph.order)))
    with pytest.raises(ValueError, match="singleton classes"):
        replace(record, auxiliary_witness=wrong_singletons)
    wrong_graph = replace(witness, auxiliary_graph6=graph.to_graph6())
    with pytest.raises(ValueError, match="auxiliary graph"):
        replace(record, auxiliary_witness=wrong_graph)
    wrong_distinguished = replace(witness, distinguished_edges=())
    with pytest.raises(ValueError, match="distinguished edges"):
        replace(record, auxiliary_witness=wrong_distinguished)


def test_checkpoint_validation_and_recovery_guards(tmp_path: Path) -> None:
    fingerprint = "c" * 64
    graph = complete_graph(3)
    record = census._record_without_search(
        run_fingerprint=fingerprint,
        index=0,
        graph=graph,
        color_count=5,
        status=CensusStatus.CANDIDATE_UNSAT,
        outcome_code="test_result",
        detail="test",
    )

    symlink = tmp_path / "symlink"
    symlink.symlink_to(tmp_path / "missing")
    with pytest.raises(CensusFormatError, match="symbolic"):
        census._scan_partial(symlink, run_fingerprint=fingerprint)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(CensusFormatError, match="regular"):
        census._scan_partial(directory, run_fingerprint=fingerprint)

    trailing = tmp_path / "trailing.jsonl"
    trailing.write_bytes(canonical_json_bytes(record.to_dict()) + b"\npartial")
    count, counts = census._scan_partial(trailing, run_fingerprint=fingerprint)
    assert count == counts.total == 1
    assert trailing.read_bytes().endswith(b"\n")

    noncanonical = tmp_path / "noncanonical.jsonl"
    noncanonical.write_text(json.dumps(record.to_dict()) + "\n", encoding="utf-8")
    with pytest.raises(CensusFormatError, match="canonical"):
        census._scan_partial(noncanonical, run_fingerprint=fingerprint)
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_bytes(canonical_json_bytes(record.to_dict()) + b"\n")
    with pytest.raises(CensusResumeError, match="different run"):
        census._scan_partial(foreign, run_fingerprint="d" * 64)
    discontinuous = tmp_path / "discontinuous.jsonl"
    discontinuous_record = replace(record, index=1)
    discontinuous.write_bytes(canonical_json_bytes(discontinuous_record.to_dict()) + b"\n")
    with pytest.raises(CensusResumeError, match="discontinuity"):
        census._scan_partial(discontinuous, run_fingerprint=fingerprint)
    empty = tmp_path / "empty.jsonl"
    empty.touch()
    with pytest.raises(CensusResumeError, match="disappeared"):
        next(census._iter_checkpoint_records(empty, 1))


def test_interrupted_publication_recovery_rejects_ambiguous_paths(tmp_path: Path) -> None:
    (tmp_path / "completion.json").touch()
    census._recover_interrupted_publication(tmp_path)
    (tmp_path / "completion.json").unlink()

    records = tmp_path / "records.jsonl"
    records.mkdir()
    with pytest.raises(CensusFormatError, match="regular"):
        census._recover_interrupted_publication(tmp_path)
    records.rmdir()
    records.touch()
    (tmp_path / ".records.jsonl.partial").touch()
    with pytest.raises(CensusFormatError, match="both completed and partial"):
        census._recover_interrupted_publication(tmp_path)


def test_low_level_identity_and_document_guards(tmp_path: Path) -> None:
    spec = GengSpec(4)
    with pytest.raises(ValueError, match="executable"):
        census._generator_dict(GengIdentity("", "a" * 64, spec.arguments()))
    for unsafe_name in ("/private/cluster/bin/geng", r"C:\\private\\geng.exe", "..", "geng\n"):
        with pytest.raises(ValueError, match="portable basename"):
            census._generator_dict(GengIdentity(unsafe_name, "a" * 64, ()))
    with pytest.raises(ValueError, match="arguments"):
        census._generator_dict(
            GengIdentity("geng", "a" * 64, ("ok", 1))  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="do not match"):
        census._build_run_identity(
            CensusConfig(spec),
            GengIdentity("geng", "a" * 64, ("wrong",)),
            TEST_TOOLKIT,
        )

    no_newline = tmp_path / "no-newline.json"
    no_newline.write_text("{}", encoding="utf-8")
    with pytest.raises(CensusFormatError, match="end with one LF"):
        census._load_canonical_json(no_newline)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{\n", encoding="utf-8")
    with pytest.raises(CensusFormatError, match="invalid"):
        census._load_canonical_json(invalid)
    array = tmp_path / "array.json"
    array.write_text("[]\n", encoding="utf-8")
    with pytest.raises(CensusFormatError, match="JSON object"):
        census._load_canonical_json(array)
    spaced = tmp_path / "spaced.json"
    spaced.write_text('{"a": 1}\n', encoding="utf-8")
    with pytest.raises(CensusFormatError, match="canonical"):
        census._load_canonical_json(spaced)


def test_canonical_metadata_loader_enforces_size_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(census, "MAX_CENSUS_METADATA_BYTES", 4)
    oversized = tmp_path / "manifest.json"
    oversized.write_bytes(b"{}\n  ")
    with pytest.raises(CensusFormatError, match="metadata bytes"):
        census._load_canonical_json(oversized)


def test_graph_processing_defensive_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_order = census._process_graph(
        config=CensusConfig(GengSpec(4)),
        run_fingerprint="c" * 64,
        index=0,
        graph=complete_graph(3),
    )
    assert wrong_order.outcome_code == "generator_order_mismatch"
    null_record = census._process_graph(
        config=CensusConfig(GengSpec(0), require_high_degree=False),
        run_fingerprint="c" * 64,
        index=0,
        graph=SimpleGraph.from_edges(0, []),
    )
    assert null_record.outcome_code == "outside_auxiliary_regime"

    def explode_result(**kwargs: object) -> CensusRecord:
        del kwargs
        raise RuntimeError("malformed result")

    monkeypatch.setattr(census, "_result_record", explode_result)
    malformed = census._process_graph(
        config=CensusConfig(GengSpec(3)),
        run_fingerprint="c" * 64,
        index=0,
        graph=complete_graph(3),
    )
    assert malformed.outcome_code == "result_processing_exception"


def test_run_rejects_invalid_inputs_and_midrun_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="CensusConfig"):
        run_census("bad", tmp_path)  # type: ignore[arg-type]

    spec = GengSpec(4)

    def fake_stream(spec: GengSpec, *, executable: str = "geng") -> tuple[object, ...]:
        del spec, executable
        return ("not-a-graph",)

    def stable_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    monkeypatch.setattr(census, "geng_identity", stable_identity)
    monkeypatch.setattr(census, "stream_geng", fake_stream)
    with pytest.raises(census.CensusError, match="not a SimpleGraph"):
        run_census(CensusConfig(spec), tmp_path / "bad-item", toolkit_identity=TEST_TOOLKIT)

    monkeypatch.setattr(census, "stream_geng", lambda *args, **kwargs: ())
    identity_calls = 0

    def changing_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        nonlocal identity_calls
        del executable
        identity_calls += 1
        digest = "a" * 64 if identity_calls == 1 else "d" * 64
        return GengIdentity("geng", digest, spec.arguments())

    monkeypatch.setattr(census, "geng_identity", changing_identity)
    with pytest.raises(census.CensusError, match="geng executable identity changed"):
        run_census(CensusConfig(spec), tmp_path / "changed-geng", toolkit_identity=TEST_TOOLKIT)

    monkeypatch.setattr(census, "geng_identity", stable_identity)
    toolkits = iter(
        (
            TEST_TOOLKIT,
            ToolkitIdentity("test", "e" * 64, "CPython", "3.13.0"),
        )
    )
    monkeypatch.setattr(census, "detect_toolkit_identity", lambda: next(toolkits))
    with pytest.raises(census.CensusError, match="toolkit source identity changed"):
        run_census(CensusConfig(spec), tmp_path / "changed-toolkit")


def write_canonical_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def test_completed_manifest_validation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch, (complete_graph(4),))
    monkeypatch.setattr(census, "search_auxiliary_extensions", synthetic_search)
    config = CensusConfig(GengSpec(4))

    missing = run_census(config, tmp_path / "missing", toolkit_identity=TEST_TOOLKIT)
    missing.manifest_path.unlink()
    with pytest.raises(CensusFormatError, match="missing manifest"):
        run_census(config, tmp_path / "missing", toolkit_identity=TEST_TOOLKIT)

    cases: tuple[tuple[str, str, str, object, str], ...] = (
        ("manifest-state", "manifest", "complete", False, "version or completion"),
        ("completion-version", "completion", "schema_version", "bad", "schema_version"),
        ("run-fingerprint", "manifest", "run_fingerprint", "d" * 64, "different run"),
        ("provenance", "manifest", "provenance", {}, "provenance"),
        ("artifacts", "manifest", "artifacts", [], "artifacts and counts"),
        ("completion", "completion", "record_count", 999, "completion marker"),
    )
    for directory_name, document_name, key, value, message in cases:
        result = run_census(
            config,
            tmp_path / directory_name,
            toolkit_identity=TEST_TOOLKIT,
        )
        target = result.manifest_path if document_name == "manifest" else result.completion_path
        document = json.loads(target.read_text(encoding="utf-8"))
        document[key] = value
        write_canonical_json(target, document)
        expected_error = (
            CensusResumeError if directory_name == "run-fingerprint" else CensusFormatError
        )
        with pytest.raises(expected_error, match=message):
            run_census(config, tmp_path / directory_name, toolkit_identity=TEST_TOOLKIT)

    counts_result = run_census(config, tmp_path / "counts", toolkit_identity=TEST_TOOLKIT)
    manifest = json.loads(counts_result.manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["candidate_unsat"] = 2
    write_canonical_json(counts_result.manifest_path, manifest)
    completion = json.loads(counts_result.completion_path.read_text(encoding="utf-8"))
    completion["manifest_sha256"] = hashlib.sha256(
        counts_result.manifest_path.read_bytes()
    ).hexdigest()
    write_canonical_json(counts_result.completion_path, completion)
    with pytest.raises(CensusFormatError, match="counts"):
        run_census(config, tmp_path / "counts", toolkit_identity=TEST_TOOLKIT)
