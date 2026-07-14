from __future__ import annotations

import copy
import errno
import gzip
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

import total_coloring.publishing as publishing
import total_coloring.universal_census as universal
import total_coloring.universal_release as release
from total_coloring.backends import SolverBackend, solve_with_backend
from total_coloring.census import ToolkitIdentity
from total_coloring.geng import GengIdentity, GengSpec
from total_coloring.graph import SimpleGraph, canonical_json_bytes
from total_coloring.publishing import (
    BundleVerificationError,
    ExternalArtifactFile,
    PublicationConfig,
    plan_promotion,
)
from total_coloring.solver import SolveResult, SolveStatus
from total_coloring.universal_census import (
    UniversalCensusConfig,
    UniversalCheckSpec,
    run_universal_census,
)
from total_coloring.universal_release import (
    DEFAULT_LIMITATIONS,
    ArchiveMemberReceipt,
    UniversalReleaseConfig,
    UniversalReleaseError,
    canonical_finite_scope,
    export_universal_release,
    is_stable_https_url,
    release_schema_digests,
    release_summary_path,
    validate_replay_archive,
)

TEST_TOOLKIT = ToolkitIdentity("0.1.0", "b" * 64, "CPython", "3.13.0")
CODE_COMMIT = "c" * 40
ARCHIVE_NAME = PurePosixPath("archives/order-2-3-universal-census-replay-v1.tar.gz")
ARCHIVE_URL = (
    "https://github.com/chenle02/total-coloring-data/releases/download/"
    "v1.0.0-rc.1/order-2-3-universal-census-replay-v1.tar.gz"
)


def complete(order: int) -> SimpleGraph:
    return SimpleGraph.from_edges(
        order,
        ((left, right) for left in range(order) for right in range(left + 1, order)),
    )


