"""
Pre-processing filter for the ServiceNow CSV pipeline.

Executes the approved data-quality SQL against the Bridge schema, splits
violation rows into the Error schema (stamped with __rejected_at /
__rule_id / __source_file), and promotes clean rows into Source.
"""
from __future__ import annotations
import json as _json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import MetaData, insert, text
from sqlalchemy.engine import Engine

from db import quote_ident, fq, supports_schemas

log = logging.getLogger(__name__)

RULES_SQL_PATH = Path(__file__).parent / "rules.sql"


def _lookup_table(md: MetaData, physical: str):
    """Find a table in `md` by physical name."""
    for key, tbl in md.tables.items():
        name = key.split(".", 1)[1] if "." in key else key
        if name == physical:
            return tbl
    return None


def load_rules_sql() -> str:
    sql = RULES_SQL_PATH.read_text(encoding="utf-8").strip()
    if not sql:
        raise RuntimeError("rules.sql is empty — no pre-processing filter to run")
    return sql


def fetch_violations(target_engine: Engine, sql: str, bridge_schema: str = "bridge") -> list[dict]:
    """Run the pre-processing query against Bridge; return violation rows."""
    with target_engine.connect() as conn:
        if supports_schemas(target_engine):
            try:
                conn.execute(text(f'SET search_path TO "{bridge_schema}", public'))
            except Exception:
                log.debug("search_path SET not supported", exc_info=True)
        result = conn.execute(text(sql))
        rows = [dict(r._mapping) for r in result]
    log.info("Pre-processor returned %d violation rows", len(rows))
    return rows


def _split_violations(violations: Iterable[dict]) -> dict[str, list[dict]]:
    """Group violation rows by source table — same contract as SCCM:
        RuleId, __table, __row(jsonb)."""
    by_table: dict[str, list[dict]] = {}
    for raw in violations:
        tbl = raw.pop("__table", None) or raw.pop("SourceTable", None) or "_unrouted"
        rule_id = raw.pop("RuleId", None)
        payload = raw.pop("__row", None)
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                log.warning("Could not parse __row JSON for %s", tbl)
                continue
        if not isinstance(payload, dict):
            payload = dict(raw)
        if rule_id is not None:
            payload["RuleId"] = rule_id
        by_table.setdefault(tbl, []).append(payload)
    return by_table


def write_errors(target_engine: Engine, error_md: MetaData,
                 violations: list[dict], name_map: dict) -> dict[str, int]:
    """Write violation rows to the Error schema.

    ``rules.sql`` emits ``__table`` as the BARE base name; *name_map*
    resolves that to the physical error table.
    """
    counts: dict[str, int] = {}
    if not violations:
        return counts
    now = datetime.now(timezone.utc)
    grouped = _split_violations(violations)
    with target_engine.begin() as conn:
        for base, rows in grouped.items():
            entry = name_map.get(base)
            phys = entry["error"] if entry else base
            err_table = _lookup_table(error_md, phys)
            if err_table is None:
                log.warning("Error table %s missing — skipping %d rows", phys, len(rows))
                continue
            full_key = err_table.fullname
            payload = []
            for r in rows:
                rule_id = r.pop("RuleId", None)
                clean = {k: v for k, v in r.items() if k in err_table.columns.keys()}
                if "__source_file" in err_table.columns.keys() and "__source_file" not in clean:
                    clean["__source_file"] = r.get("__source_file")
                # Preserve the original row verbatim as the rejection identity for
                # promote_to_source's anti-join (see _build_table).
                if "__row" in err_table.columns.keys():
                    clean["__row"] = {k: v for k, v in r.items() if k != "__row"}
                clean["__rejected_at"] = now
                clean["__rule_id"]     = str(rule_id) if rule_id is not None else None
                payload.append(clean)
            if not payload:
                continue
            # Isolate each table's insert in its own SAVEPOINT: on Postgres a
            # failed statement poisons the rest of the enclosing transaction,
            # so without this a single bad table would silently drop every
            # other table's errors for the rest of this run.
            try:
                with conn.begin_nested():
                    conn.execute(insert(err_table), payload)
                counts[base] = len(payload)
                log.info("Wrote %d errors → %s", len(payload), full_key)
            except Exception as exc:
                log.error("Failed to write %d error rows for %s — skipping "
                          "this table and continuing. Error: %s",
                          len(payload), full_key, exc)
    return counts


def promote_to_source(target_engine: Engine, bridge_md: MetaData,
                      source_md: MetaData, error_md: MetaData,
                      name_map: dict,
                      bridge_schema: str = "bridge") -> dict[str, int]:
    """Bridge rows that have NO matching Error row → Source schema.

    Pairs bridge/source/error tables via *name_map*. EXCEPT on the full row
    (excluding lineage / metadata).
    """
    counts: dict[str, int] = {}
    with target_engine.begin() as conn:
        for base, entry in name_map.items():
            bri = _lookup_table(bridge_md, entry["bridge"])
            src_t = _lookup_table(source_md, entry["source"])
            err_t = _lookup_table(error_md,  entry["error"])
            if bri is None or src_t is None or err_t is None:
                continue
            cols = [c.name for c in bri.columns if c.name not in ("__source_file",)]
            col_list = ", ".join(quote_ident(c) for c in cols)
            bridge_ref = fq(bri.schema, bri.name) if bri.schema else quote_ident(bri.name)
            source_ref = fq(src_t.schema, src_t.name) if src_t.schema else quote_ident(src_t.name)
            error_ref  = fq(err_t.schema, err_t.name) if err_t.schema else quote_ident(err_t.name)
            if "__row" in {c.name for c in err_t.columns}:
                # Identity anti-join: promote a bridge row only when no error row
                # captured it (e.__row == to_jsonb(bridge row)). Robust to lineage
                # columns and to error rows whose business columns were only
                # partially reconstructed — the failure mode of a column-wise
                # EXCEPT, which leaks rejected rows into Source.
                insert_sql = (
                    f"INSERT INTO {source_ref} ({col_list}) "
                    f"SELECT {col_list} FROM {bridge_ref} b "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {error_ref} e "
                    f'WHERE e."__row" IS NOT NULL AND e."__row" = to_jsonb(b))'
                )
            else:
                insert_sql = (
                    f"INSERT INTO {source_ref} ({col_list}) "
                    f"SELECT {col_list} FROM {bridge_ref} "
                    f"EXCEPT SELECT {col_list} FROM {error_ref}"
                )
            try:
                res = conn.execute(text(insert_sql))
                counts[base] = res.rowcount or 0
            except Exception as e:
                log.warning("Promote failed for %s — %s", base, e)
                counts[base] = 0
                continue
            log.info("Promoted %d rows → %s", counts[base], src_t.fullname)
    return counts
