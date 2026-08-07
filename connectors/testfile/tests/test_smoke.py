"""Smoke test — verifies modules import and the catalogue is present."""
import importlib
import os


def test_modules_import(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_DIR", str(tmp_path))
    monkeypatch.setenv("TARGET_CONNECTION_STRING", "sqlite:///:memory:")
    for mod in ("config", "db", "schema_manager", "csv_loader", "pre_processor"):
        importlib.import_module(mod)


def test_tables_json_present():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "tables.json"
    assert p.exists(), "tables.json must be packaged with the connector"
    import json
    cat = json.loads(p.read_text())
    assert isinstance(cat, dict) and cat, "tables.json must list at least one CSV"


def test_rules_sql_present():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "rules.sql"
    assert p.exists(), "rules.sql must be packaged with the connector"
    assert p.read_text().strip(), "rules.sql must not be empty"
