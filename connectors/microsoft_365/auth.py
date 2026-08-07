"""HTTP authentication helpers.

Supported flows (resolved in order — first one with credentials wins):
  1. Static Bearer token       (AUTH_TOKEN env var)
  2. HTTP Basic Auth           (BASIC_AUTH_USER + BASIC_AUTH_PASSWORD)
  3. OAuth2 client credentials (TENANT_ID + CLIENT_ID + CLIENT_SECRET)
     – auto-discovers the token endpoint for Azure AD when TENANT_ID is set.

Query-string tokens (e.g. BigFix `?token=...`) are handled separately
by HttpClient via config.API_QUERY_TOKEN; they are appended as a query
parameter on every request and are independent of the Authorization header.
"""
from __future__ import annotations
import base64
import logging
import time
from typing import Optional

import httpx

import config

log = logging.getLogger(__name__)


class AuthenticationError(RuntimeError):
    """Raised when no usable credentials are configured."""


class AuthProvider:
    """Returns a fresh `Authorization` header value on demand."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        # One-shot guard so the auth-method log line fires exactly once
        # per process (helps debug "why is my OAuth connector doing Basic
        # Auth?" without spamming on every request).
        self._announced: bool = False

    def _resolve_token_url(self) -> str:
        if config.OAUTH_TOKEN_URL:
            return config.OAUTH_TOKEN_URL
        if config.TENANT_ID:
            return f"https://login.microsoftonline.com/{config.TENANT_ID}/oauth2/v2.0/token"
        raise AuthenticationError(
            "No OAUTH_TOKEN_URL configured and TENANT_ID is empty — cannot acquire token"
        )

    def _fetch_oauth_token(self) -> str:
        url = self._resolve_token_url()
        log.info("Acquiring OAuth2 token from %s", url)
        resp = httpx.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
                "scope": config.OAUTH_SCOPE,
            },
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthenticationError(f"Token endpoint returned no access_token: {body}")
        # Refresh 60s early.
        self._expires_at = time.time() + max(60, int(body.get("expires_in", 3600)) - 60)
        return token

    def _basic_header(self) -> dict[str, str]:
        raw = f"{config.BASIC_AUTH_USER}:{config.BASIC_AUTH_PASSWORD}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    def header(self) -> dict[str, str]:
        # 1. Pre-issued bearer token wins (zero round-trip).
        if config.AUTH_TOKEN:
            if not self._announced:
                log.info("Auth: using pre-issued bearer token (AUTH_TOKEN)")
                self._announced = True
            return {"Authorization": f"Bearer {config.AUTH_TOKEN}"}
        # 2. HTTP Basic Auth — common for on-premise tools (BigFix, JAMF, etc.)
        if config.BASIC_AUTH_USER and config.BASIC_AUTH_PASSWORD:
            if not self._announced:
                log.info("Auth: using HTTP Basic Auth (BASIC_AUTH_USER + BASIC_AUTH_PASSWORD)")
                self._announced = True
            return self._basic_header()
        # 3. OAuth2 client credentials — fetched lazily, cached until expiry.
        if config.CLIENT_ID and config.CLIENT_SECRET:
            if not self._announced:
                log.info("Auth: using OAuth2 client credentials (CLIENT_ID + CLIENT_SECRET)")
                self._announced = True
            if not self._token or time.time() >= self._expires_at:
                self._token = self._fetch_oauth_token()
            return {"Authorization": f"Bearer {self._token}"}
        # 4. No header-based auth — query-string token (config.API_QUERY_TOKEN)
        # is the only auth left, applied by HttpClient. If neither is set,
        # warn loudly so misconfigured deployments fail fast on first call.
        if not self._announced:
            if config.API_QUERY_TOKEN:
                log.info("Auth: using query-string token only (API_QUERY_TOKEN)")
            else:
                log.warning("No auth credentials configured — calls will be unauthenticated")
            self._announced = True
        return {}