def patch_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        universal,
        "resolve_geng",
        lambda executable="geng": Path("/synthetic") / Path(executable).name,
    )

    def identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        return GengIdentity("geng", "a" * 64, spec.arguments())

    def stream(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
        del executable
        yield complete(spec.order)

    monkeypatch.setattr(universal, "geng_identity", identity)
    monkeypatch.setattr(universal, "stream_geng", stream)


def make_runs(
    root: Path, monkeypatch: pytest.MonkeyPatch, orders: tuple[int, ...] = (2, 3)
) -> tuple[Path, ...]:
    patch_generator(monkeypatch)
    directories: list[Path] = []
    for order in orders:
        directory = root / f"run-{order}"
        run_universal_census(
            UniversalCensusConfig(GengSpec(order)),
            directory,
            toolkit_identity=TEST_TOOLKIT,
        )
        directories.append(directory)
    return tuple(directories)


def release_config(root: Path, suffix: str = "first") -> UniversalReleaseConfig:
    return UniversalReleaseConfig(
        bundle_root=root / f"bundle-{suffix}",
        archive_path=root / f"archive-{suffix}.tar.gz",
        summary_id="order-2-3-universal-census",
        created_utc="2026-07-14T12:00:00Z",
        release_version="1.0.0-rc.1",
        code_commit=CODE_COMMIT,
        external_artifact=ARCHIVE_NAME,
        external_url=ARCHIVE_URL,
        expected_toolkit_source_sha256="b" * 64,
        expected_generator_sha256="a" * 64,
    )


@pytest.mark.parametrize("orders", [(), (2, 1), (1, 1), (-1,), (0,), (True,), (1.0,)])
def test_canonical_finite_scope_rejects_noncanonical_orders(orders: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="positive, unique, and sorted"):
        canonical_finite_scope(orders)


@pytest.mark.parametrize("orders", [(), (2, 1), (1, 1), (0,), (True,), (1.0,)])
def test_release_summary_path_requires_canonical_positive_orders(
    orders: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        release_summary_path(orders)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda values: values.update(summary_id="Bad"), "summary_id"),
        (lambda values: values.update(claim_id="bad"), "claim_id"),
        (lambda values: values.update(release_version="01.0.0"), "SemVer"),
        (lambda values: values.update(release_version="1.0.0-01"), "SemVer"),
        (lambda values: values.update(release_version="1.0.0+build.1"), "SemVer"),
        (lambda values: values.update(release_version="1.0.0\n"), "SemVer"),
        (lambda values: values.update(release_status="development"), "release_status"),
        (lambda values: values.update(code_commit="0" * 40), "code_commit"),
        (lambda values: values.update(created_utc="2026-07-14"), "created_utc"),
        (lambda values: values.update(created_utc="2026-02-30T00:00:00Z"), "real UTC"),
        (lambda values: values.update(code_repository="http://example.org/x"), "code_repository"),
        (lambda values: values.update(dataset_repository="https:///missing"), "dataset_repository"),
        (
            lambda values: values.update(external_artifact=PurePosixPath("../archive.tar.gz")),
            "external_artifact",
        ),
        (
            lambda values: values.update(
                external_artifact=PurePosixPath("archives/.hidden.tar.gz")
            ),
            "hidden",
        ),
        (
            lambda values: values.update(external_artifact=PurePosixPath("archives/a.zip")),
            "tar.gz",
        ),
        (lambda values: values.update(external_url="http://example.org/a.tar.gz"), "external_url"),
        (
            lambda values: values.update(
                external_url=("HTTPS://example.org/order-2-3-universal-census-replay-v1.tar.gz")
            ),
            "external_url",
        ),
        (
            lambda values: values.update(external_url="https://example.org/wrong.tar.gz"),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=(
                    "https://user@example.org/order-2-3-universal-census-replay-v1.tar.gz"
                )
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=("https://example.org:443/order-2-3-universal-census-replay-v1.tar.gz")
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=("https://Example.org/order-2-3-universal-census-replay-v1.tar.gz")
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=(
                    "https://example.org/a/../order-2-3-universal-census-replay-v1.tar.gz"
                )
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=(
                    "https://example.org/a/%2e%2e/order-2-3-universal-census-replay-v1.tar.gz"
                )
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=(
                    "https://example.org/v%31/order-2-3-universal-census-replay-v1.tar.gz"
                )
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=("https://example.org//order-2-3-universal-census-replay-v1.tar.gz")
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=(
                    "https://example.org/order-2-3-universal-census-replay-v1.tar.gz?download=1"
                )
            ),
            "external_url",
        ),
        (
            lambda values: values.update(
                external_url=("https://example.org/order-2-3-universal-census-replay-v1.tar.gz ")
            ),
            "external_url",
        ),
        (
            lambda values: values.update(expected_toolkit_source_sha256="bad"),
            "toolkit_source",
        ),
        (lambda values: values.update(expected_generator_sha256="bad"), "generator_sha256"),
    ],
)
def test_release_config_rejects_noncanonical_metadata(
    tmp_path: Path,
    change: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    baseline = release_config(tmp_path)
    values = {field: getattr(baseline, field) for field in baseline.__dataclass_fields__}
    change(values)
    with pytest.raises(ValueError, match=message):
        UniversalReleaseConfig(**values)


def test_release_config_semver_rejects_long_near_miss_without_backtracking_blowup(
    tmp_path: Path,
) -> None:
    baseline = release_config(tmp_path)
    values = {field: getattr(baseline, field) for field in baseline.__dataclass_fields__}
    values["release_version"] = "1.2.3-" + "a" * 20_000 + "+"
    with pytest.raises(ValueError, match="SemVer"):
        UniversalReleaseConfig(**values)


def test_stable_external_url_uses_exact_ascii_pchar_without_percent_encoding() -> None:
    assert is_stable_https_url("https://example.org/Az09._~!$&'()*+,;=:@-/artifact.tar.gz")
    for character in ('"', "{", "}", "[", "]", "<", ">", "^", "`", "|", "\x7f"):
        assert not is_stable_https_url(f"https://example.org/release/{character}/artifact")
    for character in (" ", "\\", "%", "?", "#", "é", "\x00", "\x1f"):
        assert not is_stable_https_url(f"https://example.org/release/{character}/artifact")


def test_stable_external_url_rejects_overlong_dns_hostname() -> None:
    hostname = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
    assert len(hostname) == 253
    assert is_stable_https_url(f"https://{hostname}/artifact")
    assert not is_stable_https_url(f"https://x.{hostname}/artifact")


def _bundle_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _summary(result_path: Path) -> dict[str, object]:
    value = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _refresh_bundle_summary(
    bundle: Path, summary_path: Path, summary: object, *, sync_external: bool = False
) -> None:
    summary_path.write_text(
        json.dumps(summary, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_digest = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    manifest_path = bundle / "manifests/dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["bytes"] = summary_path.stat().st_size
    manifest["artifacts"][0]["sha256"] = summary_digest
    if (
        sync_external
        and isinstance(summary, dict)
        and isinstance(summary.get("replay_archive"), dict)
    ):
        manifest["external_artifacts"][0]["bytes"] = summary["replay_archive"]["bytes"]
        manifest["external_artifacts"][0]["sha256"] = summary["replay_archive"]["sha256"]
    manifest_path.write_text(
        json.dumps(manifest, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (bundle / "SHA256SUMS").write_text(
        f"{summary_digest}  {summary_path.relative_to(bundle).as_posix()}\n",
        encoding="ascii",
    )


def _destination(root: Path) -> Path:
    root.mkdir()
    (root / "reports").mkdir()
    (root / "results").mkdir()
    (root / "README.md").write_text("candidate destination\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "bootstrap"], check=True)
    return root


def test_export_replays_runs_and_is_byte_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch)
    first = export_universal_release(runs[::-1], release_config(tmp_path), executable="geng")
    second = export_universal_release(runs, release_config(tmp_path, "second"), executable="geng")

    assert first.orders == (2, 3)
    assert first.archive_sha256 == second.archive_sha256
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert _bundle_files(first.bundle_root) == _bundle_files(second.bundle_root)
    assert first.summary_path.name == "order-2-3-universal-census-summary-v1.json"
    summary = _summary(first.summary_path)
    assert summary["producer"] == {
        "commit": CODE_COMMIT,
        "distribution_version": "0.1.0",
        "python_implementation": "CPython",
        "python_version": "3.13.0",
        "repository": "https://github.com/chenle02/total-coloring-toolkit",
        "source_sha256": "b" * 64,
    }
    assert summary["totals"] == {
        "check_evaluations": 6,
        "counts": {
            "candidate_unsat": 0,
            "error": 0,
            "skipped": 0,
            "unknown": 0,
            "verified_all": 2,
        },
        "order_count": 2,
        "partition_count": 2,
        "record_count": 2,
    }
    claim = summary["claims"][0]  # type: ignore[index]
    assert claim["claim_type"] == "finite_bound"
    assert claim["status"] == "verified_in_finite_scope"
    assert claim["finite_scope"] == canonical_finite_scope([2, 3])
    assert claim["orders"] == [2, 3]
    assert claim["limitations"] == list(DEFAULT_LIMITATIONS)
    assert summary["limitations"] == list(DEFAULT_LIMITATIONS)
    assert claim["required_checks"] == [
        "dsatur-delta-plus-2",
        "dsatur-delta-plus-3",
        "static-delta-plus-2",
    ]

    with tarfile.open(first.archive_path, "r:gz") as archive:
        assert archive.getnames() == [
            "order-02/completion.json",
            "order-02/manifest.json",
            "order-02/records.jsonl",
            "order-03/completion.json",
            "order-03/manifest.json",
            "order-03/records.jsonl",
        ]
        assert all(member.mode == 0o644 and member.mtime == 0 for member in archive)


def test_private_snapshot_permissions_and_original_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    original_records = (runs[0] / "records.jsonl").read_bytes()
    with release._private_run_snapshots(runs, parent=tmp_path) as snapshots:
        snapshot = snapshots.paths[0]
        root = snapshot.parent
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
        assert {path.name for path in snapshot.iterdir()} == {
            "manifest.json",
            "completion.json",
            "records.jsonl",
        }
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in snapshot.iterdir())
        (runs[0] / "records.jsonl").write_bytes(b"original changed after snapshot\n")
        assert (snapshot / "records.jsonl").read_bytes() == original_records
        snapshot_root = root
    assert not snapshot_root.exists()


@pytest.mark.parametrize("attack", ["directory", "ancestor", "member", "fifo"])
def test_private_snapshot_rejects_symlinks_and_fifo_without_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    run = make_runs(tmp_path / "source", monkeypatch, (2,))[0]
    source: Path = run
    if attack == "directory":
        source = tmp_path / "run-link"
        source.symlink_to(run, target_is_directory=True)
    elif attack == "ancestor":
        ancestor = tmp_path / "ancestor-link"
        ancestor.symlink_to(run.parent, target_is_directory=True)
        source = ancestor / run.name
    elif attack == "member":
        records = run / "records.jsonl"
        target = run / "records-target.jsonl"
        records.rename(target)
        records.symlink_to(target.name)
    else:
        records = run / "records.jsonl"
        records.unlink()
        os.mkfifo(records)

    with (
        pytest.raises(UniversalReleaseError, match=r"capture|regular|safely open"),
        release._private_run_snapshots((source,), parent=tmp_path),
    ):
        pytest.fail("unsafe run input must not be yielded")


@pytest.mark.parametrize("stage", ["copy", "validation", "receipt", "archive"])
def test_export_rejects_snapshot_mutation_at_every_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    if stage == "copy":
        original = release._copy_snapshot_member

        def copy_then_mutate(
            source_descriptor: int, destination_descriptor: int, name: str
        ) -> object:
            result = original(source_descriptor, destination_descriptor, name)
            if name == "completion.json":
                with (runs[0] / "records.jsonl").open("ab") as stream:
                    stream.write(b"copy-race")
            return result

        monkeypatch.setattr(release, "_copy_snapshot_member", copy_then_mutate)
    elif stage == "validation":
        original_validation = universal.validate_completed_universal_census

        def validate_then_mutate(*args: object, **kwargs: object) -> object:
            result = original_validation(*args, **kwargs)  # type: ignore[arg-type]
            with result.result.records_path.open("ab") as stream:
                stream.write(b"validation-race")
            return result

        monkeypatch.setattr(release, "validate_completed_universal_census", validate_then_mutate)
    elif stage == "receipt":
        original_exports = release._make_exports

        def receipt_then_mutate(validations: object) -> object:
            exports = original_exports(validations)  # type: ignore[arg-type]
            with exports[0].validation.result.records_path.open("ab") as stream:
                stream.write(b"receipt-race")
            return exports

        monkeypatch.setattr(release, "_make_exports", receipt_then_mutate)
    else:
        original_archive = release._write_deterministic_archive

        def archive_then_mutate(path: Path, sources: object) -> None:
            source_items: tuple[tuple[ArchiveMemberReceipt, Path], ...] = tuple(
                cast(Iterable[tuple[ArchiveMemberReceipt, Path]], sources)
            )
            original_archive(path, source_items)
            records_source = next(
                source for receipt, source in source_items if receipt.path.name == "records.jsonl"
            )
            with records_source.open("ab") as stream:
                stream.write(b"archive-race")

        monkeypatch.setattr(release, "_write_deterministic_archive", archive_then_mutate)

    config = release_config(tmp_path)
    with pytest.raises(UniversalReleaseError, match=r"changed|replaced"):
        export_universal_release(runs, config)
    assert not config.bundle_root.exists()
    assert not config.archive_path.exists()
    assert not list(tmp_path.glob(".universal-census-snapshots-*"))


def test_run_limit_is_rejected_before_snapshot_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        release,
        "_private_run_snapshots",
        lambda *_args, **_kwargs: pytest.fail("run-limit rejection must precede snapshotting"),
    )
    with pytest.raises(UniversalReleaseError, match="at most 256"):
        export_universal_release(
            tuple(Path(str(index)) for index in range(257)), release_config(tmp_path)
        )


def test_export_rejects_run_tampering_and_wrong_expected_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    records = runs[0] / "records.jsonl"
    records.write_bytes(records.read_bytes() + b"tamper")
    with pytest.raises(UniversalReleaseError, match=r"validation failed.*digest"):
        export_universal_release(runs, release_config(tmp_path))

    runs = make_runs(tmp_path / "fresh", monkeypatch, (2,))
    wrong = replace(
        release_config(tmp_path / "fresh"),
        expected_toolkit_source_sha256="f" * 64,
    )
    with pytest.raises(UniversalReleaseError, match="toolkit source"):
        export_universal_release(runs, wrong)

    wrong_generator = replace(
        release_config(tmp_path / "fresh", "generator"),
        expected_generator_sha256="f" * 64,
    )
    with pytest.raises(UniversalReleaseError, match="geng SHA-256"):
        export_universal_release(runs, wrong_generator)


def test_export_rejects_invalid_output_state_and_duplicate_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    config = release_config(tmp_path)
    with pytest.raises(ValueError, match="UniversalReleaseConfig"):
        export_universal_release(runs, object())  # type: ignore[arg-type]
    with pytest.raises(UniversalReleaseError, match="at least one"):
        export_universal_release((), config)

    config.bundle_root.mkdir()
    with pytest.raises(UniversalReleaseError, match="overwrite bundle"):
        export_universal_release(runs, config)
    config.bundle_root.rmdir()
    config.archive_path.write_bytes(b"existing")
    with pytest.raises(UniversalReleaseError, match="overwrite archive"):
        export_universal_release(runs, config)
    config.archive_path.unlink()

    missing_parent = replace(
        config,
        bundle_root=tmp_path / "missing" / "bundle",
        archive_path=tmp_path / "archive.tar.gz",
    )
    with pytest.raises(UniversalReleaseError, match="bundle output parent"):
        export_universal_release(runs, missing_parent)
    with monkeypatch.context() as bounded:
        bounded.setattr(release, "_MAX_RELEASE_RUNS", 0)
        with pytest.raises(UniversalReleaseError, match="at most 256"):
            export_universal_release(runs, config)
    with pytest.raises(UniversalReleaseError, match="duplicate completed run"):
        export_universal_release((runs[0], runs[0]), config)


def test_export_rejects_order_zero_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = make_runs(tmp_path, monkeypatch, (0,))
    monkeypatch.setattr(
        release,
        "_write_deterministic_archive",
        lambda *_args, **_kwargs: pytest.fail("archive layout must not run for order zero"),
    )
    with pytest.raises(UniversalReleaseError, match="orders must be positive"):
        export_universal_release(runs, release_config(tmp_path))


def test_export_rejects_mixed_toolkit_generator_and_operational_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch)
    first = tmp_path / "first"
    second = tmp_path / "second"
    run_universal_census(UniversalCensusConfig(GengSpec(2)), first, toolkit_identity=TEST_TOOLKIT)
    run_universal_census(
        UniversalCensusConfig(GengSpec(3), checkpoint_interval=2),
        second,
        toolkit_identity=TEST_TOOLKIT,
    )
    with pytest.raises(UniversalReleaseError, match="identical non-order config"):
        export_universal_release((first, second), release_config(tmp_path))

    second = tmp_path / "second-toolkit"
    run_universal_census(
        UniversalCensusConfig(GengSpec(3)),
        second,
        toolkit_identity=replace(TEST_TOOLKIT, source_sha256="d" * 64),
    )
    with pytest.raises(UniversalReleaseError, match="toolkit identity"):
        export_universal_release((first, second), release_config(tmp_path, "mixed-toolkit"))

    def order_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
        del executable
        digest = "a" * 64 if spec.order == 2 else "e" * 64
        return GengIdentity("geng", digest, spec.arguments())

    monkeypatch.setattr(universal, "geng_identity", order_identity)
    generator_first = tmp_path / "generator-first"
    generator_second = tmp_path / "generator-second"
    run_universal_census(
        UniversalCensusConfig(GengSpec(2)), generator_first, toolkit_identity=TEST_TOOLKIT
    )
    run_universal_census(
        UniversalCensusConfig(GengSpec(3)), generator_second, toolkit_identity=TEST_TOOLKIT
    )
    with pytest.raises(UniversalReleaseError, match="same geng executable"):
        export_universal_release(
            (generator_first, generator_second),
            release_config(tmp_path, "mixed-generator"),
        )


def test_export_requires_unrestricted_unsharded_uniform_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch)
    restricted = tmp_path / "restricted"
    run_universal_census(
        UniversalCensusConfig(GengSpec(3, connected=True)),
        restricted,
        toolkit_identity=TEST_TOOLKIT,
    )
    with pytest.raises(UniversalReleaseError, match="unrestricted, unsharded"):
        export_universal_release((restricted,), release_config(tmp_path))

    wrong_checks = tmp_path / "wrong-checks"
    run_universal_census(
        UniversalCensusConfig(GengSpec(3), checks=(UniversalCheckSpec(SolverBackend.DSATUR, 1),)),
        wrong_checks,
        toolkit_identity=TEST_TOOLKIT,
    )
    with pytest.raises(UniversalReleaseError, match="check matrix"):
        export_universal_release((wrong_checks,), release_config(tmp_path, "wrong-checks"))

    unfiltered = tmp_path / "unfiltered"
    run_universal_census(
        UniversalCensusConfig(GengSpec(3), require_high_degree=False),
        unfiltered,
        toolkit_identity=TEST_TOOLKIT,
    )
    with pytest.raises(UniversalReleaseError, match="high-degree filter"):
        export_universal_release((unfiltered,), release_config(tmp_path, "unfiltered"))


def test_export_rejects_adverse_status_before_writing_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_generator(monkeypatch)
    real_solve = solve_with_backend

    def unknown(problem: object, **kwargs: object) -> SolveResult:
        solved = real_solve(problem, **kwargs)  # type: ignore[arg-type]
        return SolveResult(
            SolveStatus.UNKNOWN,
            solved.problem_digest,
            None,
            solved.stats,
            "synthetic resource limit",
        )

    monkeypatch.setattr(universal, "solve_with_backend", unknown)
    run = tmp_path / "adverse-run"
    run_universal_census(UniversalCensusConfig(GengSpec(2)), run, toolkit_identity=TEST_TOOLKIT)
    config = release_config(tmp_path)
    with pytest.raises(UniversalReleaseError, match="zero adverse"):
        export_universal_release((run,), config)
    assert not config.bundle_root.exists()
    assert not config.archive_path.exists()


def test_archive_validator_rejects_tampering_extra_members_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    result = export_universal_release(runs, release_config(tmp_path))
    summary = _summary(result.summary_path)
    receipts = [
        ArchiveMemberReceipt(PurePosixPath(member["path"]), member["bytes"], member["sha256"])
        for member in summary["runs"][0]["members"].values()  # type: ignore[index]
    ]
    validate_replay_archive(result.archive_path, summary)

    tampered = tmp_path / "tampered.tar.gz"
    tampered.write_bytes(result.archive_path.read_bytes())
    payload = bytearray(tampered.read_bytes())
    payload[len(payload) // 2] ^= 1
    tampered.write_bytes(payload)
    with pytest.raises(UniversalReleaseError):
        release._validate_replay_archive_structure(tampered, receipts)

    missing = copy.deepcopy(receipts)
    missing.pop()
    with pytest.raises(UniversalReleaseError, match=r"USTAR|undeclared member"):
        release._validate_replay_archive_structure(result.archive_path, missing)


def _write_archive_fixture(
    path: Path,
    *,
    data: bytes = b"payload\n",
    mode: int = 0o644,
    mtime: int = 0,
    member_type: bytes = tarfile.REGTYPE,
    archive_format: int = tarfile.USTAR_FORMAT,
) -> ArchiveMemberReceipt:
    name = "order-02/records.jsonl"
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w|", format=archive_format) as archive,
    ):
        info = tarfile.TarInfo(name)
        info.size = len(data) if member_type == tarfile.REGTYPE else 0
        info.mode = mode
        info.mtime = mtime
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.type = member_type
        if member_type != tarfile.REGTYPE:
            info.linkname = "target"
            archive.addfile(info)
        else:
            archive.addfile(info, io.BytesIO(data))
    return ArchiveMemberReceipt(PurePosixPath(name), len(data), hashlib.sha256(data).hexdigest())


def _rewrite_archive_members(
    source: Path, destination: Path, replacements: dict[str, bytes]
) -> dict[str, ArchiveMemberReceipt]:
    members: list[tuple[str, bytes]] = []
    with tarfile.open(source, mode="r:gz") as archive:
        for member in archive:
            extracted = archive.extractfile(member)
            assert extracted is not None
            with extracted:
                data = extracted.read()
            members.append((member.name, replacements.get(member.name, data)))
    receipts: dict[str, ArchiveMemberReceipt] = {}
    with (
        destination.open("xb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w|", format=tarfile.USTAR_FORMAT) as archive,
    ):
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
            receipts[name] = ArchiveMemberReceipt(
                PurePosixPath(name), len(data), hashlib.sha256(data).hexdigest()
            )
    return receipts


def _bind_summary_to_archive(
    summary: dict[str, object], archive: Path, receipts: dict[str, ArchiveMemberReceipt]
) -> None:
    runs = summary["runs"]
    assert isinstance(runs, list)
    for run in runs:
        assert isinstance(run, dict)
        members = run["members"]
        assert isinstance(members, dict)
        for descriptor in members.values():
            assert isinstance(descriptor, dict)
            receipt = receipts[descriptor["path"]]
            descriptor.update(bytes=receipt.bytes, sha256=receipt.sha256)
    replay = summary["replay_archive"]
    assert isinstance(replay, dict)
    replay.update(
        bytes=archive.stat().st_size,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )


def test_semantic_archive_and_publisher_reject_forged_canonical_run_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    result = export_universal_release(runs, release_config(tmp_path))
    summary = _summary(result.summary_path)
    forged = tmp_path / "forged-canonical.tar.gz"
    receipts = _rewrite_archive_members(
        result.archive_path,
        forged,
        {
            "order-02/completion.json": b"{}\n",
            "order-02/manifest.json": b"{}\n",
            "order-02/records.jsonl": b"{}\n",
        },
    )
    _bind_summary_to_archive(summary, forged, receipts)
    monkeypatch.setattr(
        universal,
        "stream_geng",
        lambda *_args, **_kwargs: pytest.fail("semantic archive validation must not run geng"),
    )

    with pytest.raises(UniversalReleaseError, match="semantic validation"):
        validate_replay_archive(forged, summary)

    _refresh_bundle_summary(result.bundle_root, result.summary_path, summary, sync_external=True)
    with pytest.raises(BundleVerificationError, match="semantic validation"):
        plan_promotion(
            PublicationConfig(
                source_root=result.bundle_root,
                destination_root=_destination(tmp_path / "forged-destination"),
                expected_code_commit=CODE_COMMIT,
                external_files=(ExternalArtifactFile(ARCHIVE_NAME, forged),),
            )
        )


@pytest.mark.parametrize("sequence", [(2, 3, 2), (0, 1, 2)])
def test_exact_geng_replay_rejects_nonadjacent_duplicate_and_truncated_prefix(
    tmp_path: Path,
    sequence: tuple[int, ...],
) -> None:
    executable = "/usr/bin/geng"
    run_directory = tmp_path / "real-order-four"
    run_universal_census(
        UniversalCensusConfig(GengSpec(4)),
        run_directory,
        executable=executable,
        toolkit_identity=TEST_TOOLKIT,
    )
    config = replace(
        release_config(tmp_path),
        expected_generator_sha256=None,
    )
    exported = export_universal_release((run_directory,), config, executable=executable)
    with tarfile.open(exported.archive_path, mode="r:gz") as archive:
        member_payloads = {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive
        }
    record_path = "order-04/records.jsonl"
    original_rows = [
        json.loads(line) for line in member_payloads[record_path].decode("utf-8").splitlines()
    ]
    selected_rows: list[dict[str, object]] = []
    for index, original_index in enumerate(sequence):
        row = copy.deepcopy(original_rows[original_index])
        row["index"] = index
        selected_rows.append(row)
    records_bytes = b"".join(canonical_json_bytes(row) + b"\n" for row in selected_rows)

    manifest_path = "order-04/manifest.json"
    completion_path = "order-04/completion.json"
    manifest = json.loads(member_payloads[manifest_path])
    partition_count = sum(cast(int, row["partition_count"]) for row in selected_rows)
    counts: dict[str, int] = {
        "candidate_unsat": 0,
        "error": 0,
        "skipped": 0,
        "unknown": 0,
        "verified_all": 0,
    }
    for row in selected_rows:
        counts[cast(str, row["status"])] += 1
    records_sha256 = hashlib.sha256(records_bytes).hexdigest()
    manifest.update(
        counts=counts,
        partition_count=partition_count,
        record_count=len(selected_rows),
    )
    manifest["artifacts"].update(records_bytes=len(records_bytes), records_sha256=records_sha256)
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    completion = json.loads(member_payloads[completion_path])
    completion.update(
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        record_count=len(selected_rows),
        records_sha256=records_sha256,
    )
    completion_bytes = canonical_json_bytes(completion) + b"\n"

    forged = tmp_path / f"forged-{sequence[-1]}.tar.gz"
    receipts = _rewrite_archive_members(
        exported.archive_path,
        forged,
        {
            record_path: records_bytes,
            manifest_path: manifest_bytes,
            completion_path: completion_bytes,
        },
    )
    summary = _summary(exported.summary_path)
    run = summary["runs"][0]  # type: ignore[index]
    run.update(
        counts=counts,
        partition_count=partition_count,
        record_count=len(selected_rows),
        check_evaluations=partition_count * 3,
    )
    totals = summary["totals"]
    assert isinstance(totals, dict)
    totals.update(
        counts=counts,
        partition_count=partition_count,
        record_count=len(selected_rows),
        check_evaluations=partition_count * 3,
    )
    _bind_summary_to_archive(summary, forged, receipts)

    with pytest.raises(UniversalReleaseError, match=r"disagrees|extra graph"):
        validate_replay_archive(forged, summary, executable=executable)

    _refresh_bundle_summary(
        exported.bundle_root, exported.summary_path, summary, sync_external=True
    )
    with pytest.raises(BundleVerificationError, match=r"disagrees|extra graph"):
        plan_promotion(
            PublicationConfig(
                source_root=exported.bundle_root,
                destination_root=_destination(tmp_path / "exact-replay-destination"),
                expected_code_commit=CODE_COMMIT,
                geng_executable=executable,
                external_files=(ExternalArtifactFile(ARCHIVE_NAME, forged),),
            )
        )


def test_public_archive_validation_requires_generator_and_rejects_order_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    exported = export_universal_release(runs, release_config(tmp_path))
    summary = _summary(exported.summary_path)
    with pytest.raises(ValueError, match="nonempty command"):
        validate_replay_archive(exported.archive_path, summary, executable=None)  # type: ignore[arg-type]

    summary["runs"][0]["order"] = 17  # type: ignore[index]
    monkeypatch.setattr(
        release,
        "_open_regular_nofollow",
        lambda *_args, **_kwargs: pytest.fail("order bound must precede archive I/O"),
    )
    with pytest.raises(UniversalReleaseError, match="contract is malformed"):
        validate_replay_archive(exported.archive_path, summary)


def test_archive_validator_rejects_headers_receipts_and_nondeterministic_metadata(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain.gz"
    plain.write_bytes(b"not gzip")
    with pytest.raises(UniversalReleaseError, match="canonical gzip"):
        release._validate_replay_archive_structure(plain, ())

    canonical = tmp_path / "canonical.tar.gz"
    receipt = _write_archive_fixture(canonical)
    with pytest.raises(UniversalReleaseError, match="unique"):
        release._validate_replay_archive_structure(canonical, (receipt, receipt))
    with pytest.raises(UniversalReleaseError, match="canonical USTAR"):
        release._validate_replay_archive_structure(canonical, (replace(receipt, bytes=999),))
    with pytest.raises(UniversalReleaseError, match="SHA-256"):
        release._validate_replay_archive_structure(canonical, (replace(receipt, sha256="0" * 64),))

    bad_mode = tmp_path / "bad-mode.tar.gz"
    _write_archive_fixture(bad_mode, mode=0o600)
    with pytest.raises(UniversalReleaseError, match="canonical USTAR"):
        release._validate_replay_archive_structure(bad_mode, (receipt,))
    bad_mtime = tmp_path / "bad-mtime.tar.gz"
    _write_archive_fixture(bad_mtime, mtime=1)
    with pytest.raises(UniversalReleaseError, match="canonical USTAR"):
        release._validate_replay_archive_structure(bad_mtime, (receipt,))
    symlink = tmp_path / "symlink.tar.gz"
    _write_archive_fixture(symlink, member_type=tarfile.SYMTYPE)
    with pytest.raises(UniversalReleaseError, match="canonical USTAR"):
        release._validate_replay_archive_structure(symlink, (receipt,))

    bad_gzip_mtime = bytearray(canonical.read_bytes())
    bad_gzip_mtime[4:8] = (1).to_bytes(4, "little")
    changed_header = tmp_path / "gzip-mtime.tar.gz"
    changed_header.write_bytes(bad_gzip_mtime)
    with pytest.raises(UniversalReleaseError, match="canonical gzip"):
        release._validate_replay_archive_structure(changed_header, (receipt,))

    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(canonical.read_bytes()[:20])
    with pytest.raises(UniversalReleaseError, match="truncated"):
        release._validate_replay_archive_structure(truncated, (receipt,))


def _canonical_gzip(data: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0) as zipped:
        zipped.write(data)
    return output.getvalue()


def test_archive_validator_rejects_gzip_trailer_members_and_header_drift(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical.tar.gz"
    receipt = _write_archive_fixture(canonical)
    original = canonical.read_bytes()
    variants: list[tuple[str, bytes, str]] = []
    variants.append(("missing-trailer", original[:-8], "truncated"))
    bad_crc = bytearray(original)
    bad_crc[-8] ^= 1
    variants.append(("bad-crc", bytes(bad_crc), "trailer or stream"))
    bad_size = bytearray(original)
    bad_size[-1] ^= 1
    variants.append(("bad-size", bytes(bad_size), "trailer or stream"))
    variants.append(("raw-trailer", original + b"trailing", "one gzip member"))
    variants.append(("second-member", original + _canonical_gzip(b"second"), "one gzip member"))
    bad_xfl = bytearray(original)
    bad_xfl[8] = 0
    variants.append(("bad-xfl", bytes(bad_xfl), "canonical gzip"))
    bad_os = bytearray(original)
    bad_os[9] = 3
    variants.append(("bad-os", bytes(bad_os), "canonical gzip"))

    for name, data, message in variants:
        candidate = tmp_path / f"{name}.tar.gz"
        candidate.write_bytes(data)
        with pytest.raises(UniversalReleaseError, match=message):
            release._validate_replay_archive_structure(candidate, (receipt,))


def test_archive_validator_rejects_noncanonical_tar_and_padding(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical.tar.gz"
    receipt = _write_archive_fixture(canonical)
    uncompressed = gzip.decompress(canonical.read_bytes())

    bad_member_padding = bytearray(uncompressed)
    bad_member_padding[512 + len(b"payload\n")] = 1
    bad_padding = tmp_path / "bad-padding.tar.gz"
    bad_padding.write_bytes(_canonical_gzip(bytes(bad_member_padding)))
    with pytest.raises(UniversalReleaseError, match="padding"):
        release._validate_replay_archive_structure(bad_padding, (receipt,))

    bad_terminal = bytearray(uncompressed)
    bad_terminal[-1] = 1
    nonzero_terminal = tmp_path / "bad-terminal.tar.gz"
    nonzero_terminal.write_bytes(_canonical_gzip(bytes(bad_terminal)))
    with pytest.raises(UniversalReleaseError, match="padding"):
        release._validate_replay_archive_structure(nonzero_terminal, (receipt,))

    short_terminal = tmp_path / "short-terminal.tar.gz"
    short_terminal.write_bytes(_canonical_gzip(uncompressed[:-512]))
    with pytest.raises(UniversalReleaseError, match="terminal length"):
        release._validate_replay_archive_structure(short_terminal, (receipt,))

    gnu = tmp_path / "gnu.tar.gz"
    _write_archive_fixture(gnu, archive_format=tarfile.GNU_FORMAT)
    with pytest.raises(UniversalReleaseError, match="canonical USTAR"):
        release._validate_replay_archive_structure(gnu, (receipt,))


def test_archive_validator_bounds_metadata_and_jsonl_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical.tar.gz"
    _write_archive_fixture(canonical)
    oversized_metadata = ArchiveMemberReceipt(
        PurePosixPath("order-02/manifest.json"),
        4 * 1024 * 1024 + 1,
        "0" * 64,
    )
    with pytest.raises(UniversalReleaseError, match="4 MiB"):
        release._validate_replay_archive_structure(canonical, (oversized_metadata,))

    monkeypatch.setattr(release, "MAX_UNIVERSAL_RECORD_BYTES", 32)
    long_line = tmp_path / "long-line.tar.gz"
    long_receipt = _write_archive_fixture(long_line, data=b"x" * 32 + b"\n")
    with pytest.raises(UniversalReleaseError, match="16 MiB"):
        release._validate_replay_archive_structure(long_line, (long_receipt,))

    missing_lf = tmp_path / "missing-lf.tar.gz"
    missing_lf_receipt = _write_archive_fixture(missing_lf, data=b"payload")
    with pytest.raises(UniversalReleaseError, match="end with LF"):
        release._validate_replay_archive_structure(missing_lf, (missing_lf_receipt,))

    with monkeypatch.context() as bounded:
        bounded.setattr(release, "_MAX_REPLAY_UNCOMPRESSED_BYTES", 1_024)
        with pytest.raises(UniversalReleaseError, match="16 GiB"):
            release._validate_replay_archive_structure(canonical, ())


def test_export_is_non_overwriting_and_schema_pins_are_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    config = release_config(tmp_path)
    result = export_universal_release(runs, config)
    before = _bundle_files(result.bundle_root)
    with pytest.raises(UniversalReleaseError, match="overwrite bundle"):
        export_universal_release(runs, config)
    assert _bundle_files(result.bundle_root) == before
    assert release_schema_digests() == {
        "schemas/dataset-manifest-v2.schema.json": (
            "60351bf5daeda4d119678896cbe2a5771d451aaf279c4ae12f9f99dfd4c657fd"
        ),
        "schemas/universal-census-summary-v1.schema.json": (
            "0a32e047fa967f9d4bc87c2ee433d9e8af9095864920b37791b1aef171d675fd"
        ),
    }


def test_export_preserves_archive_created_at_final_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    config = release_config(tmp_path)
    real_rename = release._rename_noreplace
    injected = False

    def create_foreign_then_rename(
        *,
        source_directory_descriptor: int,
        source_name: str,
        destination_directory_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal injected
        if destination_name == config.archive_path.name and not injected:
            injected = True
            config.archive_path.write_bytes(b"foreign archive\n")
        real_rename(
            source_directory_descriptor=source_directory_descriptor,
            source_name=source_name,
            destination_directory_descriptor=destination_directory_descriptor,
            destination_name=destination_name,
        )

    monkeypatch.setattr(release, "_rename_noreplace", create_foreign_then_rename)
    with pytest.raises(UniversalReleaseError, match="destination already exists"):
        export_universal_release(runs, config)

    assert injected
    assert config.archive_path.read_bytes() == b"foreign archive\n"
    assert not config.bundle_root.exists()


def test_export_preserves_bundle_created_after_archive_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    config = release_config(tmp_path)
    real_rename = release._rename_noreplace
    calls = 0

    def create_bundle_before_second_install(
        *,
        source_directory_descriptor: int,
        source_name: str,
        destination_directory_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            config.bundle_root.mkdir()
            (config.bundle_root / "foreign.txt").write_text("keep\n", encoding="utf-8")
        real_rename(
            source_directory_descriptor=source_directory_descriptor,
            source_name=source_name,
            destination_directory_descriptor=destination_directory_descriptor,
            destination_name=destination_name,
        )

    monkeypatch.setattr(release, "_rename_noreplace", create_bundle_before_second_install)
    with pytest.raises(UniversalReleaseError, match="destination already exists"):
        export_universal_release(runs, config)

    assert calls >= 2
    assert (config.bundle_root / "foreign.txt").read_text(encoding="utf-8") == "keep\n"
    assert not config.archive_path.exists()


def test_export_rejects_normalized_output_alias_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nested").mkdir()
    config = replace(
        release_config(tmp_path),
        bundle_root=tmp_path / "same-output.tar.gz",
        archive_path=tmp_path / "nested" / ".." / "same-output.tar.gz",
    )
    monkeypatch.setattr(
        release,
        "_private_run_snapshots",
        lambda *_args, **_kwargs: pytest.fail("alias rejection must precede snapshotting"),
    )
    with pytest.raises(UniversalReleaseError, match="alias one destination"):
        export_universal_release((tmp_path / "unused-run",), config)


def test_export_rollback_preserves_foreign_archive_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch, (2,))
    config = release_config(tmp_path)
    real_install = release._install_output_noreplace
    calls = 0

    def replace_archive_then_fail_bundle(
        target: Any,
        *,
        staging_name: str,
        expected: Any,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "forced bundle install failure")
        real_install(target, staging_name=staging_name, expected=expected)
        if calls == 1:
            config.archive_path.unlink()
            config.archive_path.write_bytes(b"foreign replacement\n")

    monkeypatch.setattr(release, "_install_output_noreplace", replace_archive_then_fail_bundle)
    with pytest.raises(UniversalReleaseError, match="foreign final replacement preserved"):
        export_universal_release(runs, config)

    assert config.archive_path.read_bytes() == b"foreign replacement\n"
    assert not config.bundle_root.exists()


def test_v2_publisher_requires_and_validates_exact_external_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch)
    result = export_universal_release(runs, release_config(tmp_path))
    destination = _destination(tmp_path / "destination")
    config = PublicationConfig(
        source_root=result.bundle_root,
        destination_root=destination,
        expected_code_commit=CODE_COMMIT,
        external_files=(ExternalArtifactFile(ARCHIVE_NAME, result.archive_path),),
    )

    plan = plan_promotion(config)
    assert result.summary_path.relative_to(result.bundle_root).as_posix() in {
        path.as_posix() for path in plan.changed_paths
    }
    assert "schemas/dataset-manifest-v2.schema.json" in {
        path.as_posix() for path in plan.changed_paths
    }

    with pytest.raises(BundleVerificationError, match="exactly match"):
        plan_promotion(
            PublicationConfig(
                source_root=result.bundle_root,
                destination_root=destination,
                expected_code_commit=CODE_COMMIT,
            )
        )

    result.archive_path.write_bytes(result.archive_path.read_bytes() + b"tamper")
    with pytest.raises(BundleVerificationError, match="byte count mismatch"):
        plan_promotion(config)


def test_v2_publisher_rejects_forged_verified_claim_even_with_refreshed_local_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch)
    result = export_universal_release(runs, release_config(tmp_path))
    destination = _destination(tmp_path / "destination")
    summary = _summary(result.summary_path)
    summary["runs"][0]["counts"]["candidate_unsat"] = 1  # type: ignore[index]
    summary["runs"][0]["counts"]["verified_all"] -= 1  # type: ignore[index]
    summary["totals"]["counts"]["candidate_unsat"] = 1  # type: ignore[index]
    summary["totals"]["counts"]["verified_all"] -= 1  # type: ignore[index]
    _refresh_bundle_summary(result.bundle_root, result.summary_path, summary)

    with pytest.raises(BundleVerificationError, match="adverse status"):
        plan_promotion(
            PublicationConfig(
                source_root=result.bundle_root,
                destination_root=destination,
                expected_code_commit=CODE_COMMIT,
                external_files=(ExternalArtifactFile(ARCHIVE_NAME, result.archive_path),),
            )
        )


def test_universal_summary_semantic_guard_matrix_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = make_runs(tmp_path, monkeypatch)
    result = export_universal_release(runs, release_config(tmp_path))
    pristine: Any = _summary(result.summary_path)
    replay: Any = pristine["replay_archive"]
    external = {
        replay["external_artifact"]: {
            field: replay[field] for field in ("url", "media_type", "bytes", "sha256")
        }
    }
    arguments = {
        "expected_repository": pristine["producer"]["repository"],
        "expected_commit": pristine["producer"]["commit"],
        "release_created_utc": pristine["created_utc"],
        "external_artifacts": external,
    }
    receipts = publishing._validate_universal_summary_semantics(pristine, "summary", **arguments)
    assert len(receipts) == 3 * len(pristine["runs"])

    with pytest.raises(BundleVerificationError, match="must be an object"):
        publishing._validate_universal_summary_semantics([], "summary", **arguments)

    def duplicate_order(summary: Any) -> None:
        first_order = summary["runs"][0]["order"]
        duplicate = summary["runs"][1]
        duplicate["order"] = first_order
        duplicate["generator_arguments"] = ["-q", str(first_order)]
        for member in duplicate["members"].values():
            member["path"] = member["path"].replace("order-03", "order-02")

    def adverse_status(summary: Any) -> None:
        counts = summary["runs"][0]["counts"]
        counts["verified_all"] -= 1
        counts["candidate_unsat"] += 1
        totals = summary["totals"]["counts"]
        totals["verified_all"] -= 1
        totals["candidate_unsat"] += 1

    def vacuous_claim(summary: Any) -> None:
        for run in summary["runs"]:
            counts = run["counts"]
            counts["skipped"] += counts["verified_all"]
            counts["verified_all"] = 0
        totals = summary["totals"]["counts"]
        totals["skipped"] += totals["verified_all"]
        totals["verified_all"] = 0

    mutations: list[tuple[Callable[[Any], object], str]] = [
        (lambda value: value.update(producer=[]), "producer: expected object"),
        (
            lambda value: value["producer"].update(repository="https://example.org/wrong"),
            "producer.repository",
        ),
        (lambda value: value["producer"].update(commit="f" * 40), "producer.commit"),
        (lambda value: value.update(created_utc="2026-01-01T00:00:00Z"), "created_utc"),
        (lambda value: value.update(replay_archive=[]), "invalid external binding"),
        (
            lambda value: value["replay_archive"].update(external_artifact="missing"),
            "not externally declared",
        ),
        (
            lambda value: value["replay_archive"].update(url="https://example.org/wrong"),
            "archive url",
        ),
        (lambda value: value.update(checks=[]), "checks: expected nonempty"),
        (lambda value: value.update(checks=[None]), r"checks\[0\]: invalid"),
        (lambda value: value["checks"].reverse(), "IDs must be unique and sorted"),
        (
            lambda value: value["checks"][0].update(description="wrong"),
            "missing required",
        ),
        (lambda value: value.update(runs=[]), "runs: expected nonempty"),
        (lambda value: value.update(runs=[None]), r"runs\[0\]: expected object"),
        (lambda value: value["runs"][0].update(order=17), "between 1 and 16"),
        (
            lambda value: value["runs"][0].update(generator_arguments=["-q", "99"]),
            "generator arguments",
        ),
        (lambda value: value["runs"][0].update(shard_count=2), "unsharded"),
        (lambda value: value["runs"][0].update(counts={}), "status-count object"),
        (
            lambda value: value["runs"][0]["counts"].update(verified_all=True),
            "expected nonnegative integer",
        ),
        (lambda value: value["runs"][0].update(record_count=-1), "invalid run count"),
        (
            lambda value: value["runs"][0]["counts"].update(verified_all=0),
            "statuses do not sum",
        ),
        (
            lambda value: value["runs"][0].update(check_evaluations=1),
            "check-evaluation count",
        ),
        (lambda value: value["runs"][0].update(members=[]), "members: expected object"),
        (
            lambda value: value["runs"][0]["members"].update(manifest=[]),
            "members.manifest: expected object",
        ),
        (
            lambda value: value["runs"][0]["members"]["manifest"].update(
                path="wrong/manifest.json"
            ),
            "noncanonical manifest",
        ),
        (
            lambda value: value["runs"][0]["members"]["records"].update(bytes=-1),
            "invalid records receipt",
        ),
        (duplicate_order, "orders must be unique and sorted"),
        (
            lambda value: value["runs"][1].update(
                run_fingerprint=value["runs"][0]["run_fingerprint"]
            ),
            "fingerprints must be unique",
        ),
        (lambda value: value["totals"].update(record_count=99), "sum over runs"),
        (lambda value: value.update(limitations=[]), "noncanonical limitations"),
        (lambda value: value.update(claims=[]), "exactly one finite claim"),
        (lambda value: value.update(claims=[None]), "invalid claim"),
        (
            lambda value: value["claims"][0].update(claim_type="wrong"),
            "finite_bound claim",
        ),
        (
            lambda value: value["claims"][0].update(status="wrong"),
            "verified finite status",
        ),
        (lambda value: value["claims"][0].update(orders=[]), "supporting orders"),
        (
            lambda value: value["claims"][0].update(required_checks=[]),
            "required checks",
        ),
        (
            lambda value: value["claims"][0].update(finite_scope="wrong"),
            "finite scope",
        ),
        (
            lambda value: value["claims"][0].update(limitations=[]),
            "noncanonical limitations",
        ),
        (adverse_status, "adverse status forbids verified"),
        (vacuous_claim, "verified claim is vacuous"),
    ]
    for mutation, message in mutations:
        candidate = copy.deepcopy(pristine)
        mutation(candidate)
        with pytest.raises(BundleVerificationError, match=message):
            publishing._validate_universal_summary_semantics(
                candidate,
                "summary",
                **arguments,
            )


def _claim_adverse(summary: dict[str, object]) -> None:
    summary["runs"][0]["counts"]["candidate_unsat"] = 1  # type: ignore[index]
    summary["runs"][0]["counts"]["verified_all"] -= 1  # type: ignore[index]
    summary["totals"]["counts"]["candidate_unsat"] = 1  # type: ignore[index]
    summary["totals"]["counts"]["verified_all"] -= 1  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["producer"].update(repository="https://example.org/x"),
            "producer.repository",
        ),
        (lambda value: value["producer"].update(commit="d" * 40), "producer.commit"),
        (lambda value: value.update(created_utc="2026-07-14T12:00:01Z"), "created_utc"),
        (lambda value: value["replay_archive"].update(sha256="0" * 64), "replay archive sha256"),
        (lambda value: value["checks"].reverse(), "IDs must be unique and sorted"),
        (
            lambda value: value["checks"][0].update(palette_offset=9),
            "missing required",
        ),
        (
            lambda value: value["checks"][0].update(
                description="This check proves an unbounded theorem for all graphs."
            ),
            "missing required",
        ),
        (
            lambda value: value["runs"][0].update(generator_arguments=["-q", "-c", "2"]),
            "maximum item count",
        ),
        (lambda value: value["runs"][0].update(order=0), "minimum is 1"),
        (lambda value: value["runs"][0].update(order=17), "maximum is 16"),
        (lambda value: value["runs"][0].update(order=True), "expected integer"),
        (lambda value: value["runs"][0].update(order=2.0), "expected integer"),
        (lambda value: value["runs"][0].update(shard_index=1), "expected constant 0"),
        (lambda value: value["runs"][0]["counts"].update(skipped=1), "sum to records"),
        (lambda value: value["runs"][0].update(check_evaluations=2), "check-evaluation"),
        (
            lambda value: value["runs"][0]["members"]["manifest"].update(
                path="other/manifest.json"
            ),
            "noncanonical manifest",
        ),
        (lambda value: value["totals"].update(record_count=99), "sum over runs"),
        (
            lambda value: value["claims"][0].update(required_checks=["dsatur-delta-plus-2"]),
            "expected constant",
        ),
        (
            lambda value: value["claims"].append(copy.deepcopy(value["claims"][0])),
            "maximum item count",
        ),
        (lambda value: value["claims"][0].update(orders=[2]), "supporting orders"),
        (lambda value: value["claims"][0].update(claim_type="backend_agreement"), "finite_bound"),
        (
            lambda value: value["claims"][0].update(status="inconclusive_in_finite_scope"),
            "expected constant",
        ),
        (lambda value: value["claims"][0].update(finite_scope="drift"), "finite scope"),
        (lambda value: value["claims"][0].update(limitations=["drift"]), "limitations"),
        (lambda value: value.update(limitations=["drift"]), "limitations"),
        (_claim_adverse, "adverse status"),
    ],
)
def test_v2_publisher_rejects_cross_field_summary_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[dict[str, object]], object],
    message: str,
) -> None:
    runs = make_runs(tmp_path, monkeypatch)
    result = export_universal_release(runs, release_config(tmp_path))
    summary = _summary(result.summary_path)
    mutation(summary)
    _refresh_bundle_summary(result.bundle_root, result.summary_path, summary)
    destination = _destination(tmp_path / "destination")
    monkeypatch.setattr(
        publishing,
        "validate_replay_archive",
        lambda *_args, **_kwargs: pytest.fail(
            "archive layout must not run before invalid summary metadata is rejected"
        ),
    )
    with pytest.raises(BundleVerificationError, match=message):
        plan_promotion(
            PublicationConfig(
                source_root=result.bundle_root,
                destination_root=destination,
                expected_code_commit=CODE_COMMIT,
                external_files=(ExternalArtifactFile(ARCHIVE_NAME, result.archive_path),),
            )
        )
