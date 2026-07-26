#!/usr/bin/env python3
"""
List git stashes across the multirepo (root + nested submodules).

Writes Markdown to .reports/stashes_<timestamp>.md
"""

from __future__ import annotations

import sys
from pathlib import Path

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


def list_stashes(cwd: Path) -> list[str]:
    result = run_git(cwd, "stash", "list")
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def main() -> int:
    root = repo_root()
    repos = discover_repos(root)
    sections: list[str] = []
    repos_with_stashes = 0
    total_stashes = 0

    print(f"Scanning {len(repos)} git repositories under {root}...")

    for path in repos:
        info = repo_info(root, path)
        stashes = list_stashes(path)
        if not stashes:
            continue
        repos_with_stashes += 1
        total_stashes += len(stashes)
        sections.extend([
            f"### `{info.rel}`",
            f"- Branch: `{info.branch}`",
            f"- Stashes: **{len(stashes)}**",
            "",
            "```",
            *stashes,
            "```",
            "",
        ])
        print(f"  stashes: {info.rel} ({len(stashes)})")

    body: list[str] = [
        "## Summary",
        "",
        f"- Repositories scanned: **{len(repos)}**",
        f"- Repositories with stashes: **{repos_with_stashes}**",
        f"- Total stashes: **{total_stashes}**",
        "",
        "## Stashes",
        "",
    ]
    if sections:
        body.extend(sections)
    else:
        body.append("_No stashes found._")
        body.append("")

    out = reports_dir(root) / f"stashes_{timestamp_slug()}.md"
    write_markdown_report(out, "Git stash report", root, body)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
