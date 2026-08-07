"""Smoke test — verifies modules import and configuration loads."""
import importlib


def test_modules_import(monkeypatch):
    monkeypatch.setenv("TARGET_CONNECTION_STRING", "postgresql://u:p@h/db")
    for mod in ("config", "db", "auth", "http_client",
                "schema_manager", "extractor", "bridge_loader", "pre_processor"):
        importlib.import_module(mod)


def test_endpoints_packaged():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "endpoints.json"
    assert p.exists(), "endpoints.json must be packaged with the connector"
    import json as _json
    data = _json.loads(p.read_text())
    assert isinstance(data, list) and data, "endpoints.json must list at least one endpoint"


def test_rules_sql_present():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "rules.sql"
    assert p.exists(), "rules.sql must be packaged with the connector"
    assert p.read_text().strip(), "rules.sql must not be empty"
