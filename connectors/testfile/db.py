"""SQLAlchemy engine factory + small helpers."""
from __future__ import annotations
import re
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def make_engine(connection_string: str) -> Engine:
    return create_engine(connection_string, future=True, pool_pre_ping=True)


@contextmanager
def begin(engine: Engine) -> Iterator:
    with engine.begin() as conn:
        yield conn


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return f'"{name}"'


def fq(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def supports_schemas(engine: Engine) -> bool:
    """SQLite has no real schema concept — return False so callers can skip
    CREATE SCHEMA / schema-qualified DDL on it."""
    return engine.dialect.name != "sqlite"
