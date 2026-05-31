#!/usr/bin/env python3
"""
Aggregate .gitignore files into .gitignore.cleanfs and remove matching paths from disk.

  python scripts/cleanfs.py merge   # build .gitignore.cleanfs
  python scripts/cleanfs.py clean   # delete matches (respects # comments in cleanfs)
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SKIP_PARTS = frozenset({".git"})
SKIP_DIR_NAMES = frozenset({".git"})
SKIP_SCAN_DIR_NAMES = frozenset({".git", "node_modules"})
PROTECTED_NAMES = frozenset({".gitignore", ".gitignore.cleanfs", ".gitmodules", ".gitkeep"})


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


def should_skip_path(path: Path) -> bool:
    return any(part in SKIP_PARTS for part in path.parts)


def parse_gitignore_line(line: str) -> str | None:
    line = line.rstrip("\n\r")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            line = line[:i].rstrip()
            break
    pattern = line.strip()
    return pattern or None


def _scandir_entries(directory: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(directory) as it:
            return list(it)
    except OSError:
        return []


def _safe_is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _safe_is_file(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def walk_paths(
    root: Path,
    *,
    files_only: bool = False,
    dirs_only: bool = False,
    skip_dir_names: frozenset[str] = SKIP_DIR_NAMES,
) -> list[Path]:
    """Walk the tree without following symlinks; skip unreadable or missing paths."""
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in _scandir_entries(current):
            try:
                path = Path(entry.path)
            except OSError:
                continue
            if _safe_is_dir(entry):
                if entry.name not in skip_dir_names:
                    stack.append(path)
                if not files_only:
                    found.append(path)
            elif _safe_is_file(entry) and not dirs_only:
                found.append(path)
    return found


def find_gitignore_files_git(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--recurse-submodules", "**/.gitignore"],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    paths: list[Path] = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        candidate = (root / line).resolve()
        if candidate.is_file():
            paths.append(candidate)
    return paths


def find_gitignore_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for path in find_gitignore_files_git(root):
        if not should_skip_path(path):
            found.add(path)
    for path in walk_paths(root, files_only=True, skip_dir_names=SKIP_SCAN_DIR_NAMES):
        if path.name != ".gitignore" or should_skip_path(path):
            continue
        found.add(path.resolve())
    root_file = (root / ".gitignore").resolve()
    if root_file.is_file():
        found.add(root_file)
    return sorted(found)


def canonical_key(raw: str) -> str:
    """Normalize a gitignore line for cross-file deduplication."""
    s = raw.replace("\\", "/")
    if s.startswith("/"):
        s = s[1:]
    return s.rstrip("/")


def pick_display_form(variants: list[str]) -> str:
    """Pick one representative spelling from equivalent gitignore lines."""
    normalized = [v.replace("\\", "/") for v in variants]
    best = Counter(normalized).most_common(1)[0][0]
    if best.startswith("/"):
        best = best[1:]
    return best


def resolve_project_pattern(gitignore_path: Path, root: Path, raw: str) -> str:
    """Anchor a single-source gitignore rule to its project directory."""
    raw = raw.replace("\\", "/")
    if raw.startswith("!"):
        return raw

    rel_parent = gitignore_path.parent.relative_to(root)
    if raw.startswith("/"):
        if rel_parent == Path("."):
            return raw[1:]
        return f"{rel_parent.as_posix()}{raw}"

    core = raw.rstrip("/")
    if rel_parent == Path("."):
        return raw
    if "/" in core:
        return f"{rel_parent.as_posix()}/{raw}"
    return f"{rel_parent.as_posix()}/{raw}"


def pattern_key(pattern: str) -> str:
    return pattern.replace("\\", "/").rstrip("/")


def collect_patterns(root: Path) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Return common patterns, project patterns, and per-source (non-common) mapping."""
    Rule = tuple[Path, str]
    by_canonical: dict[str, list[Rule]] = {}

    for gitignore in find_gitignore_files(root):
        try:
            text = gitignore.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            parsed = parse_gitignore_line(line)
            if not parsed or parsed.startswith("!"):
                continue
            key = canonical_key(parsed)
            by_canonical.setdefault(key, []).append((gitignore, parsed))

    common_keys = {
        key
        for key, rules in by_canonical.items()
        if len({gitignore for gitignore, _ in rules}) >= 2
    }

    common_seen: dict[str, str] = {}
    common_ordered: list[str] = []
    project_seen: dict[str, str] = {}
    project_ordered: list[str] = []
    by_source: dict[str, list[str]] = {}

    for key, rules in sorted(by_canonical.items(), key=lambda item: item[0].lower()):
        if key in common_keys:
            display = pick_display_form([raw for _, raw in rules])
            pkey = pattern_key(display)
            if pkey not in common_seen:
                common_seen[pkey] = display
                common_ordered.append(display)
            continue

        for gitignore, raw in rules:
            resolved = resolve_project_pattern(gitignore, root, raw)
            rel_source = gitignore.relative_to(root).as_posix()
            pkey = pattern_key(resolved)
            if pkey not in project_seen:
                project_seen[pkey] = resolved
                project_ordered.append(resolved)
            by_source.setdefault(rel_source, []).append(resolved)

    return common_ordered, project_ordered, by_source


