"""Schema alignment for the ServiceNow CSV pipeline.

The three schemas (Bridge, Source, Error) all mirror the source CSV
column layout. Bridge + Error tables carry a `__source_file` lineage
column populated with the originating CSV filename. Error tables also
carry `__rejected_at` / `__rule_id` rejection metadata.
"""
from __future__ import annotations
import logging

from sqlalchemy import MetaData, Table, Column, String, Text, DateTime, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema

from db import quote_ident, supports_schemas
from table_naming import build_name_map, assert_physical_name

log = logging.getLogger(__name__)


def ensure_schema(engine: Engine, schema: str) -> None:
    if not supports_schemas(engine):
        return
    insp = inspect(engine)
    if schema not in insp.get_schema_names():
        with engine.begin() as conn:
            conn.execute(CreateSchema(schema))
        log.info("Created schema %s", schema)


def _build_table(
    md: MetaData, table_name: str, columns: list[str], schema: str | None,
    *, with_lineage: bool, with_error_cols: bool,
) -> Table:
    cols = [Column(c, Text(), nullable=True) for c in columns]
    if with_lineage:
        cols.append(Column("__source_file", String(255), nullable=True))
    if with_error_cols:
        # __row stores the original bridge row verbatim (as captured by rules.sql)
        # so promote_to_source can anti-join bridge↔error on identity rather than
        # a fragile column-wise EXCEPT that leaks rejected rows when the error row
        # isn't a faithful full copy (e.g. subset-projection rules).
        cols.append(Column("__row",         JSONB,                   nullable=True))
        cols.append(Column("__rejected_at", DateTime(timezone=True), nullable=True))
        cols.append(Column("__rule_id",     String(128),             nullable=True))
    # Enforce <stage>_tbl_<source>_<base> at the create-table call site.
    assert_physical_name(table_name, context="servicenow._build_table")
    return Table(table_name, md, *cols, schema=schema)


def _migrate_legacy_bare_tables(engine: Engine, name_map: dict, *schemas) -> None:
    """Rename any TABLE still using the bare base name in the target stage
    schemas to ``<base>__legacy_pre_naming_convention`` so the new compat VIEW
    can take the bare name unambiguously. Preserves data (RENAME, not DROP).
    """
    import logging as _logging
    from sqlalchemy import inspect, text as _text
    _log = _logging.getLogger("schema_manager")
    insp = inspect(engine)
    bases = set(name_map.keys())
    with engine.begin() as conn:
        for schema in schemas:
            if schema is None:
                continue
            try:
                existing = set(insp.get_table_names(schema=schema))
            except Exception as e:
                _log.warning("Could not inspect schema %s: %s", schema, e)
                continue
            for base in bases & existing:
                quarantine = f"{base}__legacy_pre_naming_convention"
                if quarantine in existing:
                    _log.warning(
                        "Legacy bare table %s.%s found, but quarantine name "
                        "%s already exists; leaving untouched.",
                        schema, base, quarantine,
                    )
                    continue
                try:
                    conn.execute(_text(
                        "ALTER TABLE {sch}.{old} RENAME TO {new}".format(
                            sch=quote_ident(schema),
                            old=quote_ident(base),
                            new=quote_ident(quarantine),
                        )
                    ))
                    _log.warning(
                        "Migrated legacy bare table %s.%s → %s.%s (data preserved; "
                        "compat view will now take the bare name).",
                        schema, base, schema, quarantine,
                    )
                except Exception as e:
                    _log.error(
                        "Could not rename legacy bare table %s.%s: %s. "
                        "Compat view may fail — rename or drop manually.",
                        schema, base, e,
                    )


def _create_compat_views(
    engine: Engine, schema: str | None, name_map: dict,
    stage: str,
) -> None:
    """Create bare-base-name views pointing at the physical stage table so
    legacy pre / post SQL referencing ``FROM v_R_System`` still resolves when
    search_path includes *schema*. No-op on SQLite.

    Uses DROP-then-CREATE rather than CREATE OR REPLACE because the
    latter cannot drop or change existing columns when the underlying
    table's shape changes between runs (Postgres throws "cannot drop
    columns from view"). Each view runs in its own transaction so a
    single bad alias cannot poison the rest of the run.
    """
    if not supports_schemas(engine) or not schema:
        return
    import re as _re
    _IDENT_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for base, entry in name_map.items():
        if not _IDENT_RE.match(str(base) or ""):
            log.warning(
                "Skipping compat-view for base %r (not a bare SQL identifier); "
                "physical %s table is still created.", base, stage,
            )
            continue
        phys = entry[stage]
        if phys == base:
            continue
        view_fq  = f"{quote_ident(schema)}.{quote_ident(base)}"
        table_fq = f"{quote_ident(schema)}.{quote_ident(phys)}"
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DROP VIEW IF EXISTS {view_fq}"))
                conn.execute(text(
                    f"CREATE VIEW {view_fq} AS SELECT * FROM {table_fq}"))
        except Exception as e:
            log.warning("Could not create compat view %s.%s -> %s.%s: %s",
                        schema, base, schema, phys, e)


