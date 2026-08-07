"""
Microsoft 365 API connector -- standalone bridge extraction.

collect_data(credentials, logger) authenticates against the source API
using credentials["api"], calls every endpoint below, and loads the
responses into the bridge.* tables using credentials["bridge"] for the
connection string and row metadata. Every print() is mirrored by a
logger.info() call with the identical message.

Run via: python test_bridge_extraction.py
"""
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("This script requires 'requests'. Install with: pip install requests")

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    raise SystemExit("This script requires 'psycopg2'. Install with: pip install psycopg2-binary")

CONNECTOR_NAME = 'Microsoft 365'
AUTH_TYPE = 'oauth2'
DEFAULT_BASE_URL = 'https://graph.microsoft.com/v1.0'
TOKEN_ENDPOINT = 'https://login.microsoftonline.com/{MSGRAPH_TENANT_ID}/oauth2/v2.0/token'
# Used when UI/credentials omit api.scope (CLIENT_SETUP_GUIDE default).
DEFAULT_SCOPE = 'https://graph.microsoft.com/.default'
REQUEST_TIMEOUT = 60
MAX_PAGES = 50
BATCH_SIZE = 1000
ENDPOINTS = [
    {
        "method": "GET",
        "path": "/subscribedSkus",
        "bridge_table": "tbl_microsoft_365_subscribedskus",
        "array_key": "",
        "query_parameters": [],
        "fields": [
            {
                "column": "grouptype",
                "response_key": "groupType"
            },
            {
                "column": "displayname",
                "response_key": "displayName"
            }
        ]
    }
]

# Path placeholders like {tenant_id} / {subscriptionId} are resolved at runtime
# from credentials["api"] (DB/UI values via the platform pipeline). Do NOT bake
# credentials.json placeholders into TOKEN_ENDPOINT / ENDPOINTS at import time.


def _join(base, path):
    if not path:
        return base
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_refs(raw):
    """Expand ${ENV_VAR} tokens against the process environment."""
    if not raw or "${" not in raw:
        return raw
    return _ENV_REF_RE.sub(lambda m: os.environ.get(m.group(1), ""), raw)


