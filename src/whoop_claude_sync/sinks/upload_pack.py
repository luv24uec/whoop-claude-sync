"""Stage curated markdown for manual Claude Project upload."""

from __future__ import annotations

import shutil
from pathlib import Path

UPLOAD_FILES = [
    "WHOOP_BRIEF.md",
    "WHOOP_LAST_7_DAYS.md",
    "WHOOP_LAST_30_DAYS.md",
    "WHOOP_WORKOUTS.md",
    "PROJECT_INSTRUCTIONS.md",
]


def stage_upload_pack(out_dir: Path) -> Path:
    pack = out_dir / "upload_pack"
    if pack.exists():
        shutil.rmtree(pack)
    pack.mkdir(parents=True, exist_ok=True)

    copied = 0
    for name in UPLOAD_FILES:
        src = out_dir / name
        if src.exists():
            shutil.copy2(src, pack / name)
            copied += 1

    readme = pack / "README_UPLOAD.txt"
    readme.write_text(
        "Upload these .md files into your Claude Project knowledge.\n"
        "Replace prior WHOOP_*.md files so Claude does not keep stale copies.\n"
        "Paste PROJECT_INSTRUCTIONS.md into the Project's custom instructions "
        "(optional).\n",
        encoding="utf-8",
    )
    if copied == 0:
        raise SystemExit("No markdown files found. Run sync first.")
    return pack
