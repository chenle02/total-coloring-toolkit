"""Stable command-line interface for solving and verifying small instances."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

from total_coloring import __version__
from total_coloring.auxiliary import (
    check_all_auxiliary_partitions,
    search_auxiliary_extensions,
)
from total_coloring.backends import DEFAULT_SOLVER_BACKEND, SolverBackend
from total_coloring.census import CensusConfig, CensusCounts, CensusError, run_census
from total_coloring.certificates import TotalColoringCertificate, verify_total_coloring
from total_coloring.geng import GengError, GengSpec
from total_coloring.graph import SimpleGraph
from total_coloring.proof_audit import (
    CountingParameters,
    audit_corrected_incidence_closure,
    audit_draft_final_inequality,
)
from total_coloring.solver import SearchLimits, SolveStatus, solve_dsatur
from total_coloring.total import split_total_assignment, total_coloring_problem
from total_coloring.universal_census import (
    DEFAULT_UNIVERSAL_CHECKS,
    UniversalCensusConfig,
    UniversalCensusCounts,
    UniversalCheckSpec,
    run_universal_census,
)
from total_coloring.universal_release import (
    UniversalReleaseConfig,
    UniversalReleaseError,
    export_universal_release,
)

EXIT_SUCCESS = 0
EXIT_NO_WITNESS = 1
EXIT_ERROR = 2
EXIT_UNKNOWN = 3
DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024


class CliError(RuntimeError):
    """Expected command-line operational error without a traceback."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _emit(value: object, *, stream: TextIO | None = None) -> None:
    active_stream = sys.stdout if stream is None else stream
    active_stream.write(_canonical_json(value) + "\n")


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _universal_check(value: str) -> UniversalCheckSpec:
    """Parse ``BACKEND:OFFSET`` with concise or stable backend identifiers."""

    try:
        backend_text, offset_text = value.rsplit(":", 1)
        aliases = {
            "dsatur": SolverBackend.DSATUR,
            "static": SolverBackend.STATIC,
        }
        backend = aliases[backend_text] if backend_text in aliases else SolverBackend(backend_text)
        offset = int(offset_text)
        return UniversalCheckSpec(backend, offset)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "must be BACKEND:OFFSET, for example dsatur:1 or static:1"
        ) from exc


def _read_bytes(path: str, *, max_bytes: int) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise CliError("max input bytes must be a positive integer")
    try:
        if path == "-":
            payload = sys.stdin.buffer.read(max_bytes + 1)
        else:
            with Path(path).open("rb") as handle:
                payload = handle.read(max_bytes + 1)
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc}") from exc
    if len(payload) > max_bytes:
        raise CliError(f"input exceeds --max-input-bytes={max_bytes}: {path}")
    return payload


def _load_graph(path: str, input_format: str, *, max_bytes: int) -> SimpleGraph:
    payload = _read_bytes(path, max_bytes=max_bytes)
    selected = input_format
    if selected == "auto":
        selected = "json" if payload.lstrip().startswith(b"{") else "graph6"
    try:
        if selected == "json":
            return SimpleGraph.from_json(payload)
        return SimpleGraph.from_graph6(payload)
    except ValueError as exc:
        raise CliError(f"invalid {selected} graph: {exc}") from exc


