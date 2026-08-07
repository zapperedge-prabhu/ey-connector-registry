"""
Pre-processing filter (Bridge → Source/Error split).

Executes the approved data-quality SQL against the Bridge schema. The
query returns rows that VIOLATE at least one rule (each tagged with a
`RuleId`). Violating rows go to the Error schema; all other Bridge rows
are promoted to the Source schema.
"""
from __future__ import annotations
import json as _json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import MetaData, insert, text
from sqlalchemy.engine import Engine

from db import quote_ident, fq

log = logging.getLogger(__name__)

RULES_SQL_PATH = Path(__file__).parent / "rules.sql"


def load_rules_sql() -> str:
    sql = RULES_SQL_PATH.read_text(encoding="utf-8").strip()
    if not sql:
        raise RuntimeError("rules.sql is empty — no pre-processing filter to run")
    return sql


def fetch_violations(target_engine: Engine, sql: str,
                     bridge_schema: str = "bridge") -> list[dict]:
    with target_engine.connect() as conn:
        try:
            conn.execute(text(f'SET search_path TO "{bridge_schema}", public'))
        except Exception:
            log.debug("search_path SET not supported by dialect", exc_info=True)
        result = conn.execute(text(sql))
        rows = [dict(r._mapping) for r in result]
    log.info("Pre-processor returned %d violation rows", len(rows))
    return rows


def _split_violations(violations: Iterable[dict]) -> dict[str, list[dict]]:
    by_table: dict[str, list[dict]] = {}
    for raw in violations:
        tbl = raw.pop("__table", None) or raw.pop("SourceTable", None) or "_unrouted"
        rule_id = raw.pop("RuleId", None)
        payload = raw.pop("__row", None)
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                log.warning("Could not parse __row JSON for table %s — skipping", tbl)
                continue
        if not isinstance(payload, dict):
            payload = dict(raw)
        if rule_id is not None:
            payload["RuleId"] = rule_id
        by_table.setdefault(tbl, []).append(payload)
    return by_table


def write_errors(target_engine: Engine, error_md: MetaData,
                 violations: list[dict], name_map: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not violations:
        return counts
    now = datetime.now(timezone.utc)
    grouped = _split_violations(violations)
    with target_engine.begin() as conn:
        for base, rows in grouped.items():
            entry = name_map.get(base)
            phys = entry["error"] if entry else base
            err_table = error_md.tables.get(f"{error_md.schema}.{phys}")
            if err_table is None:
                log.warning("Error table %s.%s not found — skipping %d rows",
                            error_md.schema, phys, len(rows))
                continue
            payload = []
            for r in rows:
                rule_id = r.pop("RuleId", None)
                clean = {k: v for k, v in r.items() if k in err_table.columns.keys()}
                # Preserve the ORIGINAL bridge row (captured verbatim as __row by
                # rules.sql) as the stable rejection identity. Without this the
                # error row's __id/__fetched_at/__row are NULL and its flat columns
                # are only partially populated, so the value-based EXCEPT in
                # promote_to_source never matches the bridge row — leaking every
                # rejected row into Source. promote_to_source anti-joins on __row.
                if "__row" in err_table.columns.keys():
                    clean["__row"] = dict(r)
                clean["__rejected_at"] = now
                clean["__rule_id"]     = str(rule_id) if rule_id is not None else None
                payload.append(clean)
            if payload:
                conn.execute(insert(err_table), payload)
                counts[base] = len(payload)
                log.info("Wrote %d errors → %s.%s", len(payload), error_md.schema, phys)
    return counts


def promote_to_source(target_engine: Engine,
                      bridge_md: MetaData, source_md: MetaData, error_md: MetaData,
                      name_map: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    with target_engine.begin() as conn:
        for base, entry in name_map.items():
            bri  = bridge_md.tables.get(f"{bridge_md.schema}.{entry['bridge']}")
            srct = source_md.tables.get(f"{source_md.schema}.{entry['source']}")
            errt = error_md.tables.get(f"{error_md.schema}.{entry['error']}")
            if bri is None or srct is None or errt is None:
                continue
            cols = [c.name for c in bri.columns]
            col_list = ", ".join(quote_ident(c) for c in cols)
            bri_cols = {c.name for c in bri.columns}
            err_cols = {c.name for c in errt.columns}
            if "__row" in bri_cols and "__row" in err_cols:
                # Identity-based anti-join: a bridge row is promoted only when no
                # error row shares its __row (the verbatim original row captured by
                # rules.sql). This is robust to lineage columns (__id/__fetched_at)
                # that differ between bridge and error and to error rows whose flat
                # columns were only partially reconstructed — the failure mode of a
                # plain column-wise EXCEPT, which leaks rejected rows into Source.
                insert_sql = (
                    f"INSERT INTO {fq(srct.schema, srct.name)} ({col_list}) "
                    f'SELECT {col_list} FROM {fq(bri.schema, bri.name)} b '
                    f"WHERE NOT EXISTS (SELECT 1 FROM {fq(errt.schema, errt.name)} e "
                    f'WHERE e."__row" IS NOT NULL AND e."__row" = b."__row")'
                )
            else:
                insert_sql = (
                    f"INSERT INTO {fq(srct.schema, srct.name)} ({col_list}) "
                    f"SELECT {col_list} FROM {fq(bri.schema, bri.name)} "
                    f"EXCEPT SELECT {col_list} FROM {fq(errt.schema, errt.name)}"
                )
            res = conn.execute(text(insert_sql))
            counts[base] = res.rowcount or 0
            log.info("Promoted %d rows → %s.%s", counts[base], srct.schema, srct.name)
    return counts
