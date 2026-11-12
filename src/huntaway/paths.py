"""
Where huntaway keeps its config, data, state and runtime files.

(XDG on Linux, Library on macOS).
"""

import os
import sys
from pathlib import Path

APP = "huntaway"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "").strip()
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    """
    Return the folder holding ``config.toml``.

    :returns: ``$HUNTAWAY_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/huntaway``
    """
    override = os.environ.get("HUNTAWAY_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def data_dir() -> Path:
    """
    Return the folder holding the database and the session archive.

    :returns: ``$HUNTAWAY_DATA_DIR``, else ``$XDG_DATA_HOME/huntaway``
    """
    override = os.environ.get("HUNTAWAY_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def state_dir() -> Path:
    """
    Return the folder holding the API token and the daemon log.

    :returns: ``$HUNTAWAY_STATE_DIR``, else ``$XDG_STATE_HOME/huntaway``
    """
    override = os.environ.get("HUNTAWAY_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP / "state"
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def runtime_dir() -> Path:
    """
    Return the folder holding leases from ``huntaway run``.

    :returns: ``$XDG_RUNTIME_DIR/huntaway``, else a folder under the state dir
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / APP if base else state_dir() / "run"


def config_file() -> Path:
    """
    Return the config file.

    :returns: ``config_dir() / "config.toml"``
    """
    return config_dir() / "config.toml"


def db_file() -> Path:
    """
    Return the SQLite database.

    :returns: ``data_dir() / "huntaway.db"``
    """
    return data_dir() / "huntaway.db"


def token_file() -> Path:
    """
    Return the API token file, mode 600.

    :returns: ``state_dir() / "token"``
    """
    return state_dir() / "token"


def shared_scan_dir() -> Path:
    """
    Where deep scans run as root leave their results for the user's daemon to read.

    :returns: ``/var/lib/huntaway/scans``
    """
    return Path("/var/lib") / APP / "scans"
