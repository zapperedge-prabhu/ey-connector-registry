"""SQLAlchemy engine factory + small helpers (PostgreSQL-only target)."""
from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text  # noqa: F401
from sqlalchemy.engine import Engine


def make_engine(connection_string: str) -> Engine:
    return create_engine(connection_string, future=True, pool_pre_ping=True)


@contextmanager
def begin(engine: Engine) -> Iterator:
    with engine.begin() as conn:
        yield conn


def quote_ident(name: str) -> str:
    import re
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return f'"{name}"'


def fq(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"
