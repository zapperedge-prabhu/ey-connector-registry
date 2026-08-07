"""Runtime configuration loaded from environment variables.

Authentication resolution order:
  1. AUTH_TOKEN          — pre-issued bearer token
  2. OAuth2 client credentials (TENANT_ID, CLIENT_ID, CLIENT_SECRET, SCOPE)

The target warehouse holding Bridge/Source/Error/ETL schemas is REQUIRED
(TARGET_CONNECTION_STRING). There is no fallback — the connector must
not accidentally write Bridge data into the source API host.
"""
from __future__ import annotations
import os


def _first(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


# Source API
# Per-instance / on-premise tools (BigFix, ServiceNow on-prem, etc.) can
# either set API_BASE_URL directly to the fully-resolved URL
# (e.g. https://bigfix.acme.com:9443) OR set HOSTNAME + PORT and let the
# config build it. The fully-resolved form always wins.
_HOSTNAME       = _first("HOSTNAME", "API_HOSTNAME")
_PORT           = _first("PORT", "API_PORT")
_BASE_FROM_PARTS = (
    f"https://{_HOSTNAME}:{_PORT}" if _HOSTNAME and _PORT
    else (f"https://{_HOSTNAME}" if _HOSTNAME else "")
)
API_BASE_URL    = _first("API_BASE_URL", "BASE_URL", default=_BASE_FROM_PARTS).rstrip("/")

# Auth resolution accepts BOTH canonical names AND the SAM Studio
# credential-template field names so the same .env file works whether
# the operator wrote it by hand OR exported it from the Studio UI.
# The Studio aliases come SECOND so an explicitly-set canonical name
# always wins over a Studio default.
AUTH_TOKEN      = _first(
    "AUTH_TOKEN", "BEARER_TOKEN",
    # Studio aliases (Splunk auth_token, Tanium api_token, generic api_key, …)
    "auth_token", "api_token", "access_token", "api_key",
    "AUTH_TOKEN_LOWER", "API_TOKEN", "ACCESS_TOKEN", "API_KEY",
)
# Basic-Auth aliases are kept narrow on purpose: we deliberately do NOT
# accept a bare `USERNAME` / `PASSWORD` because those are commonly set by
# the host OS (PAM, container runtime) for unrelated reasons and could
# silently flip a Bearer/OAuth2 deployment into Basic Auth.
BASIC_AUTH_USER     = _first(
    "BASIC_AUTH_USER", "API_USERNAME",
    # Studio aliases (AutoRabit/Cority/Anaplan/etc. -> "username",
    # Celtra -> "api_username", Anomali -> "username").
    "username", "api_username",
)
BASIC_AUTH_PASSWORD = _first(
    "BASIC_AUTH_PASSWORD", "API_PASSWORD",
    "password", "api_secret",
)
# Query-string token (e.g. BigFix `?token=...`). When set, the HttpClient
# appends it as `<API_QUERY_TOKEN_NAME>=<API_QUERY_TOKEN>` to every request.
API_QUERY_TOKEN      = _first("API_QUERY_TOKEN")
API_QUERY_TOKEN_NAME = _first("API_QUERY_TOKEN_NAME", default="token")
TENANT_ID       = _first("TENANT_ID", "AZURE_TENANT_ID", "tenant_id")
CLIENT_ID       = _first(
    "CLIENT_ID", "AZURE_CLIENT_ID",
    "client_id", "api_id",   # Cornerstone uses api_id
)
CLIENT_SECRET   = _first(
    "CLIENT_SECRET", "AZURE_CLIENT_SECRET",
    "client_secret", "api_secret",
)
OAUTH_SCOPE     = _first("OAUTH_SCOPE", "AZURE_SCOPE", "scope",
                         default="https://management.azure.com/.default")
OAUTH_TOKEN_URL = _first("OAUTH_TOKEN_URL", "token_url")
SUBSCRIPTION_ID = _first("SUBSCRIPTION_ID", "AZURE_SUBSCRIPTION_ID", "subscription_id")

# Path-parameter substitutions (e.g. {subscriptionId} → SUBSCRIPTION_ID).
# Per-instance tokens like {hostname} / {port} are resolved at the
# API_BASE_URL level above (not via PATH_PARAMS), so the registry can keep
# storing template URLs while runtime uses the resolved value.
PATH_PARAMS = {
    "subscriptionId": SUBSCRIPTION_ID,
    "subscription_id": SUBSCRIPTION_ID,
    "tenantId": TENANT_ID,
    "tenant_id": TENANT_ID,
    "hostname": _HOSTNAME,
    "port": _PORT,
}

# Target warehouse holding Bridge / Source / Error / ETL schemas.
TARGET_CONNECTION_STRING = os.environ.get("TARGET_CONNECTION_STRING", "").strip()
if not TARGET_CONNECTION_STRING:
    raise RuntimeError(
        "TARGET_CONNECTION_STRING is not set — provide a PostgreSQL SQLAlchemy URL "
        "for the warehouse holding the Bridge / Source / Error / ETL schemas. "
        "Example: postgresql://user:pass@host:5432/warehouse"
    )

BRIDGE_SCHEMA        = os.environ.get("BRIDGE_SCHEMA", "bridge")
TARGET_SOURCE_SCHEMA = os.environ.get("TARGET_SOURCE_SCHEMA", "source")
ERROR_SCHEMA         = os.environ.get("ERROR_SCHEMA",  "error")

# Short upstream-system token baked into physical table names:
#   <stage>_tbl_<SOURCE_NAME>_<base>
SOURCE_NAME = os.environ.get("SOURCE_NAME", "microsoft_365")

# Pagination / rate-limit guardrails
MAX_PAGES        = int(os.environ.get("MAX_PAGES",        "200"))
PAGE_SIZE        = int(os.environ.get("PAGE_SIZE",        "100"))
REQUEST_TIMEOUT  = float(os.environ.get("REQUEST_TIMEOUT", "30"))
HTTP_RETRIES     = int(os.environ.get("HTTP_RETRIES",     "3"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def _base_url_for(key: str) -> str:
    """Return the base URL for a named service domain (multi-domain connectors).

    Multi-domain connectors (e.g. AWS with IAM, S3, EC2 on separate hosts) set
    ``BASE_URL_<KEY>=`` in ``.env`` instead of a single ``API_BASE_URL``.
    This helper reads the per-service var and falls back to ``API_BASE_URL`` for
    single-domain connectors or when the key-specific var is not set.

    Examples::

        _base_url_for("iam")  -> os.environ["BASE_URL_IAM"]  or API_BASE_URL
        _base_url_for("s3")   -> os.environ["BASE_URL_S3"]   or API_BASE_URL
    """
    env_var = f"BASE_URL_{(key or 'DEFAULT').upper()}"
    return os.environ.get(env_var, "").rstrip("/") or API_BASE_URL