def _atomic_write(path: Path, data: bytes, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise CliError(f"refusing to overwrite existing file: {path}")
    if path.is_symlink():
        raise CliError(f"refusing to replace a symlink: {path}")
    parent = path.parent
    if not parent.is_dir():
        raise CliError(f"output directory does not exist: {parent}")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise CliError(f"cannot atomically write {path}: {exc}") from exc
    finally:
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                Path(temporary_name).unlink()


def _write_certificate(
    certificate: TotalColoringCertificate, path: str | None, *, overwrite: bool
) -> dict[str, object]:
    value = certificate.to_dict()
    if path is not None:
        _atomic_write(
            Path(path),
            (certificate.to_json() + "\n").encode("utf-8"),
            overwrite=overwrite,
        )
    return value


def _limits(arguments: argparse.Namespace) -> SearchLimits:
    return SearchLimits(
        max_nodes=arguments.max_nodes,
        timeout_seconds=arguments.timeout,
    )


def _status_exit(status: SolveStatus) -> int:
    if status is SolveStatus.WITNESS:
        return EXIT_SUCCESS
    if status is SolveStatus.CANDIDATE_UNSAT:
        return EXIT_NO_WITNESS
    if status is SolveStatus.UNKNOWN:
        return EXIT_UNKNOWN
    return EXIT_ERROR


def _command_verify(arguments: argparse.Namespace) -> int:
    graph = _load_graph(
        arguments.graph,
        arguments.graph_format,
        max_bytes=arguments.max_input_bytes,
    )
    try:
        certificate = TotalColoringCertificate.from_json(
            _read_bytes(arguments.certificate, max_bytes=arguments.max_input_bytes)
        )
    except ValueError as exc:
        raise CliError(f"invalid certificate: {exc}") from exc
    result = verify_total_coloring(graph, certificate)
    _emit(
        {
            "certificate_fingerprint": certificate.fingerprint,
            "graph_fingerprint": graph.fingerprint,
            "issues": [
                {"code": issue.code, "message": issue.message, "path": issue.path}
                for issue in result.issues
            ],
            "valid": result.valid,
        }
    )
    return EXIT_SUCCESS if result.valid else EXIT_NO_WITNESS


def _command_solve(arguments: argparse.Namespace) -> int:
    graph = _load_graph(
        arguments.graph,
        arguments.graph_format,
        max_bytes=arguments.max_input_bytes,
    )
    problem = total_coloring_problem(graph, arguments.colors)
    solved = solve_dsatur(problem, limits=_limits(arguments))
    payload: dict[str, Any] = {
        "detail": solved.detail,
        "graph_fingerprint": graph.fingerprint,
        "problem_digest": solved.problem_digest,
        "stats": {
            "backtracks": solved.stats.backtracks,
            "elapsed_seconds": solved.stats.elapsed_seconds,
            "nodes": solved.stats.nodes,
        },
        "status": solved.status.value,
    }
    if solved.status is SolveStatus.WITNESS:
        if solved.assignment is None:
            raise CliError("solver reported a witness without an assignment")
        vertex_colors, edge_colors = split_total_assignment(graph, solved.assignment)
        certificate = TotalColoringCertificate.create(
            graph, arguments.colors, vertex_colors, edge_colors
        )
        verify_total_coloring(graph, certificate).require_valid()
        payload["certificate"] = _write_certificate(
            certificate, arguments.certificate_out, overwrite=arguments.force
        )
    _emit(payload)
    return _status_exit(solved.status)


def _command_aux_search(arguments: argparse.Namespace) -> int:
    graph = _load_graph(
        arguments.graph,
        arguments.graph_format,
        max_bytes=arguments.max_input_bytes,
    )
    result = search_auxiliary_extensions(
        graph,
        arguments.colors,
        limits_per_partition=_limits(arguments),
        max_partitions=arguments.max_partitions,
        backend=arguments.backend,
    )
    payload: dict[str, Any] = {
        "backend_id": arguments.backend.value,
        "candidate_failures": result.candidate_failures,
        "color_count": result.color_count,
        "detail": result.detail,
        "graph_fingerprint": result.graph_fingerprint,
        "partitions_completed": result.partitions_completed,
        "partitions_started": result.partitions_started,
        "status": result.status.value,
        "unknown_partitions": result.unknown_partitions,
    }
    if result.witness is not None:
        payload["partition"] = {
            "pairs": [list(edge) for edge in result.witness.partition.pairs],
            "singletons": list(result.witness.partition.singletons),
        }
        payload["certificate"] = _write_certificate(
            result.witness.total_coloring,
            arguments.certificate_out,
            overwrite=arguments.force,
        )
    _emit(payload)
    return _status_exit(result.status)


def _command_aux_check_all(arguments: argparse.Namespace) -> int:
    graph = _load_graph(
        arguments.graph,
        arguments.graph_format,
        max_bytes=arguments.max_input_bytes,
    )
    result = check_all_auxiliary_partitions(
        graph,
        arguments.colors,
        limits_per_partition=_limits(arguments),
        max_partitions=arguments.max_partitions,
        fix_distinguished_colors=not arguments.unfixed_distinguished_colors,
        backend=arguments.backend,
    )
    payload: dict[str, Any] = {
        "backend_id": arguments.backend.value,
        "color_count": result.color_count,
        "detail": result.detail,
        "graph_fingerprint": result.graph_fingerprint,
        "partitions_started": result.partitions_started,
        "status": result.status.value,
        "unknown_partitions": result.unknown_partitions,
        "verified_partitions": result.verified_partitions,
    }
    if result.first_nonwitness is not None:
        payload["first_nonwitness"] = {
            "detail": result.first_nonwitness.solve_result.detail,
            "pairs": [list(edge) for edge in result.first_nonwitness.construction.partition.pairs],
            "singletons": list(result.first_nonwitness.construction.partition.singletons),
            "status": result.first_nonwitness.status.value,
        }
    _emit(payload)
    return _status_exit(result.status)


def _fraction_dict(value: Fraction) -> dict[str, int]:
    return {"denominator": value.denominator, "numerator": value.numerator}


def _command_proof_audit(arguments: argparse.Namespace) -> int:
    parameters = CountingParameters(
        repeated_colors=arguments.repeated,
        singleton_colors=arguments.singletons,
        multiplicity_cap=Fraction(arguments.cap),
    )
    draft = audit_draft_final_inequality(parameters)
    corrected = audit_corrected_incidence_closure(parameters)
    _emit(
        {
            "corrected_incidence_closure": {
                "holds": corrected.holds,
                "left": _fraction_dict(corrected.left),
                "margin": _fraction_dict(corrected.margin),
                "right": _fraction_dict(corrected.right),
            },
            "draft_final_inequality": {
                "holds": draft.holds,
                "left": _fraction_dict(draft.left),
                "margin": _fraction_dict(draft.margin),
                "right": _fraction_dict(draft.right),
            },
            "parameters": {
                "multiplicity_cap": _fraction_dict(parameters.multiplicity_cap),
                "repeated_colors": parameters.repeated_colors,
                "singleton_colors": parameters.singleton_colors,
            },
        }
    )
    return EXIT_SUCCESS if draft.holds else EXIT_NO_WITNESS


def _command_census(arguments: argparse.Namespace) -> int:
    config = CensusConfig(
        geng=GengSpec(
            order=arguments.order,
            connected=arguments.connected,
            min_degree=arguments.min_degree,
            max_degree=arguments.max_degree,
            shard_index=arguments.shard_index,
            shard_count=arguments.shard_count,
        ),
        color_offset_from_degree_parameter=arguments.color_offset,
        require_high_degree=not arguments.relaxed_partition_domain,
        limits_per_partition=_limits(arguments),
        max_partitions=arguments.max_partitions,
        checkpoint_interval=arguments.checkpoint_interval,
    )
    result = run_census(config, arguments.output, executable=arguments.geng)
    payload = {
        "completion_path": str(result.completion_path),
        "counts": result.counts.to_dict(),
        "manifest_path": str(result.manifest_path),
        "record_count": result.record_count,
        "records_path": str(result.records_path),
        "resumed_records": result.resumed_records,
        "run_fingerprint": result.run_fingerprint,
        "status": "complete",
    }
    _emit(payload)
    return _census_exit(result.counts)


def _census_exit(counts: CensusCounts) -> int:
    if counts.error:
        return EXIT_ERROR
    if counts.unknown:
        return EXIT_UNKNOWN
    if counts.candidate_unsat:
        return EXIT_NO_WITNESS
    return EXIT_SUCCESS


def _command_universal_census(arguments: argparse.Namespace) -> int:
    checks = DEFAULT_UNIVERSAL_CHECKS if arguments.check is None else tuple(arguments.check)
    config = UniversalCensusConfig(
        geng=GengSpec(
            order=arguments.order,
            connected=arguments.connected,
            min_degree=arguments.min_degree,
            max_degree=arguments.max_degree,
            shard_index=arguments.shard_index,
            shard_count=arguments.shard_count,
        ),
        checks=checks,
        require_high_degree=not arguments.relaxed_partition_domain,
        limits_per_check=_limits(arguments),
        checkpoint_interval=arguments.checkpoint_interval,
    )
    result = run_universal_census(config, arguments.output, executable=arguments.geng)
    _emit(
        {
            "completion_path": str(result.completion_path),
            "counts": result.counts.to_dict(),
            "manifest_path": str(result.manifest_path),
            "partition_count": result.partition_count,
            "record_count": result.record_count,
            "records_path": str(result.records_path),
            "resumed_records": result.resumed_records,
            "run_fingerprint": result.run_fingerprint,
            "status": "complete",
        }
    )
    return _universal_census_exit(result.counts)


def _universal_census_exit(counts: UniversalCensusCounts) -> int:
    if counts.error:
        return EXIT_ERROR
    if counts.unknown:
        return EXIT_UNKNOWN
    if counts.candidate_unsat:
        return EXIT_NO_WITNESS
    return EXIT_SUCCESS


def _command_universal_export(arguments: argparse.Namespace) -> int:
    result = export_universal_release(
        arguments.run,
        UniversalReleaseConfig(
            bundle_root=Path(arguments.bundle),
            archive_path=Path(arguments.archive),
            summary_id=arguments.summary_id,
            created_utc=arguments.created_utc,
            release_version=arguments.release_version,
            release_status=arguments.release_status,
            code_commit=arguments.code_commit,
            code_repository=arguments.code_repository,
            dataset_repository=arguments.dataset_repository,
            external_artifact=PurePosixPath(arguments.external_name),
            external_url=arguments.external_url,
            claim_id=arguments.claim_id,
            expected_toolkit_source_sha256=arguments.expected_toolkit_source_sha256,
            expected_generator_sha256=arguments.expected_generator_sha256,
        ),
        executable=arguments.geng,
    )
    _emit(
        {
            "archive_bytes": result.archive_bytes,
            "archive_path": str(result.archive_path),
            "archive_sha256": result.archive_sha256,
            "bundle_root": str(result.bundle_root),
            "manifest_path": str(result.manifest_path),
            "orders": list(result.orders),
            "status": "complete",
            "summary_path": str(result.summary_path),
            "totals": result.totals,
        }
    )
    return EXIT_SUCCESS


def _add_graph_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--graph", required=True, help="graph JSON/graph6 path, or - for stdin")
    parser.add_argument(
        "--graph-format",
        choices=("auto", "json", "graph6"),
        default="auto",
        help="input format (default: infer from content)",
    )
    parser.add_argument(
        "--max-input-bytes",
        type=_positive_integer,
        default=DEFAULT_MAX_INPUT_BYTES,
        help=f"maximum bytes read from each input (default: {DEFAULT_MAX_INPUT_BYTES})",
    )


def _add_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-nodes", type=int)
    parser.add_argument("--timeout", type=float, help="per-search wall limit in seconds")


def _add_certificate_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--certificate-out", help="atomically write a verified certificate")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")


