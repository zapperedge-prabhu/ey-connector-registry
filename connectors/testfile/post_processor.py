"""
Post-processing stage: Source → ETL.

Loads a single `etl_loader.sql` — one combined INSERT…SELECT per ETL table —
and executes it against the target database. The SQL merges field-mapping
column projections and post-processing rules into one UNION ALL per ETL
table, so every transformation and filter is applied on the SOURCE table in
a single pass rather than a second stage that operates on the ETL table.

The file is optional: if absent or empty the stage is skipped silently.
ETL tables targeted by the loader are TRUNCATEd first so re-runs are
idempotent.
"""
from __future__ import annotations
import logging
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
ETL_LOADER_PATH = _HERE / "etl_loader.sql"

# Extract the target ETL table from an INSERT…INTO "etl"."<table>" header.
_INSERT_TARGET_RE = re.compile(
    r'INSERT\s+INTO\s+"?etl"?\s*\.\s*"?(\w+)"?', re.IGNORECASE
)

# Only simple snake_case table names are allowed in the TRUNCATE path.
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _read_sql(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_etl_loader_sql() -> str:
    return _read_sql(ETL_LOADER_PATH)


_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script on semicolons while honoring single- and double-
    quoted literals, Postgres dollar-quoted strings (``$$…$$`` and
    ``$tag$…$tag$``) and ``--`` / ``/* … */`` comments so a stray ``;`` inside
    any of those cannot truncate a statement."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_sq = in_dq = in_line = in_block = False
    dq_tag: str | None = None   # active dollar-quote tag, e.g. "" or "body"
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if dq_tag is not None:
            if ch == "$":
                m = _DOLLAR_TAG_RE.match(sql, i)
                if m and (m.group(1) or "") == dq_tag:
                    buf.append(m.group(0))
                    i = m.end(); dq_tag = None; continue
            buf.append(ch); i += 1; continue
        if in_line:
            buf.append(ch)
            if ch == "\n":
                in_line = False
            i += 1; continue
        if in_block:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt); i += 2; in_block = False; continue
            i += 1; continue
        if in_sq:
            buf.append(ch)
            if ch == "'" and nxt == "'":
                buf.append(nxt); i += 2; continue
            if ch == "'":
                in_sq = False
            i += 1; continue
        if in_dq:
            buf.append(ch)
            if ch == '"' and nxt == '"':
                buf.append(nxt); i += 2; continue
            if ch == '"':
                in_dq = False
            i += 1; continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                buf.append(m.group(0))
                dq_tag = m.group(1) or ""
                i = m.end(); continue
        if ch == "-" and nxt == "-":
            buf.append(ch); buf.append(nxt); i += 2; in_line = True; continue
        if ch == "/" and nxt == "*":
            buf.append(ch); buf.append(nxt); i += 2; in_block = True; continue
        if ch == "'":
            buf.append(ch); i += 1; in_sq = True; continue
        if ch == '"':
            buf.append(ch); i += 1; in_dq = True; continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []; i += 1; continue
        buf.append(ch); i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _target_tables(sql: str) -> list[str]:
    """Return the ordered list of ETL table names INSERTed into by `sql`."""
    seen: list[str] = []
    for m in _INSERT_TARGET_RE.finditer(sql):
        t = m.group(1)
        if _SAFE_IDENT_RE.match(t) and t not in seen:
            seen.append(t)
    return seen


def _run_block(conn, label: str, sql: str, counts: dict[str, int]) -> None:
    if not sql:
        log.info("%s empty — skipping", label)
        return
    for stmt in _split_statements(sql):
        m = _INSERT_TARGET_RE.search(stmt)
        tbl = m.group(1) if (m and _SAFE_IDENT_RE.match(m.group(1))) else "etl"
        # Isolate each statement in its own SAVEPOINT: on Postgres a failed
        # statement poisons the rest of the enclosing transaction, so
        # without this one bad/missing source table (e.g. a stale mapping
        # referencing a table that was renamed or never created) would
        # silently abort loading for every other ETL table in the same
        # loader run. Log it and keep going instead.
        try:
            with conn.begin_nested():
                res = conn.execute(text(stmt))
            counts[tbl] = counts.get(tbl, 0) + (res.rowcount or 0)
            log.info("%s → etl.%s: %s rows", label, tbl, res.rowcount)
        except Exception as exc:
            log.error("%s failed for etl.%s — skipping this statement and "
                      "continuing with the rest. Error: %s", label, tbl, exc)


def run_post_processing(
    target_engine: Engine,
    source_schema: str = "source",
    etl_schema: str = "etl",
) -> dict[str, int]:
    """Execute etl_loader.sql inside one transaction.

    The loader contains one combined INSERT…SELECT (UNION ALL of field-mapping
    projections and post-processing rule SELECTs) per ETL table, all reading
    from the connector's Source schema. ETL tables targeted by the loader are
    TRUNCATEd first so the stage is idempotent.
    """
    loader_sql = load_etl_loader_sql()
    if not loader_sql:
        log.info("etl_loader.sql absent or empty — skipping Source → ETL")
        return {}

    targets = _target_tables(loader_sql)

    counts: dict[str, int] = {}
    with target_engine.begin() as conn:
        try:
            conn.execute(
                text(f'SET search_path TO "{source_schema}", "{etl_schema}", public')
            )
        except Exception:
            log.debug("search_path SET not supported — relying on schema-qualified names",
                      exc_info=True)

        for tbl in targets:
            log.info("truncating etl.%s for idempotent reload", tbl)
            try:
                with conn.begin_nested():
                    conn.execute(text(f'TRUNCATE TABLE "{etl_schema}"."{tbl}"'))
            except Exception as exc:
                # A missing/renamed ETL table must not abort truncation (and
                # therefore loading) for every other ETL table in this run.
                log.error("Could not truncate etl.%s — skipping and "
                          "continuing with the rest. Error: %s", tbl, exc)

        _run_block(conn, "etl-loader", loader_sql, counts)
    return counts


def _main() -> int:
    """Standalone entrypoint: reads TARGET_CONNECTION_STRING from the
    environment, bootstraps the canonical ETL tables, then runs
    etl_loader.sql against the configured Source schema."""
    import os, json
    from sqlalchemy import create_engine

    cs = os.environ.get("TARGET_CONNECTION_STRING", "").strip()
    if not cs:
        raise SystemExit("TARGET_CONNECTION_STRING env var is required to run post_processor")

    src_schema = os.environ.get("TARGET_SOURCE_SCHEMA", "source").strip() or "source"
    etl_schema = os.environ.get("ETL_SCHEMA", "etl").strip() or "etl"

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    engine = create_engine(cs, future=True)
    try:
        from etl_schema import ensure_etl_tables  # type: ignore
        ensure_etl_tables(engine, schema=etl_schema)
    except Exception as exc:
        log.warning("ensure_etl_tables skipped: %s", exc)

    counts = run_post_processing(engine, source_schema=src_schema, etl_schema=etl_schema)
    print(json.dumps({"etlLoaded": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
