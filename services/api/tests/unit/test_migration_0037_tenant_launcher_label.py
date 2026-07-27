"""Shape tests for SR-7's additive tenant launcher-label migration."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0037_tenant_launcher_label.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0037", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_the_single_next_head_and_adds_nullable_launcher_label() -> None:
    module = _load_migration()
    assert module.revision == "0037"  # type: ignore[attr-defined]
    assert module.down_revision == "0036"  # type: ignore[attr-defined]

    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ALTER TABLE tenant_bot_settings ADD COLUMN launcher_label text" in source
    assert "NOT NULL" not in source
