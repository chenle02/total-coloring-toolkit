"""Minimal instrumentation helpers for fail-closed authoring checks.

The test suite is the contract for this module. It provides a small harness for
recording producer outputs, requiring explicit assertions for all reported
headlines, and proving that each registered producer is sealed by a degenerate
rerun.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


class InstrumentationError(RuntimeError):
    """Raised when instrumentation contracts are violated."""


@dataclass(frozen=True, slots=True)
class ProducerSpec:
    """A named producer together with an optional explicit degenerate stub."""

    name: str
    stub: object | None = None


class Harness:
    """Record producer outputs and require explicit checks before finalization."""

    def __init__(self) -> None:
        self._producers: dict[str, object] = {}
        self._headlines: dict[str, object] = {}
        self._asserted_headlines: set[str] = set()
        self._exercised_counts: dict[str, int] = {}

    def producer(self, name: str, value: object) -> object:
        """Register and return a producer value."""

        self._producers[name] = value
        return value

    def headline(self, name: str, value: object) -> None:
        """Register a reported headline value."""

        self._headlines[name] = value

    def require_headline(self, name: str, condition: bool, message: str) -> None:
        """Require a headline assertion and mark the headline as asserted."""

        if name not in self._headlines:
            raise InstrumentationError(f"unknown headline '{name}'")
        if not condition:
            raise InstrumentationError(message)
        self._asserted_headlines.add(name)

    def exercised(self, name: str, count: int) -> None:
        """Record how many cases a named measurement exercised."""

        self._exercised_counts[name] = count

    def finalize(self) -> None:
        """Reject green summaries with missing assertions or zero exercised counts."""

        unasserted = sorted(set(self._headlines) - self._asserted_headlines)
        if unasserted:
            joined = ", ".join(unasserted)
            raise InstrumentationError(f"unasserted headline: {joined}")

        zero_counts = sorted(
            name for name, count in self._exercised_counts.items() if count == 0
        )
        if zero_counts:
            joined = ", ".join(zero_counts)
            raise InstrumentationError(f"exercised count is zero: {joined}")


def assert_producers_sealed(
    module: SimpleNamespace,
    producers: tuple[str | ProducerSpec, ...],
    run: Any,
) -> None:
    """Require each named producer to make the instrumented run fail when stubbed."""

    baseline = run()
    expected = _normalize_specs(producers)
    missing = sorted(name for name in baseline._producers if name not in expected)
    if missing:
        raise InstrumentationError(f"missing sealed producers: {', '.join(missing)}")

    for name, spec in expected.items():
        original = getattr(module, name)
        stub = (
            spec.stub
            if spec.stub is not None
            else _auto_degenerate(name, baseline._producers[name])
        )
        setattr(module, name, _stub_factory(stub))
        try:
            try:
                run()
            except Exception:
                continue
            raise InstrumentationError(f"unsealed producer '{name}'")
        finally:
            setattr(module, name, original)


def _normalize_specs(producers: tuple[str | ProducerSpec, ...]) -> dict[str, ProducerSpec]:
    specs: dict[str, ProducerSpec] = {}
    for item in producers:
        spec = ProducerSpec(item) if isinstance(item, str) else item
        specs[spec.name] = spec
    return specs


def _stub_factory(stub: object) -> Any:
    def _producer() -> object:
        return stub

    return _producer


def _auto_degenerate(name: str, value: object) -> object:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return value + "__degenerate__"
    if isinstance(value, tuple):
        return tuple(_auto_degenerate(f"{name}[{index}]", item) for index, item in enumerate(value))
    raise InstrumentationError(f"cannot auto-degenerate producer '{name}'")
