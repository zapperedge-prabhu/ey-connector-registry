"""
Microsoft 365 API Pre-processing Connector — main entrypoint.

Pipeline:
  1. Fetch each endpoint listed in endpoints.json (with pagination)
  2. Bridge-load every flattened row into bridge.<bridge_tbl_*>
  3. Run rules.sql against Bridge → split into Source / Error
  4. Promote canonical rows into the ETL schema (post-processor)
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path

import config
from db import make_engine
from schema_manager import (
    ensure_schema, mirror_schemas, truncate_all,
)
from auth import AuthProvider
from http_client import HttpClient
from extractor import fetch_endpoint
from bridge_loader import load_endpoint
from pre_processor import (
    load_rules_sql, fetch_violations, write_errors, promote_to_source,
)
from etl_schema import ensure_etl_tables
from post_processor import run_post_processing

ETL_SCHEMA = "etl"
ENDPOINTS_PATH = Path(__file__).parent / "endpoints.json"


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _load_endpoints() -> list[dict]:
    if not ENDPOINTS_PATH.exists():
        raise RuntimeError("endpoints.json missing — re-generate the connector")
    return json.loads(ENDPOINTS_PATH.read_text())


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    log = logging.getLogger("api-connector")

    parser = argparse.ArgumentParser(description="Microsoft 365 API connector")
    parser.add_argument("--dry-run", action="store_true",
                        help="Bridge-load + counts only; skip Source/Error promotion")
    args = parser.parse_args(argv)

    endpoints = _load_endpoints()
    log.info("Endpoints to process: %d", len(endpoints))

    tgt_eng = make_engine(config.TARGET_CONNECTION_STRING)
    for s in (config.BRIDGE_SCHEMA, config.TARGET_SOURCE_SCHEMA, config.ERROR_SCHEMA):
        ensure_schema(tgt_eng, s)
    ensure_etl_tables(tgt_eng, schema=ETL_SCHEMA)

    bridge_md, source_md, error_md, name_map = mirror_schemas(
        tgt_eng, endpoints,
        bridge_schema=config.BRIDGE_SCHEMA,
        target_source_schema=config.TARGET_SOURCE_SCHEMA,
        error_schema=config.ERROR_SCHEMA,
        source_name=config.SOURCE_NAME,
    )
    truncate_all(tgt_eng, bridge_md)
    truncate_all(tgt_eng, source_md)
    truncate_all(tgt_eng, error_md)

    auth = AuthProvider()
    client = HttpClient(auth=auth)
    loaded: dict[str, int] = {}
    failed: dict[str, str] = {}
    try:
        for ep in endpoints:
            base = ep["table"]
            entry = name_map.get(base)
            if not entry:
                log.warning("No name-map entry for %s — skipping", base)
                continue
            bri = bridge_md.tables.get(f"{bridge_md.schema}.{entry['bridge']}")
            if bri is None:
                log.warning("Bridge table for %s missing — skipping", base)
                continue
            try:
                n = load_endpoint(tgt_eng, bri, fetch_endpoint(client, ep))
                loaded[base] = n
            except Exception as exc:  # noqa: BLE001 — endpoint-level isolation
                # One bad endpoint (auth denied, 4xx/5xx, parse error, etc.) must
                # NOT take the whole connector down. Log it and move on so the
                # remaining endpoints still land in Bridge / Source / Error.
                msg = f"{type(exc).__name__}: {exc}"
                # Keep the message terse — full traceback at debug level only.
                log.error("Endpoint %s %s FAILED — skipping. %s",
                          ep.get("method", "GET"), ep.get("endpoint", base), msg)
                log.debug("Endpoint %s traceback:", base, exc_info=True)
                failed[base] = msg
                loaded[base] = 0
    finally:
        client.close()
    log.info("Bridge load complete: %d ok, %d failed. counts=%s",
             len(loaded) - len(failed), len(failed), loaded)
    if failed:
        log.warning("Failed endpoints (%d): %s", len(failed), list(failed.keys()))

    if args.dry_run:
        log.info("--dry-run set; skipping pre-processing & promotion")
        return 0

    sql = load_rules_sql()
    violations = fetch_violations(tgt_eng, sql, bridge_schema=config.BRIDGE_SCHEMA)
    err_counts = write_errors(tgt_eng, error_md, violations, name_map)
    src_counts = promote_to_source(tgt_eng, bridge_md, source_md, error_md, name_map)

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