def write_cleanfs(
    root: Path,
    common: list[str],
    project: list[str],
    by_source: dict[str, list[str]],
) -> Path:
    out = root / ".gitignore.cleanfs"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Generated by: python scripts/cleanfs.py merge",
        f"# Generated at: {now}",
        "#",
        "# Comment out any line below (prefix with #) to keep those paths during clean.",
        "# Negation rules (!) from source .gitignore files are not included.",
        "# Patterns shared by 2+ .gitignore files are listed once under common.",
        "",
        "# --- common ---",
    ]
    lines.extend(common)
    lines.extend(["", "# --- project-specific ---"])
    lines.extend(project)

    if by_source:
        lines.extend(["", "# --- sources (reference; not used by clean) ---"])
        for source, pats in sorted(by_source.items()):
            lines.append(f"# --- {source} ---")
            lines.extend(pats)
            lines.append("")

    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def load_cleanfs_patterns(cleanfs: Path) -> list[str]:
    if not cleanfs.is_file():
        raise FileNotFoundError(f"Missing {cleanfs}; run merge first.")

    patterns: list[str] = []
    seen: set[str] = set()
    section: str | None = None

    for line in cleanfs.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped == "# --- common ---":
            section = "active"
            continue
        if stripped == "# --- project-specific ---":
            section = "active"
            continue
        if stripped.startswith("# --- ") and section == "active":
            break
        if section != "active":
            continue
        parsed = parse_gitignore_line(line)
        if not parsed:
            continue
        key = pattern_key(parsed)
        if key not in seen:
            seen.add(key)
            patterns.append(parsed)

    if patterns:
        return patterns

    # Legacy: older files used a deduplicated footer section.
    in_dedup = False
    for line in cleanfs.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("# --- deduplicated"):
            in_dedup = True
            continue
        if not in_dedup:
            continue
        parsed = parse_gitignore_line(line)
        if parsed:
            key = pattern_key(parsed)
            if key not in seen:
                seen.add(key)
                patterns.append(parsed)
    return patterns


def path_size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    if not path.is_dir():
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        if should_skip_path(Path(dirpath)):
            continue
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def is_protected(path: Path, root: Path) -> bool:
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return True
    if rel == Path("."):
        return True
    if path.name in PROTECTED_NAMES:
        return True
    return should_skip_path(path)


def _match_segment(rel_parts: tuple[str, ...], pattern_parts: list[str], idx: int) -> bool:
    if not pattern_parts:
        return idx == len(rel_parts)
    seg = pattern_parts[0]
    rest = pattern_parts[1:]
    if seg == "**":
        if _match_segment(rel_parts, rest, idx):
            return True
        if idx < len(rel_parts):
            return _match_segment(rel_parts, rest, idx) or _match_segment(rel_parts, rest, idx + 1)
        return False
    if idx >= len(rel_parts):
        return False
    if seg == "*":
        if not _match_segment(rel_parts, rest, idx + 1):
            return False
    elif not fnmatch.fnmatchcase(rel_parts[idx], seg):
        return False
    return _match_segment(rel_parts, rest, idx + 1)


def rel_matches_pattern(rel_posix: str, pattern: str) -> bool:
    pattern = pattern.replace("\\", "/").rstrip("/")
    rel = rel_posix.replace("\\", "/").lstrip("/")
    if not pattern or not rel:
        return False
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    rel_parts = tuple(rel.split("/"))
    pat_parts = [p for p in pattern.split("/") if p]
    if not pat_parts:
        return False
    if "**" not in pattern and "*" not in pattern:
        if anchored:
            return rel == pattern or rel.startswith(pattern + "/")
        return rel == pattern or rel.endswith("/" + pattern) or ("/" + pattern + "/") in ("/" + rel + "/")
    start = 0 if anchored else 0
    if anchored:
        if not _match_segment(rel_parts, pat_parts, 0):
            return False
        return True
    for i in range(len(rel_parts)):
        if _match_segment(rel_parts, pat_parts, i):
            return True
    return _match_segment(rel_parts, pat_parts, 0)


