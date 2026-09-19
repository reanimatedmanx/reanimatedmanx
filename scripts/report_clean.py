#!/usr/bin/env python3
"""
Delete old Markdown reports under .reports/, keeping only the latest file
per category (status, stashes, branches, repos, …).

Category files match: <name>_YYYYMMDD_HHMMSS.md
Non-matching files (e.g. drafts) are left untouched.

  python scripts/report_clean.py
  python scripts/report_clean.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reports_common import repo_root, reports_dir  # noqa: E402

# status_20260726_002427.md → category "status", sortable stamp "20260726_002427"
REPORT_RE = re.compile(
    r"^(?P<category>[A-Za-z][A-Za-z0-9-]*)_(?P<stamp>\d{8}_\d{6})\.(?P<ext>md|log)$"
)


def group_reports(directory: Path) -> dict[str, list[Path]]:
    by_category: dict[str, list[Path]] = defaultdict(list)
    if not directory.is_dir():
        return by_category
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = REPORT_RE.match(path.name)
        if not match:
            continue
        by_category[match.group("category")].append(path)
    return by_category


def pick_latest(files: list[Path]) -> Path:
    """Latest by embedded timestamp (lexicographic); mtime as tie-breaker."""

    def key(path: Path) -> tuple[str, float]:
        match = REPORT_RE.match(path.name)
        stamp = match.group("stamp") if match else ""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return stamp, mtime

    return max(files, key=key)


def clean_reports(directory: Path, *, dry_run: bool) -> tuple[int, int, list[str]]:
    """Return (kept, deleted, log lines)."""
    kept = 0
    deleted = 0
    lines: list[str] = []
    grouped = group_reports(directory)

    if not grouped:
        lines.append(f"No categorized reports in {directory}")
        return 0, 0, lines

    for category in sorted(grouped):
        files = grouped[category]
        latest = pick_latest(files)
        kept += 1
        lines.append(f"[{category}] keep  {latest.name}")
        for path in sorted(files, key=lambda p: p.name):
            if path == latest:
                continue
            deleted += 1
            action = "would delete" if dry_run else "delete"
            lines.append(f"[{category}] {action}  {path.name}")
            if not dry_run:
                path.unlink(missing_ok=True)

    return kept, deleted, lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Keep only the latest .reports file per category; delete older ones."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be deleted without removing files.",
    )
    args = parser.parse_args()

    root = repo_root()
    out = reports_dir(root)
    print(f"{'[dry-run] ' if args.dry_run else ''}Cleaning reports in {out}")

    kept, deleted, lines = clean_reports(out, dry_run=args.dry_run)
    for line in lines:
        print(f"  {line}")

    print()
    print(f"Categories: {kept}")
    print(f"{'Would delete' if args.dry_run else 'Deleted'}: {deleted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