def build_parser() -> argparse.ArgumentParser:
    """Build the public parser without reading process-global arguments."""

    parser = argparse.ArgumentParser(prog="total-coloring", allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="independently verify a certificate")
    _add_graph_input(verify)
    verify.add_argument("--certificate", required=True, help="certificate JSON path")
    verify.set_defaults(handler=_command_verify)

    solve = commands.add_parser("solve", help="solve a direct total-coloring instance")
    _add_graph_input(solve)
    solve.add_argument("--colors", type=int, required=True)
    _add_limits(solve)
    _add_certificate_output(solve)
    solve.set_defaults(handler=_command_solve)

    auxiliary = commands.add_parser(
        "aux-search", help="search all equitable partitions for a rainbow extension"
    )
    _add_graph_input(auxiliary)
    auxiliary.add_argument("--colors", type=int, required=True)
    auxiliary.add_argument("--max-partitions", type=int)
    auxiliary.add_argument(
        "--backend",
        type=SolverBackend,
        choices=tuple(SolverBackend),
        default=DEFAULT_SOLVER_BACKEND,
        help="deterministic solver backend (default: dsatur-iterative-v1)",
    )
    _add_limits(auxiliary)
    _add_certificate_output(auxiliary)
    auxiliary.set_defaults(handler=_command_aux_search)

    universal = commands.add_parser(
        "aux-check-all", help="test the stronger extension claim for every partition"
    )
    _add_graph_input(universal)
    universal.add_argument("--colors", type=int, required=True)
    universal.add_argument("--max-partitions", type=int)
    universal.add_argument(
        "--backend",
        type=SolverBackend,
        choices=tuple(SolverBackend),
        default=DEFAULT_SOLVER_BACKEND,
        help="deterministic solver backend (default: dsatur-iterative-v1)",
    )
    universal.add_argument(
        "--unfixed-distinguished-colors",
        action="store_true",
        help="disable the without-loss canonical color symmetry fixing",
    )
    _add_limits(universal)
    universal.set_defaults(handler=_command_aux_check_all)

    proof = commands.add_parser("proof-audit", help="audit draft counting inequalities exactly")
    proof.add_argument("--repeated", type=int, required=True)
    proof.add_argument("--singletons", type=int, required=True)
    proof.add_argument("--cap", required=True, help="positive integer or rational, e.g. 2 or 5/2")
    proof.set_defaults(handler=_command_proof_audit)

    census = commands.add_parser("census", help="run or resume one hash-pinned geng shard")
    census.add_argument("--order", type=int, required=True)
    census.add_argument("--output", required=True, help="dedicated census output directory")
    census.add_argument(
        "--geng",
        default="geng",
        help="geng executable name or path (default auto-detects geng or nauty-geng)",
    )
    census.add_argument("--connected", action="store_true")
    census.add_argument("--min-degree", type=int)
    census.add_argument("--max-degree", type=int)
    census.add_argument("--shard-index", type=int)
    census.add_argument("--shard-count", type=int)
    census.add_argument(
        "--color-offset",
        type=int,
        default=2,
        help="palette size D+offset (default 2, corresponding to Delta(G)+3)",
    )
    census.add_argument("--max-partitions", type=int)
    census.add_argument("--checkpoint-interval", type=int, default=1)
    census.add_argument(
        "--relaxed-partition-domain",
        action="store_true",
        help="use n <= 2(Delta+1), one degree broader than the paper regime",
    )
    _add_limits(census)
    census.set_defaults(handler=_command_census)

    all_partitions = commands.add_parser(
        "universal-census",
        help="run or resume a replayable all-partition geng census",
    )
    all_partitions.add_argument("--order", type=int, required=True)
    all_partitions.add_argument(
        "--output", required=True, help="dedicated universal-census output directory"
    )
    all_partitions.add_argument(
        "--geng",
        default="geng",
        help="geng executable name or path (default auto-detects geng or nauty-geng)",
    )
    all_partitions.add_argument("--connected", action="store_true")
    all_partitions.add_argument("--min-degree", type=int)
    all_partitions.add_argument("--max-degree", type=int)
    all_partitions.add_argument("--shard-index", type=int)
    all_partitions.add_argument("--shard-count", type=int)
    all_partitions.add_argument(
        "--check",
        action="append",
        type=_universal_check,
        help=("repeat BACKEND:OFFSET checks (default: dsatur:1, dsatur:2, static:1)"),
    )
    all_partitions.add_argument("--checkpoint-interval", type=int, default=1)
    all_partitions.add_argument(
        "--relaxed-partition-domain",
        action="store_true",
        help="use n <= 2(Delta+1), one degree broader than the paper regime",
    )
    _add_limits(all_partitions)
    all_partitions.set_defaults(handler=_command_universal_census)

    export = commands.add_parser(
        "universal-export",
        help="replay completed universal runs and build a deterministic public candidate",
    )
    export.add_argument(
        "--run",
        action="append",
        required=True,
        help="completed per-order universal-census directory (repeat in any order)",
    )
    export.add_argument("--bundle", required=True, help="new compact candidate-bundle path")
    export.add_argument("--archive", required=True, help="new external replay .tar.gz path")
    export.add_argument("--summary-id", required=True)
    export.add_argument("--created-utc", required=True, help="canonical YYYY-MM-DDTHH:MM:SSZ")
    export.add_argument("--release-version", required=True, help="candidate dataset SemVer")
    export.add_argument("--release-status", choices=("candidate", "published"), default="candidate")
    export.add_argument("--code-commit", required=True, help="generating 40-hex toolkit commit")
    export.add_argument(
        "--code-repository",
        default="https://github.com/chenle02/total-coloring-toolkit",
    )
    export.add_argument(
        "--dataset-repository",
        default="https://github.com/chenle02/total-coloring-data",
    )
    export.add_argument(
        "--external-name",
        required=True,
        help="logical release-asset name, e.g. archives/order-1-8-replay-v1.tar.gz",
    )
    export.add_argument("--external-url", required=True, help="future stable HTTPS release URL")
    export.add_argument("--claim-id", default="UAUX-BOUND")
    export.add_argument("--expected-toolkit-source-sha256")
    export.add_argument("--expected-generator-sha256")
    export.add_argument(
        "--geng",
        default="geng",
        help="local geng executable used for exact stream regeneration",
    )
    export.set_defaults(handler=_command_universal_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return a stable process exit code."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        handler = arguments.handler
        return int(handler(arguments))
    except (CensusError, CliError, GengError, OSError, UniversalReleaseError, ValueError) as exc:
        _emit({"error": str(exc), "status": "error"}, stream=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
