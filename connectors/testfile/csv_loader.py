"""
Bridge load — reads each ServiceNow CSV file and inserts the rows into the
matching Bridge schema table verbatim. The originating filename is stamped
on every row in the `__source_file` column.
"""
from __future__ import annotations
import csv
import logging
from pathlib import Path

from sqlalchemy import MetaData, insert
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

BATCH = 5_000


def load_csv(target_engine: Engine, bridge_md: MetaData, physical_name: str,
             csv_path: Path, columns: list[str]) -> int:
    """Stream rows from `csv_path` → bridge table `physical_name`."""
    bri = None
    for key, tbl in bridge_md.tables.items():
        bare = key.split(".", 1)[1] if "." in key else key
        if bare == physical_name:
            bri = tbl
            break
    if bri is None:
        log.warning("Bridge table %s missing — skipping %s", physical_name, csv_path.name)
        return 0
    n = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        batch: list[dict] = []
        with target_engine.begin() as conn:
            for raw in reader:
                row = {c: raw.get(c, None) for c in columns}
                row["__source_file"] = csv_path.name
                batch.append(row)
                if len(batch) >= BATCH:
                    conn.execute(insert(bri), batch)
                    n += len(batch)
                    batch = []
            if batch:
                conn.execute(insert(bri), batch)
                n += len(batch)
    log.info("Bridge-loaded %s — %d rows from %s", physical_name, n, csv_path.name)
    return n


def load_all(target_engine: Engine, bridge_md: MetaData,
             source_dir: Path, catalogue: dict[str, dict],
             name_map: dict) -> dict[str, int]:
    """Load every CSV into its mapped bridge table.

    ``name_map[base]['bridge']`` resolves the physical bridge table for each
    logical catalogue entry. Counts are keyed by the base name.
    """
    counts: dict[str, int] = {}
    for base, meta in catalogue.items():
        entry = name_map.get(base)
        if not entry:
            log.warning("No name_map entry for %s — skipping", base)
            continue
        phys = entry["bridge"]
        files = list(meta.get("files") or ([meta["file"]] if meta.get("file") else []))
        if not files:
            log.warning("No CSV files registered for %s — skipping", base)
            continue
        total = 0
        for fname in files:
            path = source_dir / fname
            if not path.exists():
                log.warning("Missing CSV for %s — expected %s", base, path)
                continue
            try:
                total += load_csv(target_engine, bridge_md, phys, path, meta["columns"])
            except Exception as exc:
                # One bad/malformed CSV must not abort loading for every
                # other table — log it and keep going.
                log.error("Bridge-load failed for %s (%s) — skipping this "
                          "file and continuing. Error: %s", base, path.name, exc)
        counts[base] = total
    return counts
