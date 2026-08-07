"""
HTTP extractor — walks each endpoint in `endpoints.json`, paginates, and
flattens responses into row dicts ready for Bridge insertion.

Each yielded row carries:
  __id          best-effort identifier (`id` or `name` from the payload)
  __fetched_at  UTC timestamp
  __row         the full response item as a Python dict
  <field>       string-coerced value extracted via the field's response_key
"""
from __future__ import annotations
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterator

import config
from http_client import HttpClient

log = logging.getLogger(__name__)


_ENV_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


# ─── Azure ARM api-version auto-injection ──────────────────────────────────
# Azure Resource Manager requires `?api-version=…` on EVERY request or
# the server returns HTTP 400 "MissingApiVersionParameter" — the exact
# failure mode the analyst sees in the live test runner when the
# discovery LLM forgot to extract `api-version` for an ARM endpoint.
# The vendor corpus directive and the wizard tester's auto-injection
# both already guard this at design time, but a connector that was
# generated BEFORE those guards landed (or whose endpoints.json is
# hand-edited) can still ship without `api-version` and silently 400
# every endpoint in production. Mirror the same allowlist + injection
# semantics as `services/endpoint_tester._apply_vendor_required_query_params`
# so the generated extractor is the third defence-in-depth layer.
#
# Host detection is host-bound (urlsplit + suffix match), never a
# substring scan, so lookalike attacker hosts like
# `management.azure.com.evil.com` cannot trigger silent injection of
# ARM-only query parameters into a third-party request.
_AZURE_ARM_HOSTS = (
    "management.azure.com",            # public Azure (commercial)
    "management.usgovcloudapi.net",    # Azure US Government
    "management.chinacloudapi.cn",     # Azure operated by 21Vianet (China)
    "management.microsoftazure.de",    # Azure Germany (legacy but still served)
)
_AZURE_ARM_DEFAULT_API_VERSION = "2021-04-01"


def _is_azure_arm_host(url: str) -> bool:
    """True iff ``url``'s host is an Azure Resource Manager endpoint in
    any sovereign cloud (exact host or any subdomain). Returns False
    for relative URLs and unparsable input."""
    if not url:
        return False
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001 — never propagate URL-parse errors
        return False
    if not host:
        return False
    for arm in _AZURE_ARM_HOSTS:
        if host == arm or host.endswith("." + arm):
            return True
    return False


def _ensure_azure_api_version(url: str, params: dict | None) -> dict | None:
    """Auto-inject ``api-version`` on Azure ARM calls where the endpoint's
    declared query parameters omitted it. Returns the (possibly updated)
    params dict.

    Skipped when:
      * URL is not an Azure ARM host.
      * ``params`` is None — that's the nextLink-pagination branch, where
        the request URL was server-supplied and already carries
        ``?api-version=…``; injecting a key into ``params`` would
        double-up the query string.
      * Either ``api-version`` or ``api_version`` is already present
        (case-insensitive) in the analyst-declared params.
      * The URL string itself already carries ``api-version=`` in its
        query string — belt-and-braces guard against double-injection.

    Logs a warning identifying the endpoint so operators see the
    fallback in the connector logs and add ``api-version`` permanently
    via Rework instead of relying on this safety net forever.
    """
    if not _is_azure_arm_host(url):
        return params
    if params is None:
        return params
    out = dict(params)
    present_lc = {k.lower() for k in out.keys()}
    if "api-version" in present_lc or "api_version" in present_lc:
        return out
    # Belt-and-braces: parse the URL query string and check for an
    # actual `api-version` (or `api_version`) key — never a substring
    # match, so URL-encoded path text or a `?ref=foo&api-version-note=bar`
    # cannot suppress legitimate injection. parse_qsl tolerates malformed
    # input by returning an empty list; any exception falls through to
    # injection (safe default — a duplicate key 400 is the same failure
    # mode the analyst already hit, so we don't make it worse).
    try:
        from urllib.parse import urlsplit, parse_qsl
        url_q = urlsplit(url or "").query
        url_keys_lc = {k.lower() for k, _ in parse_qsl(url_q, keep_blank_values=True)}
        if "api-version" in url_keys_lc or "api_version" in url_keys_lc:
            return out
    except Exception:  # noqa: BLE001
        pass
    out["api-version"] = _AZURE_ARM_DEFAULT_API_VERSION
    log.warning(
        "Azure ARM call to %s missing required `api-version` query "
        "parameter — auto-injecting `api-version=%s`. Add it to the "
        "endpoint's Query Parameters in the studio (Rework) so the "
        "extractor sends it explicitly, and override the version if "
        "the resource provider needs a different one.",
        url, _AZURE_ARM_DEFAULT_API_VERSION,
    )
    return out


