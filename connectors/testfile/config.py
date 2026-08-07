"""Runtime configuration loaded from environment variables.

Required:
  SOURCE_DIR                   — absolute path to the CSV export directory.

Required:
  TARGET_CONNECTION_STRING     — SQLAlchemy URL of the PostgreSQL warehouse
                                 holding the Bridge / Source / Error schemas.
                                 Example:
                                   postgresql://user:pass@host:5432/warehouse

Optional:
  BRIDGE_SCHEMA / TARGET_SOURCE_SCHEMA / ERROR_SCHEMA  (defaults: bridge / source / error)
"""
from __future__ import annotations
import os


SOURCE_DIR = os.environ.get("SOURCE_DIR", "").strip()
if not SOURCE_DIR:
    raise RuntimeError(
        "SOURCE_DIR is not set — point it at the CSV export directory."
    )

TARGET_CONNECTION_STRING = os.environ.get("TARGET_CONNECTION_STRING", "").strip()
if not TARGET_CONNECTION_STRING:
    raise RuntimeError(
        "TARGET_CONNECTION_STRING is not set — provide a PostgreSQL SQLAlchemy URL "
        "for the warehouse holding the Bridge / Source / Error schemas."
    )

BRIDGE_SCHEMA        = os.environ.get("BRIDGE_SCHEMA", "bridge")
TARGET_SOURCE_SCHEMA = os.environ.get("TARGET_SOURCE_SCHEMA", "source")
ERROR_SCHEMA         = os.environ.get("ERROR_SCHEMA",  "error")

# Short upstream-system token baked into physical table names:
#     <stage>_tbl_<SOURCE_NAME>_<base>
SOURCE_NAME = os.environ.get("SOURCE_NAME", "testfile")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
