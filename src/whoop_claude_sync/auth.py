"""Whoop OAuth2 authorization-code flow with rotating refresh tokens."""

from __future__ import annotations

import json
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from whoop_claude_sync.config import WHOOP_AUTH_URL, WHOOP_TOKEN_URL, Settings


class TokenStore:
    """Atomic JSON token persistence. Whoop refresh tokens are single-use."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, token: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = dict(token)
        payload["saved_at"] = int(time.time())
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def build_authorization_url(settings: Settings, state: str | None = None) -> tuple[str, str]:
    state = state or secrets.token_urlsafe(24)
    params = {
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": " ".join(settings.scopes),
        "state": state,
    }
    return f"{WHOOP_AUTH_URL}?{urllib.parse.urlencode(params)}", state


def extract_code(redirect_response: str) -> str:
    """Accept a raw auth code or a full redirect URL containing ?code=."""
    value = redirect_response.strip().strip('"').strip("'")
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urllib.parse.urlparse(value)
        qs = urllib.parse.parse_qs(parsed.query)
        if "error" in qs:
            raise SystemExit(f"Whoop auth error: {qs.get('error')}")
        code = qs.get("code", [None])[0]
        if not code:
            raise SystemExit("No ?code= found in redirect URL.")
        return code
    if "code=" in value and "&" in value:
        qs = urllib.parse.parse_qs(value.lstrip("?"))
        code = qs.get("code", [None])[0]
        if code:
            return code
    return value


def exchange_code(settings: Settings, code: str) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "redirect_uri": settings.redirect_uri,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(WHOOP_TOKEN_URL, data=data)
        if resp.status_code >= 400:
            raise SystemExit(f"Token exchange failed ({resp.status_code}): {resp.text}")
        token = resp.json()
    token["obtained_at"] = int(time.time())
    return token


def refresh_access_token(settings: Settings, token: dict[str, Any]) -> dict[str, Any]:
    refresh = token.get("refresh_token")
    if not refresh:
        raise SystemExit("No refresh_token stored. Re-run: whoop-claude-sync auth")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "scope": " ".join(settings.scopes),
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(WHOOP_TOKEN_URL, data=data)
        if resp.status_code >= 400:
            raise SystemExit(
                f"Token refresh failed ({resp.status_code}): {resp.text}\n"
                "Re-authorize with: whoop-claude-sync auth"
            )
        new_token = resp.json()
    # Preserve any fields Whoop omits on refresh; always take new refresh_token.
    merged = dict(token)
    merged.update(new_token)
    if "refresh_token" not in new_token:
        # Should not happen with offline scope, but keep old only if absent.
        merged["refresh_token"] = refresh
    merged["obtained_at"] = int(time.time())
    return merged


def access_token_expired(token: dict[str, Any], skew_seconds: int = 120) -> bool:
    obtained = int(token.get("obtained_at") or token.get("saved_at") or 0)
    expires_in = int(token.get("expires_in") or 3600)
    if not obtained:
        return True
    return time.time() >= (obtained + expires_in - skew_seconds)


def interactive_auth(settings: Settings, open_browser: bool = True) -> dict[str, Any]:
    settings.require_credentials()
    url, _state = build_authorization_url(settings)
    print("\nAuthorize Whoop access:\n")
    print(url)
    print(
        "\nAfter approving, your browser will hit the redirect URI (often a "
        "certificate warning on https://localhost). Copy the FULL redirect URL "
        "from the address bar (it contains ?code=...) and paste it below.\n"
    )
    if open_browser:
        webbrowser.open(url)
    pasted = input("Paste redirect URL or code: ").strip()
    code = extract_code(pasted)
    token = exchange_code(settings, code)
    store = TokenStore(settings.token_path)
    store.save(token)
    print(f"\nTokens saved to {settings.token_path}")
    return token


def get_valid_token(settings: Settings) -> dict[str, Any]:
    settings.require_credentials()
    store = TokenStore(settings.token_path)
    token = store.load()
    if not token:
        raise SystemExit(
            f"No tokens at {settings.token_path}. Run: whoop-claude-sync auth"
        )
    if access_token_expired(token):
        token = refresh_access_token(settings, token)
        store.save(token)
    return token
