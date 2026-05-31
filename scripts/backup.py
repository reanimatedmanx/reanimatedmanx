#!/usr/bin/env python3
"""
Backup script that creates a local copy of all files excluding those defined in .gitignore files.
Uses git to respect all .gitignore files across the entire project hierarchy.
"""

import subprocess
import sys
from pathlib import Path
from datetime import datetime
import os
import time
import platform
from multiprocessing import cpu_count


def get_excluded_patterns():
    """Extract directory patterns and general patterns from all .gitignore files."""
    patterns = []
    directories = []
    
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False
    ).stdout.strip()
    if not repo_root:
        repo_root = "."
    repo_root_path = Path(repo_root).resolve()
    
    # Find all .gitignore files using git
    result = subprocess.run(
        ["git", "ls-files", "--recurse-submodules", "**/.gitignore"],
        capture_output=True,
        text=True,
        check=False
    )
    gitignore_files = [Path(repo_root) / f for f in result.stdout.strip().split("\n") if f]
    
    # Also find .gitignore in root
    root_gitignore = Path(repo_root) / ".gitignore"
    if root_gitignore.exists() and root_gitignore not in gitignore_files:
        gitignore_files.append(root_gitignore)
    
    for gitignore_path in gitignore_files:
        if not gitignore_path.exists():
            continue
        
        try:
            # Get relative path from repo root
            rel_path = gitignore_path.relative_to(repo_root_path)
            parent_dir = str(rel_path.parent) if rel_path.parent != Path(".") else ""
            
            with open(gitignore_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith("#"):
                        continue
                    
                    # Build full pattern path
                    if parent_dir:
                        full_pattern = f"{parent_dir}/{line}"
                    else:
                        full_pattern = line
                    
                    # Check if it's a directory pattern (ends with /)
                    if line.endswith("/"):
                        directories.append(full_pattern)
                    else:
                        patterns.append(full_pattern)
        except Exception:
            pass
    
    return directories, patterns

def format_size(size_bytes):
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"

def format_time(seconds):
    """Format time in milliseconds or seconds."""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    return f"{seconds:.2f}s"

def main():
    max_cpu_cores = max(cpu_count(), 4)
    start_time = time.time()
    backup_dir = Path(".backup")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = backup_dir / f"backup_{timestamp}.zip"
    archive_file_list = backup_dir / f"backup_{timestamp}.txt"
    excluded_patterns_file = backup_dir / f"backup_{timestamp}_excluded.txt"
    
    # Create backup directory if it doesn't exist
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    # Log initial info
    print("🚀 Backup started")
    print(f"   Platform: {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"   CPU cores: {max_cpu_cores}")
    print(f"   Backup directory: {backup_dir.resolve()}")
    print()
    
    try:
        # Check for uninitialized submodules
        step_start = time.time()
        submodule_status = subprocess.run(
            ["git", "submodule", "status", "--recursive"],
            capture_output=True,
            text=True,
            check=False
        )
        uninitialized_paths = []
        for line in submodule_status.stdout.split("\n"):
            line = line.strip()
            if line.startswith("-"):
                # Parse: "-<commit-hash> <path> (<branch>)" or "-<commit-hash> <path>"
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    path = parts[1]
                    uninitialized_paths.append(path)
        
        step_elapsed = time.time() - step_start
        if uninitialized_paths:
            print(f"⚠️  Warning: {len(uninitialized_paths)} uninitialized submodule(s) detected. +{format_time(step_elapsed)}")
            print("   Files from uninitialized submodules will not be included in backup.\n")
            
            # Display in ASCII table
            min_width_len = 20
            max_path_len  = max(len(path) for path in uninitialized_paths) if uninitialized_paths else 0
            max_path_len  = max(max_path_len, min_width_len)
            
            # Table header
            print("   " + "┌" + "─" * (max_path_len + 2) + "┐")
            print("   " + "│ " + "Path".ljust(max_path_len) + " │")
            print("   " + "├" + "─" * (max_path_len + 2) + "┤")
            
            # Table rows
            for path in sorted(uninitialized_paths):
                print("   " + "│ " + path.ljust(max_path_len) + " │")
            
            # Table footer
            print("   " + "└" + "─" * (max_path_len + 2) + "┘")
            print("\n   Run 'git submodule update --init --recursive' to initialize them.\n")
        
        # Generate file list using git (respects all .gitignore files)
        # Tracked files (including submodules) - --recurse-submodules handles nested submodules
        step_start = time.time()
        result = subprocess.run(
            ["git", "ls-files", "--recurse-submodules"],
            capture_output=True,
            text=True,
            check=False
        )
        tracked_files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        step_elapsed = time.time() - step_start
        print(f"🔹 Found {len(tracked_files)} tracked files +{format_time(step_elapsed)}")
        
        # Untracked files that aren't ignored (including submodules)
        step_start = time.time()
        result_others = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--recurse-submodules"],
            capture_output=True,
            text=True,
            check=False
        )
        untracked_files = result_others.stdout.strip().split("\n") if result_others.stdout.strip() else []
        step_elapsed = time.time() - step_start
        print(f"🔹 Found {len(untracked_files)} untracked files +{format_time(step_elapsed)}")
        
        # Combine file lists and filter out directories
        all_files = [f for f in tracked_files + untracked_files if f]
        
        # Write file list directly to 7-Zip (filter out directories and non-existent files)
        step_start = time.time()
        file_count = 0
        filtered_count = 0
        
        # Get repo root for path resolution
        repo_root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False
        )
        repo_root = repo_root_result.stdout.strip() if repo_root_result.stdout.strip() else "."
        repo_root_path = Path(repo_root).resolve()
        
        with open(archive_file_list, "w", encoding="utf-8") as f:
            # Write only file paths (not directories)
            for file_path in all_files:
                if not file_path.strip():
                    continue
                
                # Filter out paths ending with / or \ (directories)
                if file_path.endswith("/") or file_path.endswith("\\"):
                    filtered_count += 1
                    continue
                
                # Resolve path relative to repo root and check if it's a file
                try:
                    full_path = (repo_root_path / file_path).resolve()
                    # Check if it exists and is a file (not a directory)
                    if not full_path.exists() or not full_path.is_file():
                        filtered_count += 1
                        continue
                except (OSError, ValueError):
                    # Path is invalid or can't be resolved
                    filtered_count += 1
                    continue
                
                # Write the file path (use forward slashes for 7-Zip compatibility)
                f.write(f"{file_path.replace(chr(92), '/')}\n")
                file_count += 1
        
        step_elapsed = time.time() - step_start
        if filtered_count > 0:
            print(f"💾 Created 7-Zip file list ({file_count} files, filtered {filtered_count} directories/missing) +{format_time(step_elapsed)}")
        else:
            print(f"💾 Created 7-Zip file list ({file_count} files) +{format_time(step_elapsed)}")

        # Get excluded directories and patterns
        excluded_dirs, excluded_patterns = get_excluded_patterns()
        
        # Write excluded patterns file
        step_start = time.time()
        with open(excluded_patterns_file, "w", encoding="utf-8") as f:
            f.write("# Excluded Directories and Patterns\n")
            f.write("# ===================================\n\n")
            
            if excluded_dirs:
                f.write("## Excluded Directories:\n")
                for dir_pattern in sorted(set(excluded_dirs)):
                    f.write(f"{dir_pattern}\n")
                f.write("\n")
            
            if excluded_patterns:
                f.write("## Excluded Patterns:\n")
                for pattern in sorted(set(excluded_patterns)):
                    f.write(f"{pattern}\n")
                f.write("\n")
            
        step_elapsed = time.time() - step_start
        print(f"💾 Wrote excluded patterns file +{format_time(step_elapsed)}")
        
        # repo_root_path already set above when filtering files
        # Convert paths to forward slashes for 7-Zip compatibility
        try:
            archive_name_rel = archive_name.relative_to(repo_root_path)
            archive_file_list_rel = archive_file_list.relative_to(repo_root_path)
            archive_name_arg = str(archive_name_rel).replace("\\", "/")
            archive_file_list_arg = str(archive_file_list_rel).replace("\\", "/")
        except ValueError:
            # Fallback to absolute paths with forward slashes
            archive_name_arg = str(archive_name.resolve()).replace("\\", "/")
            archive_file_list_arg = str(archive_file_list.resolve()).replace("\\", "/")
        
        print(f"📦 Creating archive with 7-Zip...")
        subprocess.run([
            "7z", "a",
            "-tzip", "-m0=lzma2", "-mx9", "-mfb=64", "-md=32m", "-ms=on",
            f"-mmt={max_cpu_cores}",
            archive_name_arg,
            f"@{archive_file_list_arg}"
        ], check=True, cwd=repo_root_path)
        
        # Get archive file size
        archive_size = archive_name.stat().st_size if archive_name.exists() else 0
        
        total_elapsed = time.time() - start_time
        print("✅ Backup successful:")
        print(f"  Archive created: {archive_name}")
        print(f"  Archive size: {format_size(archive_size)}")
        print(f"  Included files: {archive_file_list}")
        print(f"  Excluded patterns: {excluded_patterns_file}")
        print(f"  Files included: {len(all_files)}")
        print(f"  Total time: {format_time(total_elapsed)}")
        
    except Exception as e:
        print(f"❌ Error during backup: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
