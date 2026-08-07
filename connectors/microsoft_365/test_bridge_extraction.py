"""
Pre-deployment bridge smoke test for the Microsoft 365 API connector.

Reads credentials.json, resolves any {variable} path placeholders in
TOKEN_ENDPOINT and endpoint paths using credential values, calls
main.collect_data(credentials), and reports pass/fail.

Fill in credentials.json first, then run:
    python test_bridge_extraction.py
"""
import json
import logging
import re
import sys
from pathlib import Path

CREDENTIALS_FILE = Path(__file__).with_name("credentials.json")
_PATH_VAR_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')

logger = logging.getLogger('Microsoft 365')
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _has_placeholder(obj):
    """True if any value still contains a YOUR_*_HERE placeholder."""
    if isinstance(obj, dict):
        return any(_has_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_placeholder(v) for v in obj)
    return isinstance(obj, str) and "YOUR_" in obj


def _resolve_path_vars(template, creds):
    """Replace {variable} placeholders in *template* using values from *creds*.

    Lookup order for each placeholder name:
      1. Direct match            e.g. tenant_id  -> creds['tenant_id']
      2. camelCase -> snake_case e.g. subscriptionId -> creds['subscription_id']
      3. snake_case -> camelCase e.g. tenant_id -> creds['tenantId']

    Unresolved placeholders are left as-is so the error surfaces clearly.
    """
    if not template or '{' not in template:
        return template

    def _lookup(name):
        if name in creds:
            return str(creds[name])
        snake = re.sub(r'([A-Z])', r'_\1', name).lstrip('_').lower()
        if snake in creds:
            return str(creds[snake])
        parts = name.lower().split('_')
        camel = parts[0] + ''.join(p.capitalize() for p in parts[1:])
        if camel in creds:
            return str(creds[camel])
        return '{' + name + '}'

    return _PATH_VAR_RE.sub(lambda m: _lookup(m.group(1)), template)


def _patch_main_path_vars(connector_main, api_creds):
    """Resolve {variable} placeholders in the imported main module's
    TOKEN_ENDPOINT, DEFAULT_BASE_URL, and ENDPOINTS[*].path using values
    from the api section of credentials.json.

    This is necessary because main.py resolves path params from environment
    variables at runtime, but the smoke test uses credentials.json instead.
    """
    if getattr(connector_main, 'TOKEN_ENDPOINT', None):
        connector_main.TOKEN_ENDPOINT = _resolve_path_vars(
            connector_main.TOKEN_ENDPOINT, api_creds
        )
    if getattr(connector_main, 'DEFAULT_BASE_URL', None):
        connector_main.DEFAULT_BASE_URL = _resolve_path_vars(
            connector_main.DEFAULT_BASE_URL, api_creds
        )
    for ep in getattr(connector_main, 'ENDPOINTS', []):
        if isinstance(ep.get('path'), str):
            ep['path'] = _resolve_path_vars(ep['path'], api_creds)


def main():
    print("Microsoft 365 bridge extraction smoke test")
    logger.info("Microsoft 365 bridge extraction smoke test")
    print("=" * 60)
    logger.info("=" * 60)

    if not CREDENTIALS_FILE.exists():
        print("[FAIL] credentials.json not found next to this script.")
        logger.info("[FAIL] credentials.json not found next to this script.")
        return 1

    try:
        credentials = json.loads(CREDENTIALS_FILE.read_text())
    except Exception as exc:
        print("[FAIL] Could not parse credentials.json: " + str(exc))
        logger.info("[FAIL] Could not parse credentials.json: " + str(exc))
        return 1

    if _has_placeholder(credentials):
        print("[FAIL] Replace all YOUR_*_HERE placeholders in credentials.json before running.")
        logger.info("[FAIL] Replace all YOUR_*_HERE placeholders in credentials.json before running.")
        return 1

    try:
        import main as connector_main
    except Exception as exc:
        print("[FAIL] Could not import main.py: " + str(exc))
        logger.info("[FAIL] Could not import main.py: " + str(exc))
        return 1

    # Resolve {variable} path placeholders in TOKEN_ENDPOINT, DEFAULT_BASE_URL,
    # and ENDPOINTS[*].path using values from the api section of credentials.json.
    api_creds = credentials.get('api', credentials)
    _patch_main_path_vars(connector_main, api_creds)

    # This smoke test covers API authentication and data extraction only --
    # bridge insertion is intentionally skipped by omitting the 'bridge' key,
    # so collect_data() takes its no-bridge branch regardless of what may be
    # present in credentials.json.
    status, message = connector_main.collect_data({'api': api_creds})
    print(message)
    logger.info(message)
    print("[PASS]" if status else "[FAIL]")
    logger.info("[PASS]" if status else "[FAIL]")
    return 0 if status else 1


if __name__ == "__main__":
    sys.exit(main())
