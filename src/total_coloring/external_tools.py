"""Hash-pinned external executable identities.

The toolkit never treats a solver or proof checker as part of its trusted
Python runtime. Callers must supply an explicit path and SHA-256 digest, and
the digest is rechecked immediately before each invocation.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of *path* without loading it all at once."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PinnedFile:
    """A regular file whose canonical path and bytes are part of run identity."""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("file SHA-256 must be 64 lowercase hexadecimal characters")
        requested = Path(self.path).expanduser()
        try:
            canonical = requested.resolve(strict=True)
            status = canonical.stat()
        except OSError as exc:
            raise ValueError(f"cannot resolve pinned file {requested}: {exc}") from exc
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f"pinned path is not a regular file: {canonical}")
        object.__setattr__(self, "path", canonical)
        self.verify()

    def verify(self) -> Path:
        """Recheck the file and return its canonical path."""

        try:
            status = self.path.stat()
        except OSError as exc:
            raise ValueError(f"cannot inspect pinned file {self.path}: {exc}") from exc
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f"pinned path is no longer a regular file: {self.path}")
        actual = sha256_file(self.path)
        if actual != self.sha256:
            raise ValueError(
                f"SHA-256 mismatch for {self.path}: expected {self.sha256}, got {actual}"
            )
        return self.path

    def identity(self) -> dict[str, str]:
        """Return a path-free provenance identity suitable for public receipts."""

        return {"name": self.path.name, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class PinnedExecutable:
    """An executable whose canonical path and bytes are part of run identity."""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        pinned = PinnedFile(self.path, self.sha256)
        if not os.access(pinned.path, os.X_OK):
            raise ValueError(f"external tool is not executable: {pinned.path}")
        object.__setattr__(self, "path", pinned.path)

    def verify(self) -> Path:
        """Recheck the executable and return its canonical invocation path."""

        path = PinnedFile(self.path, self.sha256).verify()
        if not os.access(path, os.X_OK):
            raise ValueError(f"pinned tool is no longer executable: {path}")
        return path

    def identity(self) -> dict[str, str]:
        """Return a path-free provenance identity suitable for public receipts."""

        return {"name": self.path.name, "sha256": self.sha256}
