"""Shared fixtures: every test gets its own home, config, data and state folders."""

import os
from pathlib import Path

import pytest

from huntaway import config
from huntaway.store import Store


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every huntaway folder and HOME at a temporary directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUNTAWAY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HUNTAWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUNTAWAY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    return home


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    """Build default settings with notifications and agent detection off."""
    values = config.defaults()
    values["general.notify"] = False
    values["agents.enabled"] = False
    values["sessions.archive_dir"] = str(tmp_path / "archive")
    return config.Config(values=values, path=tmp_path / "config" / "config.toml")


@pytest.fixture
def store(tmp_path: Path):
    """Open an empty database and close it afterwards."""
    db = Store(tmp_path / "data" / "test.db")
    yield db
    db.close()


def pytest_configure() -> None:
    os.environ.setdefault("TZ", "UTC")
