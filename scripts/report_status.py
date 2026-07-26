#!/usr/bin/env python3
"""
Report uncommitted and unpushed changes across the multirepo (root + nested submodules).

Writes Markdown to .reports/status_<timestamp>.md
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/report_status.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reports_common import (  # noqa: E402
    discover_repos,
    repo_info,
    repo_root,
    reports_dir,
    run_git,
    timestamp_slug,
    write_markdown_report,
)


def uncommitted_summary(cwd: Path) -> tuple[bool, list[str]]:
    """Return (dirty, porcelain status lines)."""
    result = run_git(cwd, "status", "--porcelain=v1", "--branch")
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    # First line is ## branch… when --branch is used; file entries follow.
    file_lines = [ln for ln in lines if not ln.startswith("##")]
    return bool(file_lines), lines


def unpushed_commits(cwd: Path) -> tuple[int, list[str]]:
    """Commits on HEAD not on the upstream (or origin/<branch> fallback)."""
    upstream = run_git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode == 0 and upstream.stdout.strip():
        ref = upstream.stdout.strip()
    else:
        branch = run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":
            return 0, []
        probe = run_git(cwd, "rev-parse", "--verify", f"origin/{branch}")
        if probe.returncode != 0:
            return 0, []
        ref = f"origin/{branch}"

    count_result = run_git(cwd, "rev-list", "--count", f"{ref}..HEAD")
    try:
        count = int(count_result.stdout.strip() or "0")
    except ValueError:
        count = 0
    if count == 0:
        return 0, []

    log = run_git(cwd, "log", "--oneline", f"{ref}..HEAD")
    commits = [ln for ln in log.stdout.splitlines() if ln.strip()]
    return count, commits


def main() -> int:
    root = repo_root()
    repos = discover_repos(root)
    dirty_sections: list[str] = []
    unpushed_sections: list[str] = []
    clean_count = 0

    print(f"Scanning {len(repos)} git repositories under {root}...")

    for path in repos:
        info = repo_info(root, path)
        dirty, status_lines = uncommitted_summary(path)
        ahead, commits = unpushed_commits(path)

        if not dirty and ahead == 0:
            clean_count += 1
            continue

        header = f"### `{info.rel}`"
        meta = f"- Branch: `{info.branch}`"

        if dirty:
            dirty_sections.extend([
                header,
                meta,
                "",
                "```",
                *status_lines,
                "```",
                "",
            ])
            print(f"  dirty: {info.rel} ({info.branch})")

        if ahead > 0:
            unpushed_sections.extend([
                header,
                meta,
                f"- Unpushed commits: **{ahead}**",
                "",
                "```",
                *commits,
                "```",
                "",
            ])
            print(f"  unpushed: {info.rel} (+{ahead})")

    body: list[str] = [
        "## Summary",
        "",
        f"- Repositories scanned: **{len(repos)}**",
        f"- Clean (no uncommitted / unpushed): **{clean_count}**",
        f"- With uncommitted changes: **{len([s for s in dirty_sections if s.startswith('###')])}**",
        f"- With unpushed commits: **{len([s for s in unpushed_sections if s.startswith('###')])}**",
        "",
    ]

    if dirty_sections:
        body.extend(["## Uncommitted changes", ""] + dirty_sections)
    else:
        body.extend(["## Uncommitted changes", "", "_None._", ""])

    if unpushed_sections:
        body.extend(["## Unpushed commits", ""] + unpushed_sections)
    else:
        body.extend(["## Unpushed commits", "", "_None._", ""])

    out = reports_dir(root) / f"status_{timestamp_slug()}.md"
    write_markdown_report(out, "Git status report", root, body)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
