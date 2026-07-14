from __future__ import annotations

import json
from pathlib import Path

import pytest

from total_coloring.census import CensusConfig, CensusCounts, CensusRunResult
from total_coloring.certificates import TotalColoringCertificate
from total_coloring.cli import EXIT_ERROR, EXIT_NO_WITNESS, EXIT_SUCCESS, EXIT_UNKNOWN, main
from total_coloring.graph import SimpleGraph


def write_graph(path: Path, graph: SimpleGraph) -> None:
    path.write_text(graph.to_json() + "\n", encoding="utf-8")


def test_solve_then_verify_round_trip(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(3, [(0, 1), (0, 2), (1, 2)])
    graph_path = tmp_path / "triangle.json"
    certificate_path = tmp_path / "triangle-certificate.json"
    write_graph(graph_path, graph)

    exit_code = main(
        [
            "solve",
            "--graph",
            str(graph_path),
            "--colors",
            "3",
            "--certificate-out",
            str(certificate_path),
        ]
    )

    assert exit_code == EXIT_SUCCESS
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    solved_payload = json.loads(captured.out)
    assert solved_payload["status"] == "witness"
    assert certificate_path.is_file()

    exit_code = main(
        [
            "verify",
            "--graph",
            str(graph_path),
            "--certificate",
            str(certificate_path),
        ]
    )
    assert exit_code == EXIT_SUCCESS
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out)["valid"] is True


def test_verify_invalid_certificate_returns_one(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])
    graph_path = tmp_path / "edge.g6"
    graph_path.write_text(graph.to_graph6() + "\n", encoding="ascii")
    certificate = TotalColoringCertificate.create(graph, 3, (0, 0), (1,))
    certificate_path = tmp_path / "bad.json"
    certificate_path.write_text(certificate.to_json(), encoding="utf-8")

    exit_code = main(
        [
            "verify",
            "--graph",
            str(graph_path),
            "--certificate",
            str(certificate_path),
        ]
    )

    assert exit_code == EXIT_NO_WITNESS
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out)["valid"] is False


def test_auxiliary_search_writes_verified_certificate(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (1, 2), (2, 3), (0, 3)])
    graph_path = tmp_path / "cycle.json"
    output = tmp_path / "certificate.json"
    write_graph(graph_path, graph)

    exit_code = main(
        [
            "aux-search",
            "--graph",
            str(graph_path),
            "--colors",
            "5",
            "--certificate-out",
            str(output),
        ]
    )

    assert exit_code == EXIT_SUCCESS
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["status"] == "witness"
    assert output.is_file()


def test_limits_map_to_unknown_exit_code(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (1, 2), (2, 3), (0, 3)])
    graph_path = tmp_path / "cycle.json"
    write_graph(graph_path, graph)

    exit_code = main(
        [
            "aux-search",
            "--graph",
            str(graph_path),
            "--colors",
            "5",
            "--max-nodes",
            "1",
        ]
    )

    assert exit_code == EXIT_UNKNOWN
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out)["status"] == "unknown"


def test_universal_auxiliary_cli_reports_all_partitions(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(4, [(0, 1), (1, 2), (2, 3), (0, 3)])
    graph_path = tmp_path / "cycle.json"
    write_graph(graph_path, graph)

    exit_code = main(
        [
            "aux-check-all",
            "--graph",
            str(graph_path),
            "--colors",
            "4",
        ]
    )

    assert exit_code == EXIT_SUCCESS
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["status"] == "witness"
    assert payload["verified_partitions"] == 2


def test_proof_audit_exposes_failed_draft_implication(capsys: object) -> None:
    exit_code = main(["proof-audit", "--repeated", "1", "--singletons", "1", "--cap", "2"])

    assert exit_code == EXIT_NO_WITNESS
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["draft_final_inequality"]["holds"] is False
    assert payload["corrected_incidence_closure"]["holds"] is True


def test_cli_refuses_overwrite_without_force(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])
    graph_path = tmp_path / "edge.json"
    output = tmp_path / "certificate.json"
    write_graph(graph_path, graph)
    output.write_text("do not overwrite", encoding="utf-8")

    exit_code = main(
        [
            "solve",
            "--graph",
            str(graph_path),
            "--colors",
            "3",
            "--certificate-out",
            str(output),
        ]
    )

    assert exit_code == EXIT_ERROR
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "refusing to overwrite" in captured.err
    assert output.read_text(encoding="utf-8") == "do not overwrite"


def test_cli_reports_malformed_input_without_traceback(tmp_path: Path, capsys: object) -> None:
    graph_path = tmp_path / "bad.json"
    graph_path.write_text("not JSON", encoding="utf-8")

    exit_code = main(["solve", "--graph", str(graph_path), "--colors", "3"])

    assert exit_code == EXIT_ERROR
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.err)
    assert payload["status"] == "error"


