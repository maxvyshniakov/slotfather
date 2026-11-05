"""Identity of the exact runtime source, preserved across local and SSH installs."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def info() -> dict[str, Any]:
    """Read immutable installed metadata, or derive it from this source checkout."""
    metadata = Path(__file__).with_name("version.json")
    if metadata.is_file():
        return json.loads(metadata.read_text())
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("slotfather_installer_version", root / "install.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot inspect slotfather source identity")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer.source_identity(root)


def text(value: dict[str, Any] | None = None) -> str:
    """A revision and content digest; a dirty checkout never masquerades as a clean commit."""
    value = info() if value is None else value
    commit = value.get("commit") or "unversioned"
    return f"slotfather {commit}{'+dirty' if value.get('dirty') else ''} source {value['source_sha256']}"
