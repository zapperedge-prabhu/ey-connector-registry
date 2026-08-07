"""
Schema alignment for API connectors.

For each endpoint in `endpoints.json` we create three mirror tables —
Bridge, Source, Error — with an identical column shape:

    __id          TEXT          (best-effort row identifier)
    __fetched_at  TIMESTAMPTZ   (extraction timestamp)
    __row         JSONB         (full response payload as captured)
    <field>       TEXT          (one column per discovered field on the endpoint)

Error tables additionally carry __rejected_at / __rule_id for routing
metadata. Physical names follow the studio convention
``<stage>_tbl_<source>_<base>`` enforced by ``table_naming``.
"""
from __future__ import annotations
import logging
from typing import Iterable

from sqlalchemy import (
    MetaData, Table, Column, String, Text, DateTime, inspect,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema

from db import quote_ident
from table_naming import physical_name, build_name_map, assert_physical_name

log = logging.getLogger(__name__)


def ensure_schema(engine: Engine, schema: str) -> None:
    insp = inspect(engine)
    if schema not in insp.get_schema_names():
        with engine.begin() as conn:
            conn.execute(CreateSchema(schema))
        log.info("Created schema %s", schema)


def _columns_for_endpoint(endpoint: dict, *, with_error_cols: bool = False) -> list[Column]:
    cols: list[Column] = [
        Column("__id",         Text,                       nullable=True),
        Column("__fetched_at", DateTime(timezone=True),    nullable=True),
        Column("__row",        JSONB,                      nullable=True),
    ]
    seen = {"__id", "__fetched_at", "__row"}
    for f in endpoint.get("fields", []):
        col = (f.get("column") or "").strip()
        if not col or col in seen:
            continue
        seen.add(col)
        cols.append(Column(col, Text, nullable=True))
    if with_error_cols:
        cols.append(Column("__rejected_at", DateTime(timezone=True), nullable=True))
        cols.append(Column("__rule_id",     String(128),             nullable=True))
    return cols


def mirror_schemas(
    engine: Engine,
    endpoints: list[dict],
    *,
    bridge_schema: str,
    target_source_schema: str,
    error_schema: str,
    source_name: str,
) -> tuple[MetaData, MetaData, MetaData, dict]:
    """Create Bridge / Source / Error tables from the endpoint catalogue."""
    bridge_md = MetaData(schema=bridge_schema)
    source_md = MetaData(schema=target_source_schema)
    error_md  = MetaData(schema=error_schema)

    bases = [ep["table"] for ep in endpoints]
    name_map = build_name_map(source_name, bases)

    for ep in endpoints:
        base = ep["table"]
        names = name_map[base]
        Table(assert_physical_name(names["bridge"], "api._mirror"),
              bridge_md, *_columns_for_endpoint(ep), schema=bridge_schema)
        Table(assert_physical_name(names["source"], "api._mirror"),
              source_md, *_columns_for_endpoint(ep), schema=target_source_schema)
        Table(assert_physical_name(names["error"], "api._mirror"),
              error_md, *_columns_for_endpoint(ep, with_error_cols=True),
              schema=error_schema)

    bridge_md.create_all(engine, checkfirst=True)
    source_md.create_all(engine, checkfirst=True)
    error_md.create_all(engine, checkfirst=True)

    # Schema *migration* — `create_all(checkfirst=True)` only creates a
    # table if it doesn't already exist; it never adds new columns to an
    # existing table. When a connector is redeployed after the user paste
    # a new sample (or after the catalogue gains real extracted columns
    # from a sample for the first time), the bridge / source / error
    # tables already exist with the OLD column set — so the new flat
    # columns would be silently dropped by the loader's `_project()`
    # filter, leaving pre-rules unable to reference them.
    #
    # This pass adds any columns present in the catalogue but missing
    # from the live table. Columns are only ever ADDED (never dropped or
    # renamed) so existing rows are preserved exactly. The new columns
    # come up as `NULL` for historical rows; the next sync repopulates
    # them from the JSON in `__row`.
    _add_missing_columns(engine, bridge_md)
    _add_missing_columns(engine, source_md)
    _add_missing_columns(engine, error_md)

    _create_compat_views(engine, name_map,
                         bridge_schema, target_source_schema, error_schema)

    log.info("Mirrored %d endpoints into %s / %s / %s (source=%s)",
             len(endpoints), bridge_schema, target_source_schema, error_schema,
             source_name)
    return bridge_md, source_md, error_md, name_map


def _add_missing_columns(engine: Engine, md: MetaData) -> None:
    """ALTER TABLE ADD COLUMN for any catalogue column missing from a
    pre-existing physical table. SAFE — never drops or renames existing
    columns, never alters types. Idempotent.

    Without this step, a connector re-deployed after the user pastes a new
    response sample would keep its OLD column set and the loader's
    `_project()` filter would silently drop the new fields, leaving
    pre-processing rules unable to reference them as bare columns.
    """
    from sqlalchemy import text as _text
    insp = inspect(engine)
    for table in md.tables.values():
        schema = table.schema
        name = table.name
        try:
            existing_names = {c["name"] for c in insp.get_columns(name, schema=schema)}
        except Exception as exc:
            # Reflection failed — most commonly the table doesn't exist
            # yet (create_all just made it and the inspector cached the
            # pre-create snapshot) but it could also be a permissions
            # error. Log a WARNING so a real failure is diagnosable in
            # production — silent continue here would hide a missed
            # migration until the next sync drops new columns.
            log.warning("Skipped column-migration inspection for %s.%s: %s",
                        schema or "public", name, exc)
            continue
        if not existing_names:
            log.debug("No live columns reported for %s.%s — skipping ALTER pass",
                      schema or "public", name)
            continue
        with engine.begin() as conn:
            for col in table.columns:
                if col.name in existing_names:
                    continue
                col_ddl = col.type.compile(dialect=engine.dialect)
                fq_table = f'"{schema}"."{name}"' if schema else f'"{name}"'
                conn.execute(_text(
                    f'ALTER TABLE {fq_table} ADD COLUMN IF NOT EXISTS '
                    f'"{col.name}" {col_ddl}'
                ))
                log.info("Added column %s.%s.%s (%s)",
                         schema or "public", name, col.name, col_ddl)


def _create_compat_views(engine: Engine, name_map: dict, *schemas: str) -> None:
    """Create read-only views named after the bare base, in each stage schema.

    Postgres' ``CREATE OR REPLACE VIEW`` cannot drop or change existing
    columns — it can only ADD new columns at the end. When the underlying
    table's shape changes between runs, the recreate fails with
    ``cannot drop columns from view`` and (because it's all one
    transaction) every subsequent view in the same block fails with
    ``current transaction is aborted``.

    Fix: views are stateless aliases (no data), so we ``DROP VIEW IF
    EXISTS`` first and then CREATE — that lets the new column set differ
    freely. Each view runs in its OWN transaction so one bad alias
    cannot poison the rest of the run.
    """
    import re as _re
    from sqlalchemy import text
    _IDENT_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    bridge_schema, target_source_schema, error_schema = schemas
    for base, names in name_map.items():
        if not _IDENT_RE.match(str(base) or ""):
            continue
        for stage, schema in (
            ("bridge", bridge_schema),
            ("source", target_source_schema),
            ("error",  error_schema),
        ):
            phys = names[stage]
            if phys == base:
                continue
            view_fq  = "{sch}.{alias}".format(
                sch=quote_ident(schema), alias=quote_ident(base))
            table_fq = "{sch}.{phys}".format(
                sch=quote_ident(schema), phys=quote_ident(phys))
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"DROP VIEW IF EXISTS {view_fq}"))
                    conn.execute(text(
                        f"CREATE VIEW {view_fq} AS SELECT * FROM {table_fq}"))
            except Exception as e:
                log.warning("Could not create compat view %s.%s -> %s: %s",
                            schema, base, phys, e)


def truncate_all(engine: Engine, md: MetaData) -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for t in md.sorted_tables:
            conn.execute(text(f"TRUNCATE {quote_ident(t.schema)}.{quote_ident(t.name)}"))
