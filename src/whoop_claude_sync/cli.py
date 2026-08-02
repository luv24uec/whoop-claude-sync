"""CLI entrypoint for whoop-claude-sync."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from whoop_claude_sync import __version__
from whoop_claude_sync.auth import TokenStore, extract_code, exchange_code, interactive_auth
from whoop_claude_sync.config import APP_DIR, load_settings
from whoop_claude_sync.render import PROJECT_INSTRUCTIONS, render_all
from whoop_claude_sync.sinks import stage_upload_pack
from whoop_claude_sync.store import COLLECTIONS, load_collection, read_sync_meta
from whoop_claude_sync.sync import ensure_out_dir, run_sync

app = typer.Typer(
    name="whoop-claude-sync",
    help="Pull Whoop data and dump Claude Project knowledge files.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _settings(
    config: Optional[Path],
    out: Optional[Path],
):
    return load_settings(config_path=config, out_dir=out)


@app.command()
def version() -> None:
    """Print version."""
    console.print(__version__)


@app.command("init")
def init_cmd(
    out: Optional[Path] = typer.Option(None, "--out", help="Output directory"),
    config: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Create output folder + starter Project instructions."""
    settings = _settings(config, out)
    ensure_out_dir(settings.out_dir)
    instr = settings.out_dir / "PROJECT_INSTRUCTIONS.md"
    if not instr.exists():
        instr.write_text(PROJECT_INSTRUCTIONS, encoding="utf-8")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    sample = Path.cwd() / "config.toml"
    example = Path.cwd() / "config.example.toml"
    if not sample.exists() and example.exists():
        console.print(
            f"Created {settings.out_dir}\n"
            f"Next: copy config.example.toml → config.toml and add Whoop credentials,\n"
            f"then run: whoop-claude-sync auth"
        )
    else:
        console.print(f"Ready at {settings.out_dir}")