def test_cli_rejects_oversized_graph_and_certificate_inputs(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])
    graph_path = tmp_path / "edge.json"
    certificate_path = tmp_path / "certificate.json"
    write_graph(graph_path, graph)
    certificate = TotalColoringCertificate.create(graph, 3, (0, 1), (2,))
    certificate_path.write_text(certificate.to_json(), encoding="utf-8")

    solve_code = main(
        [
            "solve",
            "--graph",
            str(graph_path),
            "--colors",
            "3",
            "--max-input-bytes",
            "4",
        ]
    )
    assert solve_code == EXIT_ERROR
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "input exceeds --max-input-bytes=4" in captured.err

    verify_code = main(
        [
            "verify",
            "--graph",
            str(graph_path),
            "--certificate",
            str(certificate_path),
            "--max-input-bytes",
            str(graph_path.stat().st_size),
        ]
    )
    assert verify_code == EXIT_ERROR
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert str(certificate_path) in captured.err


@pytest.mark.parametrize("limit", ["0", "-1", "not-an-integer"])
def test_cli_rejects_invalid_input_byte_limits(limit: str) -> None:
    with pytest.raises(SystemExit) as error:
        main(["solve", "--graph", "unused", "--colors", "3", "--max-input-bytes", limit])

    assert error.value.code == 2


def test_direct_candidate_negative_uses_distinct_exit_status(
    tmp_path: Path, capsys: object
) -> None:
    graph = SimpleGraph.from_edges(3, [(0, 1), (0, 2), (1, 2)])
    graph_path = tmp_path / "triangle.json"
    write_graph(graph_path, graph)

    exit_code = main(["solve", "--graph", str(graph_path), "--colors", "2"])

    assert exit_code == EXIT_NO_WITNESS
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out)["status"] == "candidate_unsat"


def test_force_replaces_existing_certificate_atomically(tmp_path: Path, capsys: object) -> None:
    graph = SimpleGraph.from_edges(2, [(0, 1)])
    graph_path = tmp_path / "edge.json"
    output = tmp_path / "certificate.json"
    write_graph(graph_path, graph)
    output.write_text("old", encoding="utf-8")

    exit_code = main(
        [
            "solve",
            "--graph",
            str(graph_path),
            "--colors",
            "3",
            "--certificate-out",
            str(output),
            "--force",
        ]
    )

    assert exit_code == EXIT_SUCCESS
    capsys.readouterr()  # type: ignore[attr-defined]
    assert TotalColoringCertificate.from_json(output.read_bytes()).verify(graph).valid


def test_missing_graph_and_malformed_certificate_are_operational_errors(
    tmp_path: Path, capsys: object
) -> None:
    missing_code = main(["solve", "--graph", str(tmp_path / "missing.json"), "--colors", "3"])
    assert missing_code == EXIT_ERROR
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "cannot read" in captured.err

    graph = SimpleGraph.from_edges(2, [(0, 1)])
    graph_path = tmp_path / "edge.json"
    certificate_path = tmp_path / "bad-certificate.json"
    write_graph(graph_path, graph)
    certificate_path.write_text("{}", encoding="utf-8")

    verify_code = main(
        [
            "verify",
            "--graph",
            str(graph_path),
            "--certificate",
            str(certificate_path),
        ]
    )
    assert verify_code == EXIT_ERROR
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "invalid certificate" in captured.err


@pytest.mark.parametrize(
    ("counts", "expected_exit"),
    [
        (CensusCounts(witness=2, skipped=1), EXIT_SUCCESS),
        (CensusCounts(candidate_unsat=1), EXIT_NO_WITNESS),
        (CensusCounts(unknown=1), EXIT_UNKNOWN),
        (CensusCounts(error=1), EXIT_ERROR),
    ],
)
def test_census_cli_maps_terminal_counts_to_exit_codes(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
    counts: CensusCounts,
    expected_exit: int,
) -> None:
    def fake_run_census(config: CensusConfig, output: str, *, executable: str) -> CensusRunResult:
        assert output == str(tmp_path / "run")
        assert executable == "custom-geng"
        assert config.require_high_degree is True
        return CensusRunResult(
            run_fingerprint="0" * 64,
            record_count=counts.total,
            counts=counts,
            resumed_records=0,
            records_path=tmp_path / "run" / "records.jsonl",
            manifest_path=tmp_path / "run" / "manifest.json",
            completion_path=tmp_path / "run" / "completion.json",
        )

    monkeypatch.setattr("total_coloring.cli.run_census", fake_run_census)
    exit_code = main(
        [
            "census",
            "--order",
            "4",
            "--output",
            str(tmp_path / "run"),
            "--geng",
            "custom-geng",
        ]
    )

    assert exit_code == expected_exit
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["counts"] == counts.to_dict()
    assert payload["status"] == "complete"
