"""Thin httpx wrapper with retries and 429 back-off."""
from __future__ import annotations
import logging
import time
from typing import Any

import httpx

import config
from auth import AuthProvider

log = logging.getLogger(__name__)


class HttpClient:
    def __init__(self, auth: AuthProvider | None = None) -> None:
        self.auth = auth or AuthProvider()
        self._client = httpx.Client(timeout=config.REQUEST_TIMEOUT, follow_redirects=True)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def request(self, method: str, url: str, *,
                params: dict | None = None,
                json_body: Any | None = None) -> dict:
        headers = {"Accept": "application/json", **self.auth.header()}
        # Inject static query-string token (e.g. BigFix `?token=...`) on
        # every call. Endpoint-level params win on key collision.
        if config.API_QUERY_TOKEN:
            merged = {config.API_QUERY_TOKEN_NAME: config.API_QUERY_TOKEN}
            if params:
                merged.update(params)
            params = merged
        for attempt in range(1, config.HTTP_RETRIES + 1):
            try:
                resp = self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.RequestError as e:
                log.warning("HTTP transport error (attempt %d): %s", attempt, e)
                if attempt >= config.HTTP_RETRIES:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                log.warning("429 throttled — sleeping %ds (attempt %d)", retry_after, attempt)
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500 and attempt < config.HTTP_RETRIES:
                log.warning("Server %d (attempt %d) — backing off", resp.status_code, attempt)
                time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code >= 400:
                log.error("HTTP %d on %s %s: %s",
                          resp.status_code, method, url, resp.text[:500])
                resp.raise_for_status()
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                return {"_raw": resp.text}
        raise RuntimeError(f"Exhausted retries for {method} {url}")
