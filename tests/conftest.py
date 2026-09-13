from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatnodes.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Aisla la carpeta de datos para no tocar la del usuario."""
    monkeypatch.setenv("CHATNODES_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def project(store, tmp_path):
    folder = tmp_path / "material"
    folder.mkdir()
    return store.create_project("Sistemas Operativos", folder=str(folder))


@pytest.fixture
def chat(store, project):
    return store.create_chat(project["id"], "Tema 1")