def _resolve_env_refs(raw: str) -> str:
    """Expand ${ENV_VAR} tokens against process env. Unknown vars resolve to ""."""
    if not raw or "${" not in raw:
        return raw
    return _ENV_REF_RE.sub(lambda m: os.environ.get(m.group(1), ""), raw)


def _build_params(endpoint: dict) -> dict:
    """Assemble the per-request query-string dict from an endpoint's
    declared query_parameters list. Honours analyst-supplied runtime values:

      - empty/whitespace value  → param is omitted (server default applies)
      - type == "array"         → comma-separated value is split into a Python
        list. httpx serialises lists by repeating the key, so
        {"columns[]": ["a","b"]} → "?columns[]=a&columns[]=b" — exactly what
        BigFix and similar APIs require.
      - ${ENV_VAR}              → resolved against os.environ at call time so
        secrets are never baked into the generated source.

    Path / header / body params are routed elsewhere (path substitution,
    HttpClient header injection, request-body builder) and are skipped here.
    """
    out: dict = {}
    for p in (endpoint.get("query_parameters") or []):
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        raw = p.get("value")
        if raw is None:
            continue
        sval = str(raw).strip()
        if not sval:
            continue
        resolved = _resolve_env_refs(sval)
        if not resolved:
            continue
        if (p.get("type") or "").strip().lower() == "array":
            items = [s.strip() for s in resolved.split(",") if s.strip()]
            if items:
                out[name] = items
        else:
            out[name] = resolved
    return out


_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _camel_to_upper_snake(name: str) -> str:
    """`subscriptionId` -> `SUBSCRIPTION_ID`, `IPAddress` -> `IP_ADDRESS`.

    Two-pass split handles both the common camelCase boundary
    (lowercase/digit followed by uppercase) and the acronym boundary
    (uppercase run followed by titlecase word, e.g. `IPAddress` ->
    `IP_Address`, `getURLPath` -> `get_URL_Path`). Without the second
    pass, runs of capitals collapse onto the next word and produce
    incorrect env-var names (`IPADDRESS` instead of `IP_ADDRESS`).

    NOTE: Backslashes in the regex below are doubled because this entire
    function lives inside the outer triple-quoted `_EXTRACTOR_PY` string
    literal — and `\1`/`\2` (single-backslash) inside a non-raw outer
    string would be processed as octal escapes (`\x01`/`\x02`), silently
    corrupting the regex backreferences in the generated extractor.py.
    """
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.upper()


def _resolve_path_param(name: str) -> str | None:
    """Look up a path-parameter value across every supported source.

    Resolution order (first non-empty wins):
      1. `config.PATH_PARAMS[name]` — hardcoded aliases set up at config
         load time (subscriptionId, tenantId, hostname, port).
      2. `os.environ[name]` — the literal placeholder name set as an
         env var (e.g. `location` if the analyst added it directly).
      3. `os.environ[NAME]` — the UPPER form (e.g. `LOCATION`).
      4. `os.environ[NAME_SNAKE]` — camelCase -> UPPER_SNAKE (e.g.
         `subscriptionId` -> `SUBSCRIPTION_ID`, `resourceGroupName` ->
         `RESOURCE_GROUP_NAME`).

    Returns `None` if no source has a non-empty value, signalling the
    placeholder is unresolved and `_substitute_path` should fail loudly.
    """
    static = (config.PATH_PARAMS or {}).get(name)
    if static:
        return str(static)
    for env_key in (name, name.upper(), _camel_to_upper_snake(name)):
        v = os.environ.get(env_key)
        if v:
            return v
    return None


