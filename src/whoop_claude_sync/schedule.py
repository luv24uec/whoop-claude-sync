"""Install / manage macOS launchd schedules for daily + weekly sync."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_SH = PROJECT_ROOT / "deploy" / "run.sh"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs"

DAILY_LABEL = "com.whoop-claude-sync.daily"
WEEKLY_LABEL = "com.whoop-claude-sync.weekly"


def daily_plist_path() -> Path:
    return LAUNCH_AGENTS / f"{DAILY_LABEL}.plist"


def weekly_plist_path() -> Path:
    return LAUNCH_AGENTS / f"{WEEKLY_LABEL}.plist"


def _build_plist(
    *,
    label: str,
    args: list[str],
    calendar: dict,
    log_name: str,
) -> dict:
    log_path = str(LOG_DIR / log_name)
    return {
        "Label": label,
        "ProgramArguments": [str(RUN_SH), *args],
        "WorkingDirectory": str(PROJECT_ROOT),
        "StartCalendarInterval": calendar,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "RunAtLoad": False,
    }


def write_plists(
    *,
    daily_hour: int = 7,
    daily_minute: int = 15,
    weekly_weekday: int = 0,
    weekly_hour: int = 8,
    weekly_minute: int = 0,
) -> tuple[Path, Path]:
    """Write LaunchAgent plists. weekday: 0=Sunday … 6=Saturday."""
    if not RUN_SH.exists():
        raise SystemExit(f"Missing launcher: {RUN_SH}")
    RUN_SH.chmod(0o755)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    daily = _build_plist(
        label=DAILY_LABEL,
        args=["run-daily"],
        calendar={"Hour": daily_hour, "Minute": daily_minute},
        log_name="whoop-sync-daily.log",
    )
    weekly = _build_plist(
        label=WEEKLY_LABEL,
        args=["run-weekly"],
        calendar={
            "Weekday": weekly_weekday,
            "Hour": weekly_hour,
            "Minute": weekly_minute,
        },
        log_name="whoop-sync-weekly.log",
    )

    daily_path = daily_plist_path()
    weekly_path = weekly_plist_path()
    daily_path.write_bytes(plistlib.dumps(daily))
    weekly_path.write_bytes(plistlib.dumps(weekly))
    return daily_path, weekly_path


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _uid() -> str:
    return subprocess.check_output(["id", "-u"], text=True).strip()


def load_agents() -> None:
    uid = _uid()
    for path in (daily_plist_path(), weekly_plist_path()):
        label = path.stem
        # Bootout if already loaded (ignore errors), then bootstrap.
        _launchctl("bootout", f"gui/{uid}/{label}")
        result = _launchctl("bootstrap", f"gui/{uid}", str(path))
        if result.returncode != 0:
            # Older macOS fallback
            result2 = _launchctl("load", "-w", str(path))
            if result2.returncode != 0:
                raise SystemExit(
                    f"Failed to load {path.name}:\n"
                    f"{result.stderr or result.stdout}\n"
                    f"{result2.stderr or result2.stdout}"
                )


def unload_agents() -> None:
    uid = _uid()
    for path in (daily_plist_path(), weekly_plist_path()):
        label = path.stem
        _launchctl("bootout", f"gui/{uid}/{label}")
        _launchctl("unload", "-w", str(path))
        if path.exists():
            path.unlink()


def agent_loaded(label: str) -> bool:
    uid = _uid()
    result = _launchctl("print", f"gui/{uid}/{label}")
    return result.returncode == 0


def status_lines() -> list[str]:
    lines = []
    for label, path in (
        (DAILY_LABEL, daily_plist_path()),
        (WEEKLY_LABEL, weekly_plist_path()),
    ):
        loaded = agent_loaded(label)
        lines.append(
            f"{label}: {'loaded' if loaded else 'not loaded'} "
            f"({path if path.exists() else 'plist missing'})"
        )
    return lines