def _path_kind(path: Path) -> str | None:
    try:
        if path.is_dir():
            return "dir"
        if path.is_file() or path.is_symlink():
            return "file"
    except OSError:
        return None
    return None


def targets_for_pattern(root: Path, pattern: str) -> list[Path]:
    pattern = pattern.replace("\\", "/")
    is_dir_pattern = pattern.endswith("/")
    norm = pattern.rstrip("/")
    hits: set[Path] = set()

    def consider(path: Path) -> None:
        if should_skip_path(path) or is_protected(path, root):
            return
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return
        if not rel_matches_pattern(rel, norm):
            return
        kind = _path_kind(path)
        if kind is None:
            return
        if is_dir_pattern and kind != "dir":
            return
        if not is_dir_pattern and kind == "dir":
            return
        try:
            hits.add(path.resolve())
        except OSError:
            pass

    if "**" in norm or "*" in norm:
        for path in walk_paths(root):
            consider(path)
        return sorted(hits, key=lambda p: len(p.parts), reverse=True)

    if "/" in norm.lstrip("/"):
        candidate = (root / norm.lstrip("/"))
        try:
            candidate = candidate.resolve()
        except OSError:
            return []
        consider(candidate)
        return sorted(hits, key=lambda p: len(p.parts), reverse=True)

    basename = norm.lstrip("/")
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in _scandir_entries(current):
            try:
                path = Path(entry.path)
            except OSError:
                continue
            if entry.name == basename:
                consider(path)
            if _safe_is_dir(entry) and entry.name not in SKIP_DIR_NAMES:
                stack.append(path)
    return sorted(hits, key=lambda p: len(p.parts), reverse=True)


def delete_path(path: Path, dry_run: bool) -> int:
    size = path_size(path)
    if dry_run:
        return size
    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=False)
    return size


def format_gb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 ** 3):.2f}"


def cmd_merge(root: Path) -> int:
    common, project, by_source = collect_patterns(root)
    out = write_cleanfs(root, common, project, by_source)
    total = len(common) + len(project)
    print(f"Wrote {out}")
    print(f"  Sources: {len(by_source)} .gitignore file(s)")
    print(f"  Common patterns: {len(common)}")
    print(f"  Project-specific patterns: {len(project)}")
    print(f"  Total (deduplicated): {total}")
    return 0


def cmd_clean(root: Path, dry_run: bool) -> int:
    cleanfs = root / ".gitignore.cleanfs"
    patterns = load_cleanfs_patterns(cleanfs)
    if not patterns:
        print("No active patterns in .gitignore.cleanfs (all commented or empty).", file=sys.stderr)
        return 1

    print(f"{'[dry-run] ' if dry_run else ''}Cleaning from {root}")
    print(f"  Patterns: {len(patterns)}")
    print()

    reclaimed = 0
    removed_dirs = 0
    removed_files = 0
    t0 = time.time()

    all_targets: dict[Path, None] = {}
    total = len(patterns)
    for i, pattern in enumerate(patterns, 1):
        print(f"[{i}/{total}] scan {pattern}", flush=True)
        for target in targets_for_pattern(root, pattern):
            all_targets[target] = None

    ordered = sorted(all_targets.keys(), key=lambda p: len(p.parts), reverse=True)
    print(f"\nFound {len(ordered)} path(s) to remove\n")

    for j, target in enumerate(ordered, 1):
        kind = _path_kind(target) or "path"
        rel = target.relative_to(root).as_posix()
        print(f"[{j}/{len(ordered)}] {kind}: {rel}", flush=True)
        try:
            was_dir = kind == "dir"
            size = delete_path(target, dry_run=dry_run)
            reclaimed += size
            if was_dir:
                removed_dirs += 1
            else:
                removed_files += 1
        except OSError as exc:
            print(f"  ! failed: {exc}", file=sys.stderr)

    elapsed = time.time() - t0
    prefix = "Would reclaim" if dry_run else "Reclaimed"
    print()
    print(f"{prefix} {format_gb(reclaimed)} GB ({reclaimed:,} bytes)")
    print(f"  Removed: {removed_dirs} director(ies), {removed_files} file(s)")
    print(f"  Elapsed: {elapsed:.1f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge .gitignore files and clean ignored paths from disk.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("merge", help="Build .gitignore.cleanfs from all .gitignore files")

    clean_p = sub.add_parser("clean", help="Delete paths matching non-commented .gitignore.cleanfs entries")
    clean_p.add_argument("--dry-run", action="store_true", help="Report size only; do not delete")

    args = parser.parse_args()
    root = repo_root()

    if args.command == "merge":
        return cmd_merge(root)
    if args.command == "clean":
        return cmd_clean(root, dry_run=args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
