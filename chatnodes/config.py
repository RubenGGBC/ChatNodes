"""Rutas y constantes de configuracion."""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_NAME = "ChatNodes"

DEFAULT_MODEL = "sonnet"
MODELS = ("sonnet", "opus", "haiku")

# Extensiones que Claude Code sabe leer con su herramienta Read.
ALLOWED_UPLOAD_SUFFIXES = {
    ".pdf", ".md", ".txt", ".csv", ".json", ".tex",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".docx", ".pptx", ".xlsx", ".ipynb",
}


def data_dir() -> Path:
    """Carpeta raiz de datos. CHATNODES_HOME permite aislarla en los tests."""
    override = os.environ.get("CHATNODES_HOME")
    root = Path(override) if override else Path.home() / "ChatNodes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_dir() / "chatnodes.db"


def projects_dir() -> Path:
    path = data_dir() / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def web_dir() -> Path:
    return Path(__file__).parent / "web"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-.")
    return (slug or "proyecto")[:48]
