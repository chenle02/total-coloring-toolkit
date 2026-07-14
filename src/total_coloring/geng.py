"""Safe streaming adapter for nauty/Traces ``geng``."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from total_coloring.graph import SimpleGraph
from total_coloring.graph6 import decode_graph6


class GengError(RuntimeError):
    """Raised when graph generation fails or emits malformed data."""


_DEFAULT_EXECUTABLE_CANDIDATES = ("geng", "nauty-geng")


def _is_linux_proc_fd_path(path: Path) -> bool:
    parts = path.parts
    return (
        len(parts) == 5
        and parts[0] == "/"
        and parts[1] == "proc"
        and (parts[2] == "self" or parts[2].isdigit())
        and parts[3] == "fd"
        and parts[4].isdigit()
    )


@dataclass(frozen=True, slots=True)
class GengSpec:
    """Canonical graph-family and optional modular work-division arguments.

    ``split_depth`` is execution provenance carried by :meth:`arguments`. It is
    deliberately not a new field in the version-1 census configuration schema.
    """

    order: int
    connected: bool = False
    min_degree: int | None = None
    max_degree: int | None = None
    shard_index: int | None = None
    shard_count: int | None = None
    split_depth: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise ValueError("order must be a nonnegative integer")
        for label, value in (("min_degree", self.min_degree), ("max_degree", self.max_degree)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < max(self.order, 1)
            ):
                raise ValueError(f"{label} must be an integer in [0, order)")
        if (
            self.min_degree is not None
            and self.max_degree is not None
            and self.min_degree > self.max_degree
        ):
            raise ValueError("min_degree cannot exceed max_degree")
        if (self.shard_index is None) != (self.shard_count is None):
            raise ValueError("shard_index and shard_count must be supplied together")
        if self.shard_count is not None:
            if isinstance(self.shard_count, bool) or self.shard_count <= 0:
                raise ValueError("shard_count must be positive")
            if (
                isinstance(self.shard_index, bool)
                or self.shard_index is None
                or not 0 <= self.shard_index < self.shard_count
            ):
                raise ValueError("shard_index must lie in [0, shard_count)")
        if self.split_depth is not None:
            if isinstance(self.split_depth, bool) or not isinstance(self.split_depth, int):
                raise ValueError("split_depth must be a nonnegative integer")
            if self.split_depth < 0:
                raise ValueError("split_depth must be a nonnegative integer")
            if self.shard_count is None:
                raise ValueError("split_depth requires a sharded geng specification")

    def arguments(self) -> tuple[str, ...]:
        arguments = ["-q"]
        if self.split_depth is not None:
            arguments.append(f"-X{self.split_depth}")
        if self.connected:
            arguments.append("-c")
        if self.min_degree is not None:
            arguments.append(f"-d{self.min_degree}")
        if self.max_degree is not None:
            arguments.append(f"-D{self.max_degree}")
        arguments.append(str(self.order))
        if self.shard_count is not None:
            arguments.append(f"{self.shard_index}/{self.shard_count}")
        return tuple(arguments)


@dataclass(frozen=True, slots=True)
class GengIdentity:
    """Portable public identity for one resolved generator executable.

    ``executable`` is deliberately a basename, never the resolved local path.
    The SHA-256 digest binds the actual bytes used without leaking workstation
    or cluster directory names into a publishable census manifest.
    """

    executable: str
    sha256: str
    arguments: tuple[str, ...]


def resolve_geng(executable: str = "geng") -> Path:
    """Resolve a nauty ``geng`` executable without invoking a shell.

    Debian-family distributions install the binary as ``nauty-geng`` while
    upstream and several other distributions use ``geng``.  The default
    request accepts either portable package name; an explicit non-default
    executable remains exact and never falls back to another command.
    """

    candidates = _DEFAULT_EXECUTABLE_CANDIDATES if executable == "geng" else (executable,)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved is not None:
            path = Path(resolved)
            # Resolving a sealed memfd produces a display-only target such as
            # ``/memfd:geng (deleted)``, which cannot be reopened. Retain the
            # live descriptor path used by the Easley sealed loader.
            return path if _is_linux_proc_fd_path(path) else path.resolve()
    attempted = ", ".join(candidates)
    raise GengError(f"geng executable not found; tried: {attempted}")


def geng_identity(spec: GengSpec, *, executable: str = "geng") -> GengIdentity:
    path = resolve_geng(executable)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    portable_name = "geng" if _is_linux_proc_fd_path(path) else path.name
    return GengIdentity(portable_name, digest, spec.arguments())


def stream_geng(spec: GengSpec, *, executable: str = "geng") -> Iterator[SimpleGraph]:
    """Yield every graph from ``geng`` and fail closed on any bad record."""

    path = resolve_geng(executable)
    command = [str(path), *spec.arguments()]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="ascii",
    )
    assert process.stdout is not None
    assert process.stderr is not None
    completed = False
    try:
        for record_number, line in enumerate(process.stdout, start=1):
            record = line.strip()
            if not record:
                raise GengError(f"geng emitted an empty record at line {record_number}")
            try:
                graph = decode_graph6(record)
            except ValueError as error:
                raise GengError(f"invalid graph6 at line {record_number}: {error}") from error
            if graph.order != spec.order:
                raise GengError(
                    f"geng emitted order {graph.order} at line {record_number}; "
                    f"expected {spec.order}"
                )
            yield graph
        stderr = process.stderr.read()
        return_code = process.wait()
        completed = True
        if return_code != 0:
            raise GengError(f"geng exited with status {return_code}: {stderr.strip()}")
    finally:
        if not completed and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
