"""Exact verification and search tools for total graph coloring."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("total-coloring-toolkit")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0+unknown"

__all__ = ["__version__"]
