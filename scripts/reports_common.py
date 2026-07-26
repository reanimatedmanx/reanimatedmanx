#!/usr/bin/env python3
"""Shared helpers for multirepo git report scripts."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return Path.cwd().resolve()


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def discover_repos(root: Path) -> list[Path]:
    """Return root + every nested submodule path (recursive), unique and sorted."""
    repos: list[Path] = [root.resolve()]

    result = run_git(root, "submodule", "status", "--recursive")
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Formats: " <sha> path", "+<sha> path", "-<sha> path", "U<sha> path"
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        path = (root / parts[1]).resolve()
        if path.is_dir() and path not in repos:
            repos.append(path)

    # Nested gitdirs via foreach (covers initialized submodules reliably).
    foreach = run_git(
        root,
        "submodule",
        "foreach",
        "--recursive",
        "git rev-parse --show-toplevel",
    )
    for line in foreach.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Entering "):
            continue
        path = Path(line).resolve()
        if path.is_dir() and path not in repos:
            repos.append(path)

    return sorted(set(repos), key=lambda p: str(p).lower())


def rel_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix() or "."
    except ValueError:
        return str(path)


def reports_dir(root: Path) -> Path:
    out = root / ".reports"
    out.mkdir(parents=True, exist_ok=True)
    return out


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def write_markdown_report(path: Path, title: str, root: Path, body_lines: list[str]) -> Path:
    """Write a Markdown report (common human-readable report format)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# {title}",
        "",
        f"- Generated: `{now}`",
        f"- Root: `{root}`",
        "",
        *body_lines,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

@dataclass
class RepoInfo:
    path: Path
    rel: str
    branch: str
    has_origin: bool


def repo_info(root: Path, path: Path) -> RepoInfo:
    branch = run_git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "(detached)"
    remotes = run_git(path, "remote").stdout.split()
    return RepoInfo(
        path=path,
        rel=rel_path(root, path),
        branch=branch,
        has_origin="origin" in remotes,
    )