@app.command()
def auth(
    code: Optional[str] = typer.Option(
        None,
        "--code",
        help="Authorization code or full redirect URL (skips interactive paste)",
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Authorize with Whoop (OAuth) and store tokens."""
    settings = _settings(config, None)
    settings.require_credentials()
    if code:
        token = exchange_code(settings, extract_code(code))
        TokenStore(settings.token_path).save(token)
        console.print(f"Tokens saved to {settings.token_path}")
        return
    interactive_auth(settings, open_browser=not no_browser)


@app.command()
def sync(
    days: Optional[int] = typer.Option(
        None, "--days", help="Lookback window in days (overrides config)"
    ),
    out: Optional[Path] = typer.Option(None, "--out", help="Output directory"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Fetch Whoop data and regenerate Claude markdown files."""
    settings = _settings(config, out)
    console.print(f"Syncing → [bold]{settings.out_dir}[/bold]")
    meta = run_sync(settings, days=days)
    _print_sync_meta(meta)
    console.print("Markdown ready: WHOOP_BRIEF.md, WHOOP_LAST_7_DAYS.md, …")


@app.command()
def pack(
    out: Optional[Path] = typer.Option(None, "--out"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Stage curated .md files into out/upload_pack for Claude Project upload."""
    settings = _settings(config, out)
    # Re-render from archive so pack is fresh even without network.
    if any(load_collection(settings.out_dir, name) for name in COLLECTIONS):
        render_all(settings.out_dir)
    pack_dir = stage_upload_pack(settings.out_dir)
    console.print(f"Upload pack ready: [bold]{pack_dir}[/bold]")
    console.print("Drag the .md files into your Claude Project knowledge.")


@app.command()
def status(
    out: Optional[Path] = typer.Option(None, "--out"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Show token + archive status."""
    settings = _settings(config, out)
    token = TokenStore(settings.token_path).load()
    meta = read_sync_meta(settings.out_dir)
    console.print(f"Out dir: {settings.out_dir}")
    console.print(f"Token file: {settings.token_path}")
    console.print(f"Token present: {'yes' if token else 'no'}")
    if token:
        console.print(f"Has refresh_token: {'yes' if token.get('refresh_token') else 'no'}")
    if not meta:
        console.print("No sync_meta.json yet. Run sync.")
        return
    console.print(f"Last sync: {meta.get('last_sync_at')}")
    console.print(f"Window: {meta.get('window_start')} → {meta.get('window_end')}")
    for name, counts in (meta.get("counts") or {}).items():
        console.print(f"  {name}: {counts.get('total', 0)} records")


@app.command("print-auth-url")
def print_auth_url(
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Print the Whoop authorization URL without starting the flow."""
    from whoop_claude_sync.auth import build_authorization_url

    settings = _settings(config, None)
    settings.require_credentials()
    url, state = build_authorization_url(settings)
    console.print(url)
    console.print(f"state={state}")


def _print_sync_meta(meta: dict) -> None:
    table = Table(title="Whoop sync")
    table.add_column("Collection")
    table.add_column("Fetched", justify="right")
    table.add_column("Upserted", justify="right")
    table.add_column("Total", justify="right")
    for name, counts in (meta.get("counts") or {}).items():
        table.add_row(
            name,
            str(counts.get("fetched", 0)),
            str(counts.get("upserted", 0)),
            str(counts.get("total", 0)),
        )
    console.print(table)
    console.print(f"Last sync: {meta.get('last_sync_at')}")
    if meta.get("warning"):
        console.print(f"[yellow]{meta['warning']}[/yellow]")


@app.command("run-daily")
def run_daily(
    config: Optional[Path] = typer.Option(None, "--config"),
    out: Optional[Path] = typer.Option(None, "--out"),
) -> None:
    """Daily job: refresh recent data + regenerate brief / 7-day files."""
    settings = _settings(config, out)
    days = settings.daily_days
    console.print(f"Daily sync: last {days} days → {settings.out_dir}")
    meta = run_sync(settings, days=days)
    _print_sync_meta(meta)
    console.print("Daily markdown refreshed (WHOOP_BRIEF.md, WHOOP_LAST_7_DAYS.md, …)")


@app.command("run-weekly")
def run_weekly(
    config: Optional[Path] = typer.Option(None, "--config"),
    out: Optional[Path] = typer.Option(None, "--out"),
) -> None:
    """Weekly job: fuller history refresh + stage Claude upload_pack."""
    settings = _settings(config, out)
    days = settings.weekly_days
    console.print(f"Weekly sync: last {days} days → {settings.out_dir}")
    meta = run_sync(settings, days=days)
    _print_sync_meta(meta)
    pack_dir = stage_upload_pack(settings.out_dir)
    console.print(f"Weekly pack ready: [bold]{pack_dir}[/bold]")


schedule_app = typer.Typer(help="Install/manage macOS LaunchAgent schedules.")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("install")
def schedule_install(
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Install daily + weekly LaunchAgents and load them."""
    from whoop_claude_sync import schedule as sched

    settings = _settings(config, None)
    daily_path, weekly_path = sched.write_plists(
        daily_hour=settings.daily_hour,
        daily_minute=settings.daily_minute,
        weekly_weekday=settings.weekly_weekday,
        weekly_hour=settings.weekly_hour,
        weekly_minute=settings.weekly_minute,
    )
    sched.load_agents()
    weekday_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    wd = weekday_names[settings.weekly_weekday % 7]
    console.print(f"Installed {daily_path.name}")
    console.print(f"Installed {weekly_path.name}")
    console.print(
        f"Daily: every day at {settings.daily_hour:02d}:{settings.daily_minute:02d} "
        f"(lookback {settings.daily_days}d)"
    )
    console.print(
        f"Weekly: every {wd} at {settings.weekly_hour:02d}:{settings.weekly_minute:02d} "
        f"(lookback {settings.weekly_days}d + pack)"
    )
    console.print("Logs: ~/Library/Logs/whoop-sync-daily.log")
    console.print("      ~/Library/Logs/whoop-sync-weekly.log")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Unload and remove daily + weekly LaunchAgents."""
    from whoop_claude_sync import schedule as sched

    sched.unload_agents()
    console.print("Removed daily + weekly LaunchAgents.")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether scheduled jobs are loaded."""
    from whoop_claude_sync import schedule as sched

    for line in sched.status_lines():
        console.print(line)


if __name__ == "__main__":
    app()
