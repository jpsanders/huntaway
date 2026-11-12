"""
huntaway, the sheepdog for your AI agents.

A light background service that saves and closes idle AI agent sessions, stops runaway processes before the machine
freezes, and hunts rootkits. It serves a local dashboard, a CLI and an MCP server.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("huntaway")
except PackageNotFoundError:  # pragma: no cover - running from a source tree that was never installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