def _substitute_path(url: str) -> str:
    """Replace `{paramName}` placeholders. Fails loudly when unresolved.

    Path parameters are resolved via `_resolve_path_param`, which checks
    `config.PATH_PARAMS` first then falls back to environment variables
    under the literal, UPPER, and camelCase->UPPER_SNAKE forms of the
    placeholder name. Any `{name}` left unresolved after substitution
    raises `RuntimeError` listing the missing names alongside the env
    vars the analyst can set — converting what would otherwise be a
    confusing upstream HTTP 400 from a literal placeholder leaking
    into a real request (e.g. Azure ARM:
    "No registered resource provider found for location '{location}'
    and API version '...'") into a clear, actionable error from our
    own code.
    """
    out = _PATH_PARAM_RE.sub(
        lambda m: _resolve_path_param(m.group(1)) or m.group(0),
        url,
    )
    leftovers = _PATH_PARAM_RE.findall(out)
    if leftovers:
        unique = sorted(set(leftovers))
        suggestions = ", ".join(
            "{n} (set env var {snake} or {upper})".format(
                n=n, snake=_camel_to_upper_snake(n), upper=n.upper()
            )
            for n in unique
        )
        raise RuntimeError(
            "Unresolved path placeholder(s) in URL '" + url + "': "
            + str(unique)
            + ". Provide a value for each missing parameter via the "
            + "connector's credentials or as an environment variable. "
            + "Suggestions: " + suggestions
        )
    return out


def _walk_jsonpath(payload: Any, path: str) -> Any:
    """Best-effort JSON path resolver supporting `a.b.c` and trailing `[]`."""
    if not path or payload is None:
        return payload
    p = path.rstrip("[]")
    cur: Any = payload
    for part in p.split(".") if p else []:
        if cur is None:
            return None
        if isinstance(cur, list):
            # Take the first element when traversing through an array.
            cur = cur[0] if cur else None
            if cur is None:
                return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


_PAGINATION_KEYS = frozenset({
    "nextLink", "@odata.nextLink", "@odata.count", "@odata.context",
    "next", "next_url", "previous", "prev", "prev_url",
    "nextPageToken", "next_page_token", "pageToken", "page_token",
    "continuationToken", "continuation_token", "cursor", "nextCursor", "next_cursor",
    "count", "total", "totalCount", "total_count", "totalSize", "total_size",
    "page", "pageSize", "page_size", "limit", "offset", "skip",
    "hasMore", "has_more", "more",
    "links", "_links", "meta", "_meta", "pagination", "paging",
})


