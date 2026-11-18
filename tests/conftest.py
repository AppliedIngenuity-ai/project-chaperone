"""Helpers shared by the tests."""

import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def write(tmp_path: Path):
    """`write(name, text)`: writes `text`, dedented, to `tmp_path / name` and returns the path."""
    def _write(name: str, text: str) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path
    return _write


@pytest.fixture(autouse=True)
def no_users_changes(monkeypatch, tmp_path: Path):
    """No test reads the user's own private/local.yaml: the command's default file is one that does not exist."""
    import chaperone.__main__ as command
    monkeypatch.setattr(command, "LOCAL_FILE", tmp_path / "no_such_dir" / "local.yaml")
