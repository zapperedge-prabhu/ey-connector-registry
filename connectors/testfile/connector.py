"""
ServiceNow Pre-processing Connector — main entrypoint.

Pipeline:
  1. CSV → Bridge   — copy each ServiceNow CSV into the Bridge schema unchanged
  2. Pre-process    — run the approved data-quality SQL against Bridge
  3. Promote/Reject — passing rows → Source; failing rows → Error
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path

import config
from db import make_engine
from schema_manager import ensure_schema, build_metadata, truncate_all
from csv_loader import load_all
from pre_processor import load_rules_sql, fetch_violations, write_errors, promote_to_source
from etl_schema import ensure_etl_tables
from post_processor import run_post_processing

ETL_SCHEMA = "etl"


TABLES_PATH = Path(__file__).parent / "tables.json"


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _load_catalogue() -> dict[str, dict]:
    if not TABLES_PATH.exists():
        raise RuntimeError("tables.json missing — re-generate the connector")
    return json.loads(TABLES_PATH.read_text())


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    log = logging.getLogger("servicenow-connector")

    parser = argparse.ArgumentParser(description="ServiceNow CSV pre-processing connector")
    parser.add_argument("--dry-run", action="store_true",
                        help="Bridge-load only — skip Source/Error promotion")
    args = parser.parse_args(argv)

    catalogue = _load_catalogue()
    log.info("Tables to process: %s", ", ".join(catalogue.keys()))

    src_dir = Path(config.SOURCE_DIR)
    if not src_dir.is_dir():
        log.error("SOURCE_DIR is not a directory: %s", src_dir)
        return 2

    tgt_eng = make_engine(config.TARGET_CONNECTION_STRING)

    for s in (config.BRIDGE_SCHEMA, config.TARGET_SOURCE_SCHEMA, config.ERROR_SCHEMA):
        ensure_schema(tgt_eng, s)
    ensure_etl_tables(tgt_eng, schema=ETL_SCHEMA)

    bridge_md, source_md, error_md, name_map = build_metadata(
        tgt_eng, catalogue,
        bridge_schema=config.BRIDGE_SCHEMA,
        target_source_schema=config.TARGET_SOURCE_SCHEMA,
        error_schema=config.ERROR_SCHEMA,
        source_name=config.SOURCE_NAME,
    )

    truncate_all(tgt_eng, bridge_md)
    truncate_all(tgt_eng, source_md)
    truncate_all(tgt_eng, error_md)

    loaded = load_all(tgt_eng, bridge_md, src_dir, catalogue, name_map)
    log.info("Bridge load complete: %s", loaded)

    if args.dry_run:
        log.info("--dry-run set; skipping pre-processing & promotion")
        print(json.dumps({"bridgeLoaded": loaded}, indent=2))
        return 0

    sql = load_rules_sql()
    violations = fetch_violations(tgt_eng, sql, bridge_schema=config.BRIDGE_SCHEMA)
    err_counts = write_errors(tgt_eng, error_md, violations, name_map)
    src_counts = promote_to_source(tgt_eng, bridge_md, source_md, error_md,
                                   name_map,
                                   bridge_schema=config.BRIDGE_SCHEMA)

    etl_counts = run_post_processing(
        tgt_eng,
        source_schema=config.TARGET_SOURCE_SCHEMA,
        etl_schema=ETL_SCHEMA,
    )

    summary = {
        "bridgeLoaded": loaded,
        "errors":       err_counts,
        "promoted":     src_counts,
        "etlLoaded":    etl_counts,
    }
    print(json.dumps(summary, indent=2, default=str))
    log.info("Run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