def _extract_array(payload: Any, array_key: str | None) -> list[dict]:
    """Pull the per-row array from the response.

    Resolution order (must stay in sync with studio bridge_extract.py):
      1. `array_key` JSON path if provided.
      2. Top-level `value` / `data` / `results` / `items` / `records`.
      3. Sole non-pagination top-level key whose value is a list
         (e.g. Anaplan's `{"usage": [...], "nextLink": "..."}`).
      4. Payload itself if already a list.
      5. Fallback: wrap the payload in a single-element list.
    """
    if array_key:
        arr = _walk_jsonpath(payload, array_key)
        if isinstance(arr, list):
            return arr
    if isinstance(payload, dict):
        for k in ("value", "data", "results", "items", "records"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
        # Conservative sole-list-key heuristic: only auto-unwrap when
        # the list is non-empty AND its first element is a dict, so we
        # don't false-unwrap object-with-tag-list shapes like
        # `{"id":"x","name":"y","roles":["a","b"]}`.
        list_keys = [
            k for k, v in payload.items()
            if k not in _PAGINATION_KEYS
            and not k.startswith("@odata.")
            and isinstance(v, list)
        ]
        if len(list_keys) == 1:
            arr = payload[list_keys[0]]
            if arr and isinstance(arr[0], dict):
                return arr
    if isinstance(payload, list):
        return payload
    return [payload] if isinstance(payload, dict) else []


def _next_page_url(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for k in ("nextLink", "@odata.nextLink", "next", "next_url"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _extract_field(row: Any, response_key: str) -> str | None:
    """Resolve the response_key against a single row and stringify the result."""
    if not response_key:
        return None
    val = _walk_jsonpath(row, response_key)
    if val is None:
        # Direct attribute lookup as a fallback (some discoveries use
        # plain field names, not JSONPaths).
        if isinstance(row, dict):
            val = row.get(response_key)
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        import json as _json
        return _json.dumps(val, default=str)
    return str(val)


def _extract_id(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    for k in ("id", "Id", "ID", "name", "Name", "uuid", "guid"):
        v = row.get(k)
        if v:
            return str(v)
    return None


def _offset_pagination_state(endpoint: dict, params: dict) -> tuple[str | None, str | None, int]:
    """If the endpoint declares both `limit` and `offset` query parameters,
    return ("limit", "offset", page_size) so the caller can walk pages by
    incrementing offset. Returns (None, None, 0) when not applicable.
    """
    declared = {
        (p.get("name") or "").strip().lower(): p
        for p in (endpoint.get("query_parameters") or [])
        if isinstance(p, dict)
    }
    limit_p = declared.get("limit")
    offset_p = declared.get("offset")
    if not limit_p or not offset_p:
        return None, None, 0
    limit_name = (limit_p.get("name") or "").strip()
    offset_name = (offset_p.get("name") or "").strip()
    try:
        page_size = int(str(params.get(limit_name) or limit_p.get("value") or "1000"))
    except (TypeError, ValueError):
        page_size = 1000
    if page_size <= 0:
        page_size = 1000
    return limit_name, offset_name, page_size


def fetch_endpoint(client: HttpClient, endpoint: dict) -> Iterator[dict]:
    """Yield flattened row dicts for one endpoint, walking pagination.

    Pagination strategy (in priority order):
      1. nextLink/@odata.nextLink/next URL on the response payload.
      2. Declared offset/limit query params — increment offset by page_size
         until a page returns fewer than page_size rows.
      3. Single-page (no advance possible).
    """
    # Resolve the full request URL for this endpoint.
    # Priority order:
    #   1. full_url on the endpoint — already absolute, just substitute path params.
    #   2. base_url_key → read BASE_URL_{KEY} env-var (multi-domain connectors with
    #      separate per-service hosts, e.g. AWS IAM vs S3 vs EC2).
    #   3. config.API_BASE_URL + relative endpoint path (single-domain fallback).
    _raw_url = endpoint.get("full_url") or ""
    if not _raw_url:
        _bkey = (endpoint.get("base_url_key") or "default").upper()
        _benv = os.environ.get(f"BASE_URL_{_bkey}", "").rstrip("/") or config.API_BASE_URL
        _path = endpoint["endpoint"]
        _raw_url = (_benv + "/" + _path.lstrip("/")) if _benv else _path
    base_url = _substitute_path(_raw_url)
    method = (endpoint.get("method") or "GET").upper()
    array_key = endpoint.get("array_key")
    fields = endpoint.get("fields", [])
    base_params = _build_params(endpoint)

    limit_name, offset_name, page_size = _offset_pagination_state(endpoint, base_params)
    if offset_name and offset_name in base_params:
        try:
            current_offset = int(str(base_params[offset_name]))
        except (TypeError, ValueError):
            current_offset = 0
    else:
        current_offset = 0
    if limit_name and page_size:
        base_params[limit_name] = str(page_size)

    next_url: str | None = base_url
    use_next_url = False  # flips True the first time the server hands us a nextLink
    pages = 0
    while next_url and pages < config.MAX_PAGES:
        pages += 1
        if use_next_url:
            req_params: dict | None = None
            req_url = next_url
        else:
            req_params = dict(base_params)
            if offset_name and page_size:
                req_params[offset_name] = str(current_offset)
            req_url = base_url
        # Defence-in-depth: if the endpoint targets an Azure Resource
        # Manager host and the analyst-declared query_parameters didn't
        # include `api-version`, auto-inject it (and warn) so the call
        # doesn't 400 with MissingApiVersionParameter. No-op for the
        # nextLink branch (params is None there) since Azure's
        # server-supplied URL already carries api-version inline.
        req_params = _ensure_azure_api_version(req_url, req_params)
        log.info("Fetching %s %s (page %d)", method, req_url, pages)
        payload = client.request(method, req_url, params=req_params)
        rows = _extract_array(payload, array_key)
        log.info("  → %d rows", len(rows))
        now = datetime.now(timezone.utc)
        for r in rows:
            out: dict = {
                "__id": _extract_id(r),
                "__fetched_at": now,
                "__row": r if isinstance(r, dict) else {"_value": r},
            }
            for f in fields:
                col = (f.get("column") or "").strip()
                if not col:
                    continue
                out[col] = _extract_field(r, f.get("response_key") or "")
            yield out

        nxt = _next_page_url(payload)
        if nxt:
            next_url = nxt
            use_next_url = True
            continue
        if offset_name and page_size and len(rows) >= page_size:
            current_offset += page_size
            next_url = base_url
            use_next_url = False
            continue
        next_url = None