def _camel_to_upper_snake(name):
    """subscriptionId -> SUBSCRIPTION_ID, IPAddress -> IP_ADDRESS."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.upper()


def _resolve_path_param(name):
    """Resolve a {pathParam} from the environment (literal / UPPER / snake)."""
    for candidate in (name, name.upper(), _camel_to_upper_snake(name)):
        val = os.environ.get(candidate)
        if val:
            return val
    return None


def _substitute_path(url):
    """Replace {paramName} placeholders from the environment; fail loudly."""
    out = _PATH_PARAM_RE.sub(
        lambda m: _resolve_path_param(m.group(1)) or m.group(0), url
    )
    leftovers = _PATH_PARAM_RE.findall(out)
    if leftovers:
        unique = sorted(set(leftovers))
        hints = ", ".join(
            n + " (set env " + _camel_to_upper_snake(n) + " or " + n.upper() + ")"
            for n in unique
        )
        raise RuntimeError(
            "Unresolved path placeholder(s) in URL '" + url + "': " + str(unique) +
            ". Provide each via an environment variable. Suggestions: " + hints
        )
    return out


def _resolve_cred_path_vars(template, api_creds):
    """Replace {variable} placeholders in *template* using values from *api_creds*.

    Used at the start of collect_data() to resolve path variables (e.g.
    {tenant_id}, {subscriptionId}) in base_url, TOKEN_ENDPOINT, and
    ENDPOINTS[*].path from the api section of credentials.json.

    Lookup order for each placeholder name:
      1. Direct match            e.g. tenant_id     -> api_creds['tenant_id']
      2. camelCase -> snake_case e.g. subscriptionId -> api_creds['subscription_id']
      3. snake_case -> camelCase e.g. tenant_id      -> api_creds['tenantId']

    Unresolved placeholders are left as-is so downstream errors surface clearly.
    """
    if not template or '{' not in template:
        return template

    def _lookup(name):
        if name in api_creds:
            return str(api_creds[name])
        snake = re.sub(r'([A-Z])', r'_\1', name).lstrip('_').lower()
        if snake in api_creds:
            return str(api_creds[snake])
        parts = name.lower().split('_')
        camel = parts[0] + ''.join(p.capitalize() for p in parts[1:])
        if camel in api_creds:
            return str(api_creds[camel])
        return '{' + name + '}'

    return _PATH_PARAM_RE.sub(lambda m: _lookup(m.group(1)), template)


def _build_params(endpoint):
    """Assemble the query-string dict from the endpoint's declared params.

    Empty values are omitted (server default applies); type == "array" splits a
    comma-separated value into a list; ${ENV_VAR} tokens resolve at call time so
    secrets stay out of the source.
    """
    out = {}
    for p in (endpoint.get("query_parameters") or []):
        name = (p.get("name") or "").strip()
        if not name:
            continue
        raw = p.get("value")
        if raw is None:
            continue
        sval = _resolve_env_refs(str(raw).strip())
        if not sval:
            continue
        if (p.get("type") or "").lower() == "array":
            items = [s.strip() for s in sval.split(",") if s.strip()]
            if items:
                out[name] = items
        else:
            out[name] = sval
    return out


def _get_path(obj, path):
    """Resolve a dotted response key (ignoring [] markers) against a payload."""
    if not path:
        return obj
    cur = obj
    for part in path.replace("[]", "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _extract_rows(payload, array_key):
    arr = _get_path(payload, array_key) if array_key else payload
    if isinstance(arr, list):
        return arr
    if isinstance(arr, dict):
        return [arr]
    return []


def _next_url(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("@odata.nextLink", "nextLink", "next"):
        val = payload.get(key)
        if val:
            return val
    links = payload.get("links")
    if isinstance(links, dict) and links.get("next"):
        return links["next"]
    return None


def _fetch_oauth_token(api):
    token_url = api.get("token_url") or TOKEN_ENDPOINT
    if not token_url:
        raise ValueError("OAuth2 selected but no token_url/TOKEN_ENDPOINT available")
    token_url = _resolve_cred_path_vars(token_url, api)
    if "YOUR_" in token_url or "{" in token_url:
        raise ValueError(
            "Token URL still contains unresolved placeholders. "
            "Ensure api.tenant_id is provided from DB/UI credentials. "
            "URL=" + token_url
        )
    if api.get("refresh_token"):
        data = {
            "grant_type": "refresh_token",
            "refresh_token": api["refresh_token"],
            "client_id": api.get("client_id", ""),
            "client_secret": api.get("client_secret", ""),
        }
    else:
        data = {
            "grant_type": "client_credentials",
            "client_id": api.get("client_id", ""),
            "client_secret": api.get("client_secret", ""),
        }
    # Prefer api.scope from UI/credentials; fall back to connector default scope.
    # Omit the param entirely when both are empty -- some token endpoints
    # reject an explicit empty scope with invalid_scope.
    scope = api.get("scope") or DEFAULT_SCOPE
    if scope:
        data["scope"] = scope
    resp = requests.post(token_url, data=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("Token endpoint returned no access_token")
    return token


def _build_auth(api):
    """Return (headers, auth) for requests based on AUTH_TYPE."""
    headers = {"Accept": "application/json"}
    auth = None
    if AUTH_TYPE == "oauth2":
        headers["Authorization"] = "Bearer " + _fetch_oauth_token(api)
    elif AUTH_TYPE == "basic":
        auth = (api["username"], api["password"])
    elif AUTH_TYPE == "apikey":
        header_name = api.get("api_key_header") or "Authorization"
        headers[header_name] = api["api_key"]
    else:  # bearer (default)
        headers["Authorization"] = "Bearer " + api["api_token"]
    return headers, auth


def _fetch_endpoint(headers, auth, base_url, endpoint):
    method = (endpoint.get("method") or "GET").upper()
    url = _substitute_path(_join(base_url, endpoint["path"]))
    base_params = _build_params(endpoint)
    rows = []
    pages = 0
    use_next_url = False
    while url and pages < MAX_PAGES:
        params = None if use_next_url else base_params
        resp = requests.request(
            method, url, headers=headers, auth=auth,
            params=params, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows.extend(_extract_rows(payload, endpoint["array_key"]))
        nxt = _next_url(payload)
        if nxt:
            url = nxt if nxt.startswith(("http://", "https://")) else _join(base_url, nxt)
            use_next_url = True
        else:
            url = None
        pages += 1
    return rows


def _load_bridge(conn, schema, endpoint, rows, ctx):
    """Insert extracted rows into a bridge.* table.

    Every row carries the mandatory metadata columns from ``ctx`` (sourced
    from credentials["bridge"] plus the per-run execution_id/timestamps):
    tenant_id, connector_instance_id, execution_id, created_by, updated_by,
    created_on, updated_on.
    """
    fields = endpoint["fields"]
    if not rows or not fields:
        return 0
    meta_cols = [
        "tenant_id", "connector_instance_id", "execution_id",
        "created_by", "updated_by", "created_on", "updated_on", "is_deleted",
    ]
    biz_cols = [f["column"] for f in fields]
    all_cols = meta_cols + biz_cols
    values = []
    for row in rows:
        rec = [
            ctx["tenant_id"], ctx["connector_instance_id"], ctx["execution_id"],
            ctx["created_by"], ctx["updated_by"], ctx["created_on"], ctx["updated_on"],
            False,
        ]
        for f in fields:
            val = _get_path(row, f["response_key"])
            if isinstance(val, (dict, list)):
                val = json.dumps(val)
            rec.append(val)
        values.append(rec)
    col_sql = ", ".join('"' + c + '"' for c in all_cols)
    table = schema + "." + endpoint["bridge_table"]
    sql = "INSERT INTO " + table + " (" + col_sql + ") VALUES %s ON CONFLICT DO NOTHING"
    with conn.cursor() as cur:
        execute_values(cur, sql, values, page_size=BATCH_SIZE)
    conn.commit()
    return len(values)


def _default_logger():
    """Fallback logger used only when the platform does not supply one.

    collect_data(credentials, logger) expects the platform to pass its own
    logger at runtime. This fallback exists solely so this script still runs
    standalone (via __main__ below, or test_bridge_extraction.py, which calls
    collect_data(credentials) with a single argument).
    """
    lg = logging.getLogger(CONNECTOR_NAME)
    if not lg.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg


def collect_data(credentials, logger=None):
    """Authenticate, call every endpoint, and load rows into the bridge tables.

    This is the platform contract entry point. ``credentials`` is a runtime
    object supplied by the platform (NOT the contents of credentials.json):

        {
            "api": { ... },     # API auth fields -- used exclusively for
                                 # authenticating and calling source endpoints
            "bridge": { ... },  # bridge DB connection + row metadata --
                                 # used exclusively for the bridge.* insert
        }

    Three responsibilities:
      1. Data Extraction  -- authenticate and call every source API endpoint
         using credentials["api"].
      2. Data Insertion   -- insert fetched rows into bridge.* tables using
         credentials["bridge"] for the DB connection and metadata columns
         (tenant_id, connector_instance_id, execution_id, created_by,
         updated_by, created_on, updated_on) on every row.
      3. Logging          -- every print() is mirrored by logger.info() with
         the identical message.

    Args:
        credentials: runtime dict with "api" and "bridge" sections (see above).
        logger: platform-supplied logger. Falls back to a local logger when
            omitted (e.g. when invoked as `collect_data(credentials)` by
            test_bridge_extraction.py or run standalone via __main__).

    Returns:
        (status: bool, message: str)
    """
    if logger is None:
        logger = _default_logger()

    # Step 1: Always executed -- API auth + data extraction using credentials["api"].
    api = credentials.get("api") or {}
    bridge = credentials.get("bridge") or {}
    has_bridge = bool(bridge)
    base_url = _resolve_cred_path_vars(
        (api.get("base_url") or DEFAULT_BASE_URL or "").rstrip("/"), api
    )
    if not base_url:
        return False, "Missing api.base_url in credentials.json"

    try:
        headers, auth = _build_auth(api)
    except KeyError as exc:
        return False, "Missing API credential key: " + str(exc)
    except Exception as exc:
        return False, "Authentication failed: " + str(exc)

    # Step 2: Only set up bridge insertion if credentials["bridge"] is present.
    conn = None
    schema = bridge.get("schema") or "bridge"
    if has_bridge:
        conn_str = bridge.get("connection_string")
        if not conn_str:
            return False, "Missing bridge.connection_string in credentials.json"
        now = datetime.now(timezone.utc)
        actor = bridge.get("updated_by") or "vendor_smoke_test"
        ctx = {
            "tenant_id": bridge.get("tenant_id"),
            "connector_instance_id": bridge.get("connector_instance_id"),
            "execution_id": str(uuid.uuid4()),
            "created_by": bridge.get("created_by") or actor,
            "updated_by": actor,
            "created_on": now,
            "updated_on": now,
        }
        try:
            conn = psycopg2.connect(conn_str)
        except Exception as exc:
            return False, "Could not connect to bridge database: " + str(exc)
    else:
        print("Bridge credentials not provided -- skipping insertion.")
        logger.info("Bridge credentials not provided -- skipping insertion.")

    total = 0
    failures = []
    try:
        for _ep in ENDPOINTS:
            endpoint = dict(_ep)
            endpoint["path"] = _resolve_cred_path_vars(endpoint.get("path") or "", api)
            try:
                rows = _fetch_endpoint(headers, auth, base_url, endpoint)
                if has_bridge:
                    loaded = _load_bridge(conn, schema, endpoint, rows, ctx)
                    total += loaded
                    print("[OK] " + endpoint["path"] + " -> " + schema + "." +
                          endpoint["bridge_table"] + ": " + str(loaded) + " rows")
                    logger.info("[OK] " + endpoint["path"] + " -> " + schema + "." +
                          endpoint["bridge_table"] + ": " + str(loaded) + " rows")
                else:
                    total += len(rows)
                    print("[OK] " + endpoint["path"] + ": " + str(len(rows)) +
                          " row(s) extracted (insertion skipped)")
                    logger.info("[OK] " + endpoint["path"] + ": " + str(len(rows)) +
                          " row(s) extracted (insertion skipped)")
            except Exception as exc:
                if conn is not None:
                    conn.rollback()
                failures.append(endpoint["path"] + ": " + str(exc))
                print("[ERROR] " + endpoint["path"] + ": " + str(exc))
                logger.info("[ERROR] " + endpoint["path"] + ": " + str(exc))
    finally:
        if conn is not None:
            conn.close()

    if failures:
        return False, (str(len(failures)) + " endpoint(s) failed: " +
                       "; ".join(failures))
    if has_bridge:
        return True, ("Bridge extraction complete: " + str(total) +
                      " row(s) across " + str(len(ENDPOINTS)) + " endpoint(s).")
    return True, ("Extraction complete: " + str(total) +
                  " row(s) across " + str(len(ENDPOINTS)) +
                  " endpoint(s) (bridge insertion skipped).")


if __name__ == "__main__":
    creds_path = Path(__file__).with_name("credentials.json")
    if not creds_path.exists():
        print("[FAIL] credentials.json not found next to main.py")
        _default_logger().info("[FAIL] credentials.json not found next to main.py")
        sys.exit(1)
    creds = json.loads(creds_path.read_text())
    ok, msg = collect_data(creds, _default_logger())
    print(msg)
    _default_logger().info(msg)
    sys.exit(0 if ok else 1)
