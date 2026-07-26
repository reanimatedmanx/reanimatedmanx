#!/usr/bin/env python3
"""
List local branches other than main/master that are not yet on origin.

Scans the multirepo root and nested submodules. Writes Markdown to
.reports/branches_<timestamp>.md
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

DEFAULT_BRANCHES = frozenset({"main", "master"})


def local_branches(cwd: Path) -> list[str]:
    result = run_git(cwd, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]


def origin_has_branch(cwd: Path, branch: str) -> bool:
    """True if a local origin/<branch> remote-tracking ref exists (run git fetch to refresh)."""
    verify = run_git(cwd, "rev-parse", "--verify", f"refs/remotes/origin/{branch}")
    return verify.returncode == 0


def branch_ahead_of_origin(cwd: Path, branch: str) -> int | None:
    """How many commits on branch are not on origin/branch. None if origin branch missing."""
    if not origin_has_branch(cwd, branch):
        return None
    count = run_git(cwd, "rev-list", "--count", f"origin/{branch}..{branch}")
    try:
        return int(count.stdout.strip() or "0")
    except ValueError:
        return 0


def main() -> int:
    root = repo_root()
    repos = discover_repos(root)
    sections: list[str] = []
    repos_with = 0
    total_unpushed_branches = 0

    print(f"Scanning {len(repos)} git repositories under {root}...")

    for path in repos:
        info = repo_info(root, path)
        if not info.has_origin:
            continue

        findings: list[str] = []
        for branch in local_branches(path):
            if branch in DEFAULT_BRANCHES:
                continue
            ahead = branch_ahead_of_origin(path, branch)
            if ahead is None:
                findings.append(f"- `{branch}` — **not on origin** (never pushed)")
            elif ahead > 0:
                findings.append(f"- `{branch}` — **{ahead}** commit(s) not on `origin/{branch}`")

        if not findings:
            continue

        repos_with += 1
        total_unpushed_branches += len(findings)
        sections.extend([
            f"### `{info.rel}`",
            f"- Current branch: `{info.branch}`",
            "",
            *findings,
            "",
        ])
        print(f"  branches: {info.rel} ({len(findings)})")

    body: list[str] = [
        "## Summary",
        "",
        f"- Repositories scanned: **{len(repos)}**",
        f"- Repositories with unpushed non-default branches: **{repos_with}**",
        f"- Unpushed non-`main`/`master` branches: **{total_unpushed_branches}**",
        "",
        "## Branches",
        "",
        "_Default branches `main` and `master` are excluded._",
        "",
        "_Uses local `origin/*` refs only; run `git fetch --recurse-submodules` first for freshest results._",
        "",
    ]
    if sections:
        body.extend(sections)
    else:
        body.append("_No unpushed non-default branches found._")
        body.append("")

    out = reports_dir(root) / f"branches_{timestamp_slug()}.md"
    write_markdown_report(out, "Unpushed non-default branches report", root, body)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
