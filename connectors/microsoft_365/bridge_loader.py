"""Bridge load — inserts extracted rows into the matching Bridge tables."""
from __future__ import annotations
import logging
from typing import Iterable

from sqlalchemy import MetaData, Table, insert
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

BATCH = 1_000


def _project(row: dict, table: Table) -> dict:
    """Keep only keys that exist on the target table to avoid InvalidColumn."""
    cols = set(table.columns.keys())
    return {k: v for k, v in row.items() if k in cols}


def load_endpoint(target_engine: Engine, bridge_table: Table,
                  rows: Iterable[dict]) -> int:
    """Stream `rows` into `bridge_table` in batches. Returns row count."""
    n = 0
    batch: list[dict] = []
    with target_engine.begin() as conn:
        for r in rows:
            batch.append(_project(r, bridge_table))
            if len(batch) >= BATCH:
                conn.execute(insert(bridge_table), batch)
                n += len(batch)
                batch = []
        if batch:
            conn.execute(insert(bridge_table), batch)
            n += len(batch)
    log.info("Bridge-loaded %s.%s — %d rows",
             bridge_table.schema, bridge_table.name, n)
    return n
