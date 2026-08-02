"""Whoop API v2 HTTP client with pagination and backoff."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from whoop_claude_sync.auth import TokenStore, get_valid_token, refresh_access_token
from whoop_claude_sync.config import WHOOP_API_BASE, Settings


class WhoopClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = TokenStore(settings.token_path)
        self._token = get_valid_token(settings)
        self._client = httpx.Client(
            base_url=WHOOP_API_BASE,
            timeout=45.0,
            headers={
                "Accept": "application/json",
                # Cloudflare can block default Python user-agents.
                "User-Agent": "whoop-claude-sync/0.1 (+local personal sync)",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WhoopClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _ensure_token(self) -> str:
        # Reload in case another process rotated refresh token (best effort).
        loaded = self.store.load()
        if loaded:
            self._token = loaded
        self._token = get_valid_token(self.settings)
        return self._token["access_token"]

    def _request(self, method: str, path: str, params: dict | None = None) -> dict[str, Any]:
        for attempt in range(6):
            token = self._ensure_token()
            resp = self._client.request(
                method,
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401 and attempt < 2:
                self._token = refresh_access_token(self.settings, self._token)
                self.store.save(self._token)
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(retry_after, 60))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Whoop API {method} {path} → {resp.status_code}: {resp.text}")
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        raise RuntimeError(f"Whoop API gave up after retries: {method} {path}")

    def paginate(
        self,
        path: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 25,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"limit": min(limit, 25)}
        if start:
            params["start"] = _iso(start)
        if end:
            params["end"] = _iso(end)
        next_token: str | None = None
        while True:
            page_params = dict(params)
            if next_token:
                # Whoop docs / clients use both casings; send nextToken.
                page_params["nextToken"] = next_token
            data = self._request("GET", path, params=page_params)
            records = data.get("records") or []
            for record in records:
                yield record
            next_token = data.get("next_token") or data.get("nextToken")
            if not next_token:
                break

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", "/v2/user/profile/basic")

    def get_body_measurement(self) -> dict[str, Any]:
        return self._request("GET", "/v2/user/measurement/body")

    def iter_recoveries(self, start: datetime | None = None, end: datetime | None = None):
        return self.paginate("/v2/recovery", start=start, end=end)

    def iter_sleeps(self, start: datetime | None = None, end: datetime | None = None):
        return self.paginate("/v2/activity/sleep", start=start, end=end)

    def iter_cycles(self, start: datetime | None = None, end: datetime | None = None):
        return self.paginate("/v2/cycle", start=start, end=end)

    def iter_workouts(self, start: datetime | None = None, end: datetime | None = None):
        return self.paginate("/v2/activity/workout", start=start, end=end)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
