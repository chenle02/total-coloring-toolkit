from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from total_coloring.instrument import (
    Harness,
    InstrumentationError,
    ProducerSpec,
    assert_producers_sealed,
)


def test_sealed_fixture_accepts_and_rejects_degenerate_producers() -> None:
    module = SimpleNamespace(
        count_special_cells=lambda: 3,
        partition_profile=lambda: (2, 1),
        binding_total=lambda: 5,
    )

    def run() -> Harness:
        harness = Harness()
        gross = harness.producer("count_special_cells", module.count_special_cells())
        profile = harness.producer("partition_profile", module.partition_profile())
        binding = harness.producer("binding_total", module.binding_total())

        harness.headline("gross_special_cells", gross)
        harness.headline("partition_profile", profile)
        harness.headline("binding_total", binding)
        harness.require_headline("gross_special_cells", gross == 3, "gross count drifted")
        harness.require_headline("partition_profile", profile == (2, 1), "profile drifted")
        harness.require_headline("binding_total", binding == 5, "binding count drifted")
        harness.exercised("tested_partitions", 4)
        harness.finalize()
        return harness

    run()
    assert_producers_sealed(
        module,
        ("count_special_cells", "partition_profile", "binding_total"),
        run,
    )


def test_finalize_rejects_unasserted_headline() -> None:
    harness = Harness()
    count = harness.producer("count_special_cells", 3)

    harness.headline("gross_special_cells", count)
    harness.exercised("tested_partitions", 1)

    with pytest.raises(InstrumentationError, match="unasserted headline"):
        harness.finalize()


def test_finalize_rejects_zero_exercised_count() -> None:
    harness = Harness()
    count = harness.producer("count_special_cells", 3)

    harness.headline("gross_special_cells", count)
    harness.require_headline("gross_special_cells", count == 3, "gross count drifted")
    harness.exercised("tested_partitions", 0)

    with pytest.raises(InstrumentationError, match="exercised count"):
        harness.finalize()


def test_producer_tamper_rejects_green_degenerate_run() -> None:
    module = SimpleNamespace(count_special_cells=lambda: 7)

    def run() -> Harness:
        harness = Harness()
        count = harness.producer("count_special_cells", module.count_special_cells())
        harness.headline("target_labelled_special_cells", count)
        harness.require_headline(
            "target_labelled_special_cells",
            count >= 0,
            "headline should stay nonnegative",
        )
        harness.exercised("tested_cells", 1)
        harness.finalize()
        return harness

    run()
    with pytest.raises(InstrumentationError, match="unsealed producer 'count_special_cells'"):
        assert_producers_sealed(module, ("count_special_cells",), run)


def test_producer_tamper_rejects_missing_binding_producer() -> None:
    module = SimpleNamespace(
        gross_count=lambda: 3,
        profile_count=lambda: (2, 1),
        binding_count=lambda: 5,
    )

    def run() -> Harness:
        harness = Harness()
        gross = harness.producer("gross_count", module.gross_count())
        profile = harness.producer("profile_count", module.profile_count())
        binding = harness.producer("binding_count", module.binding_count())

        harness.headline("gross_special_cells", gross)
        harness.headline("profile_count", profile)
        harness.headline("binding_special_cells", binding)
        harness.require_headline("gross_special_cells", gross == 3, "gross count drifted")
        harness.require_headline("profile_count", profile == (2, 1), "profile drifted")
        harness.require_headline(
            "binding_special_cells",
            binding >= 0,
            "binding headline should stay nonnegative",
        )
        harness.exercised("tested_partitions", 3)
        harness.finalize()
        return harness

    run()
    with pytest.raises(InstrumentationError, match="missing sealed producers: binding_count"):
        assert_producers_sealed(module, ("gross_count", "profile_count"), run)


def test_producer_tamper_rejects_undegeneratable_producer_without_stub() -> None:
    module = SimpleNamespace(binding_path=lambda: Path("proof.txt"))

    def run() -> Harness:
        harness = Harness()
        binding_path = harness.producer("binding_path", module.binding_path())
        harness.headline("binding_path", binding_path)
        harness.require_headline("binding_path", binding_path.name == "proof.txt", "wrong path")
        harness.exercised("tested_paths", 1)
        harness.finalize()
        return harness

    run()
    match = "cannot auto-degenerate producer 'binding_path'"
    with pytest.raises(InstrumentationError, match=match):
        assert_producers_sealed(module, ("binding_path",), run)


def test_producer_tamper_accepts_explicit_stub_for_undegeneratable_value() -> None:
    module = SimpleNamespace(binding_path=lambda: Path("proof.txt"))

    def run() -> Harness:
        harness = Harness()
        binding_path = harness.producer("binding_path", module.binding_path())
        harness.headline("binding_path", binding_path)
        harness.require_headline("binding_path", binding_path.name == "proof.txt", "wrong path")
        harness.exercised("tested_paths", 1)
        harness.finalize()
        return harness

    run()
    assert_producers_sealed(
        module,
        (ProducerSpec("binding_path", stub=Path("degenerate.txt")),),
        run,
    )