def _add_missing_columns(engine: Engine, md: MetaData) -> None:
    """ALTER TABLE ADD COLUMN for any catalogue column missing from a
    pre-existing physical table. SAFE — never drops or renames existing
    columns, never alters types. Idempotent.

    Without this step, a connector re-run after the ServiceNow export
    catalogue gains a new column would keep the OLD column set on the
    already-created bridge / source / error tables (``create_all
    (checkfirst=True)`` skips tables that already exist), and every
    subsequent INSERT would crash with ``UndefinedColumn`` because the
    freshly-built Table object includes a column the physical table lacks.
    """
    insp = inspect(engine)
    for table in md.tables.values():
        schema = table.schema
        name = table.name
        try:
            existing_names = {c["name"] for c in insp.get_columns(name, schema=schema)}
        except Exception as exc:
            # Reflection failed — most commonly the table doesn't exist yet
            # (create_all just made it and the inspector cached the
            # pre-create snapshot) but could also be a permissions error.
            # Log a WARNING so a real failure is diagnosable in production —
            # silently continuing here would hide a missed migration until
            # the next sync drops new columns.
            log.warning("Skipped column-migration inspection for %s.%s: %s",
                        schema or "public", name, exc)
            continue
        if not existing_names:
            log.debug("No live columns reported for %s.%s — skipping ALTER pass",
                      schema or "public", name)
            continue
        for col in table.columns:
            if col.name in existing_names:
                continue
            col_ddl = col.type.compile(dialect=engine.dialect)
            fq_table = f'"{schema}"."{name}"' if schema else f'"{name}"'
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE {fq_table} ADD COLUMN IF NOT EXISTS '
                        f'"{col.name}" {col_ddl}'
                    ))
                log.info("Added column %s.%s.%s (%s)",
                         schema or "public", name, col.name, col_ddl)
            except Exception as exc:
                # Isolate the failure to this one column/table so a single
                # incompatible ALTER doesn't abort migration for every other
                # table in this metadata (bridge/source/error each hold many).
                log.error("Could not add column %s.%s.%s (%s): %s — "
                          "continuing with remaining tables/columns.",
                          schema or "public", name, col.name, col_ddl, exc)


def build_metadata(
    engine: Engine,
    catalogue: dict[str, dict],
    bridge_schema: str,
    target_source_schema: str,
    error_schema: str,
    source_name: str = "servicenow",
) -> tuple[MetaData, MetaData, MetaData, dict]:
    """Construct & create Bridge / Source / Error tables for every CSV.

    Physical table naming:  ``<stage>_tbl_<source_name>_<base>`` in every
    stage schema. Returns the three MetaData objects plus a *name_map*
    keyed by the original CSV/table base name:
        ``{base: {"base", "bridge", "source", "error"}}``
    """
    use_schemas = supports_schemas(engine)
    bs = bridge_schema       if use_schemas else None
    ss = target_source_schema if use_schemas else None
    es = error_schema        if use_schemas else None

    bridge_md = MetaData(schema=bs)
    source_md = MetaData(schema=ss)
    error_md  = MetaData(schema=es)

    bases = list(catalogue.keys())
    name_map = build_name_map(source_name, bases)

    for tname, meta in catalogue.items():
        cols = list(meta["columns"])
        entry = name_map[tname]
        _build_table(bridge_md, entry["bridge"], cols, bs, with_lineage=True,  with_error_cols=False)
        _build_table(source_md, entry["source"], cols, ss, with_lineage=False, with_error_cols=False)
        _build_table(error_md,  entry["error"],  cols, es, with_lineage=True,  with_error_cols=True)

    bridge_md.create_all(engine, checkfirst=True)
    source_md.create_all(engine, checkfirst=True)
    error_md.create_all(engine, checkfirst=True)

    # Schema *migration* — `create_all(checkfirst=True)` only creates a
    # table if it doesn't already exist; it never adds new columns to an
    # existing table. When the ServiceNow export catalogue gains a new
    # column between runs (e.g. a re-exported CSV includes an extra field),
    # the freshly-built Table objects above include it, but the physical
    # bridge/source/error tables created by an earlier run do not — so the
    # very next INSERT crashes with `UndefinedColumn`. Reconcile that drift
    # here: only ever ADD columns, never drop/rename/retype, so existing
    # rows and data are preserved exactly; new columns come up NULL for
    # historical rows and populate on the next sync.
    _add_missing_columns(engine, bridge_md)
    _add_missing_columns(engine, source_md)
    _add_missing_columns(engine, error_md)

    # Legacy-table migration: on instances that ran an earlier version of the
    # codegen (pre `<stage>_tbl_<source>_<base>` convention), each stage
    # schema may still hold bare-named TABLES. Those would shadow the new
    # compat VIEWS via search_path, leaving stale data visible to unqualified
    # SQL. Rename them to a quarantine name so the VIEW can take the bare
    # name deterministically, WITHOUT data loss.
    _migrate_legacy_bare_tables(engine, name_map, bs, ss, es)

    # Back-compat: expose bare base-name views so existing rules SQL
    # referencing e.g. "cmdb_ci_win_server" keeps resolving under search_path.
    _create_compat_views(engine, bs, name_map, "bridge")
    _create_compat_views(engine, ss, name_map, "source")
    _create_compat_views(engine, es, name_map, "error")
    log.info(
        "Mirrored %d ServiceNow tables across bridge / source / error (source=%s)",
        len(catalogue), source_name,
    )
    return bridge_md, source_md, error_md, name_map


def truncate_all(engine: Engine, md: MetaData) -> None:
    from sqlalchemy import text as _text
    with engine.begin() as conn:
        for t in md.sorted_tables:
            if md.schema:
                conn.execute(_text(f"DELETE FROM {quote_ident(md.schema)}.{quote_ident(t.name)}"))
            else:
                conn.execute(_text(f"DELETE FROM {quote_ident(t.name)}"))
