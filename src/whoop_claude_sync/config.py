"""Configuration loading for whoop-claude-sync."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


APP_DIR = Path.home() / ".config" / "whoop-claude-sync"
DEFAULT_TOKEN_PATH = APP_DIR / "tokens.json"
DEFAULT_OUT_DIR = Path.home() / "ClaudeProjects" / "Whoop"
DEFAULT_REDIRECT_URI = "https://localhost:8787/callback"
WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE = "https://api.prod.whoop.com/developer"
DEFAULT_SCOPES = [
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "offline",
]


def expand(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


class Settings(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI
    out_dir: Path = Field(default_factory=lambda: DEFAULT_OUT_DIR)
    token_path: Path = Field(default_factory=lambda: DEFAULT_TOKEN_PATH)
    sync_days: int = 14
    backfill_days: int = 90
    # Scheduled sync windows
    daily_days: int = 14
    weekly_days: int = 90
    daily_hour: int = 7
    daily_minute: int = 15
    weekly_weekday: int = 0  # 0=Sunday … 6=Saturday (launchd)
    weekly_hour: int = 8
    weekly_minute: int = 0
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))

    def require_credentials(self) -> None:
        missing = []
        if not self.client_id:
            missing.append("client_id / WHOOP_CLIENT_ID")
        if not self.client_secret:
            missing.append("client_secret / WHOOP_CLIENT_SECRET")
        if missing:
            raise SystemExit(
                "Missing Whoop credentials: "
                + ", ".join(missing)
                + "\nCopy config.example.toml → config.toml or set .env values."
            )


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(
    config_path: Path | None = None,
    out_dir: Path | None = None,
) -> Settings:
    load_dotenv()
    project_root = Path.cwd()
    candidates = []
    if config_path:
        candidates.append(expand(config_path))
    candidates.extend(
        [
            project_root / "config.toml",
            APP_DIR / "config.toml",
        ]
    )

    raw: dict = {}
    for candidate in candidates:
        raw = _read_toml(candidate)
        if raw:
            break

    client_id = (
        os.getenv("WHOOP_CLIENT_ID")
        or raw.get("client_id")
        or ""
    )
    client_secret = (
        os.getenv("WHOOP_CLIENT_SECRET")
        or raw.get("client_secret")
        or ""
    )
    redirect_uri = (
        os.getenv("WHOOP_REDIRECT_URI")
        or raw.get("redirect_uri")
        or DEFAULT_REDIRECT_URI
    )
    env_out = os.getenv("WHOOP_CLAUDE_OUT") or raw.get("out_dir")
    resolved_out = expand(out_dir or env_out or DEFAULT_OUT_DIR)
    token_path = expand(
        os.getenv("WHOOP_TOKEN_PATH") or raw.get("token_path") or DEFAULT_TOKEN_PATH
    )

    return Settings(
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        redirect_uri=redirect_uri.strip(),
        out_dir=resolved_out,
        token_path=token_path,
        sync_days=int(raw.get("sync_days", 14)),
        backfill_days=int(raw.get("backfill_days", 90)),
        daily_days=int(raw.get("daily_days", raw.get("sync_days", 14))),
        weekly_days=int(raw.get("weekly_days", raw.get("backfill_days", 90))),
        daily_hour=int(raw.get("daily_hour", 7)),
        daily_minute=int(raw.get("daily_minute", 15)),
        weekly_weekday=int(raw.get("weekly_weekday", 0)),
        weekly_hour=int(raw.get("weekly_hour", 8)),
        weekly_minute=int(raw.get("weekly_minute", 0)),
    )
