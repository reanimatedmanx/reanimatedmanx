#!/usr/bin/env python3
"""
Compare remote repositories with every local git project under the workspace
root — including nested submodules at any depth.

Remote inventory tries CLIs in order: `gh`, then `tea`. Matching uses git
remote metadata (origin URL), not directory names, so aliased clone /
submodule paths still resolve correctly.

Writes Markdown to .reports/repos_<timestamp>.md
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reports_common import (  # noqa: E402
    discover_repos,
    rel_path,
    repo_root,
    reports_dir,
    run_git,
    timestamp_slug,
    write_markdown_report,
)

CLI_TIMEOUT_SEC = 45
TOOL_PROBE_TIMEOUT_SEC = 8
PROVIDERS = ("gh", "tea")


@dataclass(frozen=True)
class RemoteId:
    realm: str
    namespace: str
    project: str

    @property
    def key(self) -> str:
        return f"{self.realm}/{self.namespace}/{self.project}".lower()

    @property
    def slug(self) -> str:
        return f"{self.namespace}/{self.project}"


@dataclass
class LocalRepo:
    path: Path
    rel: str
    remote: RemoteId | None
    origin_url: str
    is_submodule: bool
    path_basename: str

    @property
    def relation(self) -> str:
        if self.remote is None:
            return "no origin"
        if self.path_basename.lower() == self.remote.project.lower():
            return "exact"
        return "aliased"

    @property
    def alias(self) -> str:
        """Local directory name when it differs from the remote project name."""
        if self.remote is None:
            return ""
        if self.path_basename.lower() == self.remote.project.lower():
            return ""
        return self.path_basename


@dataclass
class RemoteRepo:
    remote: RemoteId
    url: str
    is_fork: bool = False
    parent: str = ""  # closest parent as owner/name (GitHub/Gitea)
    source: str = "gh"


@dataclass
class Row:
    realm: str
    namespace: str
    project: str
    local_path: str
    alias: str
    relation: str
    is_submodule: str
    status: str
    kind: str = "—"  # HTML label: yes (owner/repo) | no | —
    parent: str = "—"  # closest upstream owner/name (used to build Fork label)
    origin_url: str = ""


@dataclass
class SoftError:
    host: str
    namespace: str
    message: str
    tried: list[str] = field(default_factory=list)


def run_cmd(
    cmd: list[str],
    *,
    timeout: float = CLI_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"timed out after {timeout}s",
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="not found")


def run_gh(*args: str, host: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["gh"]
    if host and host not in {"github.com", "gist.github.com"}:
        cmd.extend(["-h", host])
    cmd.extend(args)
    return run_cmd(cmd)


def run_tea(*args: str) -> subprocess.CompletedProcess[str]:
    return run_cmd(["tea", *args])


_tool_status: dict[str, tuple[bool, str]] = {}


def tool_status(name: str) -> tuple[bool, str]:
    """Return (available, detail). On PATH counts as available; version probe is best-effort."""
    if name in _tool_status:
        return _tool_status[name]
    path = shutil.which(name)
    if not path:
        _tool_status[name] = (False, "not found on PATH")
        return _tool_status[name]
    probe = run_cmd([name, "--version"], timeout=TOOL_PROBE_TIMEOUT_SEC)
    if probe.returncode == 127:
        _tool_status[name] = (False, "not found on PATH")
        return _tool_status[name]
    version = (probe.stdout or probe.stderr or "").strip().splitlines()
    if probe.returncode == 124:
        # Binary exists but version hung (seen with some Windows installs) — still try real commands.
        _tool_status[name] = (True, f"{path} (version probe timed out; commands will still be tried)")
        return _tool_status[name]
    detail = version[0] if version else path
    _tool_status[name] = (True, detail)
    return _tool_status[name]


def normalize_remote_url(url: str) -> RemoteId | None:
    """Parse a git remote URL into (realm, namespace, project)."""
    raw = (url or "").strip()
    if not raw:
        return None

    raw = raw.removesuffix("/")
    host = ""
    path = ""

    scp = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", raw)
    if scp:
        host, path = scp.group(1), scp.group(2)
    elif "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    else:
        parts = raw.split("/", 1)
        if len(parts) != 2:
            return None
        host, path = parts[0], parts[1]

    host = host.lower().strip()
    path = path.strip().removesuffix(".git").strip("/")
    if not host or not path:
        return None

    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return None

    project = segments[-1]
    namespace = "/".join(segments[:-1])
    return RemoteId(realm=host, namespace=namespace, project=project)


def origin_url(cwd: Path) -> str:
    for name in ("origin",):
        result = run_git(cwd, "remote", "get-url", name)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    remotes = run_git(cwd, "remote").stdout.split()
    for name in remotes:
        result = run_git(cwd, "remote", "get-url", name)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return ""


def collect_submodule_paths(root: Path) -> set[Path]:
    paths: set[Path] = set()
    result = run_git(root, "submodule", "status", "--recursive")
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        path = (root / parts[1]).resolve()
        if path.is_dir():
            paths.add(path)
    return paths


def is_git_submodule_dir(path: Path) -> bool:
    git_entry = path / ".git"
    try:
        return git_entry.is_file()
    except OSError:
        return False


def discover_local(root: Path) -> list[LocalRepo]:
    submodule_paths = collect_submodule_paths(root)
    locals_: list[LocalRepo] = []
    for path in discover_repos(root):
        url = origin_url(path)
        remote = normalize_remote_url(url)
        is_sub = path in submodule_paths or (path != root.resolve() and is_git_submodule_dir(path))
        locals_.append(
            LocalRepo(
                path=path,
                rel=rel_path(root, path),
                remote=remote,
                origin_url=url,
                is_submodule=is_sub,
                path_basename=path.name if path != root.resolve() else Path(root).name,
            )
        )
    return locals_


def gh_authenticated_login(host: str = "github.com") -> str | None:
    ok, _ = tool_status("gh")
    if not ok:
        return None
    result = run_gh("api", "user", "--jq", ".login", host=host)
    if result.returncode != 0:
        return None
    login = result.stdout.strip()
    return login or None


_gh_host_cache: dict[str, bool] = {}


def gh_host_available(host: str) -> bool:
    if host in _gh_host_cache:
        return _gh_host_cache[host]
    ok_tool, _ = tool_status("gh")
    if not ok_tool:
        _gh_host_cache[host] = False
        return False
    if host == "github.com":
        _gh_host_cache[host] = True
        return True
    result = run_gh("api", "user", "--jq", ".login", host=host)
    ok = False
    if result.returncode == 0:
        login = (result.stdout or "").strip()
        if login and " " not in login and "\n" not in login and not login.lower().startswith("usage"):
            if "Makes an authenticated HTTP request" not in (result.stderr or ""):
                ok = True
    _gh_host_cache[host] = ok
    return ok


def gh_list_repos(host: str, namespace: str) -> tuple[list[RemoteRepo], str]:
    ok_tool, detail = tool_status("gh")
    if not ok_tool:
        return [], f"`gh` unavailable ({detail})"
    if not gh_host_available(host):
        return [], f"host `{host}` unreacheable"

    result = run_gh(
        "repo",
        "list",
        namespace,
        "--limit",
        "1000",
        "--json",
        "name,owner,url,nameWithOwner,isFork",
        host=host,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "gh repo list failed").strip().splitlines()
        return [], err[0] if err else "gh repo list failed"

    raw = (result.stdout or "").strip()
    if not raw:
        return [], "empty response from gh"
    if raw.lower().startswith("list repositories") or raw.lower().startswith("usage"):
        return [], f"host `{host}` is not available to `gh`"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], "invalid JSON from gh"

    remotes: list[RemoteRepo] = []
    for item in payload:
        url = item.get("url") or ""
        parsed = normalize_remote_url(url)
        if parsed is None:
            owner = (item.get("owner") or {}).get("login") or namespace
            name = item.get("name") or ""
            if not name:
                continue
            parsed = RemoteId(realm=host, namespace=owner, project=name)
            url = f"https://{host}/{owner}/{name}"
        remotes.append(
            RemoteRepo(
                remote=parsed,
                url=url,
                is_fork=bool(item.get("isFork")),
                parent="",
                source="gh",
            )
        )
    return remotes, ""


def gh_fetch_parent(host: str, namespace: str, project: str) -> str:
    """Return closest parent full_name (owner/repo) for a fork, or empty."""
    result = run_gh(
        "api",
        f"repos/{namespace}/{project}",
        "--jq",
        ".parent.full_name // empty",
        host=host,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def enrich_fork_parents(remotes: dict[str, RemoteRepo], *, workers: int = 8) -> int:
    """Fill parent for forked remotes inventored via gh. Returns how many filled."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pending = [
        r
        for r in remotes.values()
        if r.source == "gh" and r.is_fork and not r.parent and r.remote.realm == "github.com"
    ]
    if not pending:
        # Still allow non-github.com gh hosts
        pending = [
            r
            for r in remotes.values()
            if r.source == "gh" and r.is_fork and not r.parent
        ]
    if not pending:
        return 0

    print(f"  Enriching fork parents for {len(pending)} fork(s)...")
    filled = 0

    def work(repo: RemoteRepo) -> tuple[str, str]:
        parent = gh_fetch_parent(repo.remote.realm, repo.remote.namespace, repo.remote.project)
        return repo.remote.key, parent

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, repo) for repo in pending]
        for fut in as_completed(futures):
            key, parent = fut.result()
            if parent and key in remotes:
                remotes[key].parent = parent
                filled += 1
    print(f"  -> resolved {filled}/{len(pending)} parent(s)")
    return filled


_tea_login_by_host: dict[str, str | None] | None = None


def tea_login_for_host(host: str) -> str | None:
    """Map a git host to a tea login name (or None if no matching login)."""
    global _tea_login_by_host
    if _tea_login_by_host is None:
        _tea_login_by_host = {}
        result = run_tea("logins", "list", "--output", "json")
        if result.returncode != 0:
            result = run_tea("login", "list", "--output", "json")
        if result.returncode == 0 and (result.stdout or "").strip():
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, dict):
                payload = payload.get("logins") or payload.get("Logins") or []
            for item in payload or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("Name") or item.get("name") or item.get("Login") or ""
                url = item.get("URL") or item.get("url") or item.get("Address") or ""
                parsed_host = urlparse(url).hostname if url else None
                if not parsed_host and url:
                    parsed_host = url.split("/")[0]
                if name and parsed_host:
                    _tea_login_by_host[parsed_host.lower()] = str(name)
    if host in _tea_login_by_host:
        return _tea_login_by_host[host]
    _tea_login_by_host[host] = None
    return None


def _tea_parse_repos(host: str, namespace: str, payload: object) -> list[RemoteRepo]:
    if isinstance(payload, dict):
        for key in ("data", "repos", "repositories", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []

    remotes: list[RemoteRepo] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("Name") or ""
        owner = item.get("owner") or item.get("Owner") or {}
        if isinstance(owner, dict):
            owner_name = owner.get("login") or owner.get("username") or owner.get("UserName") or namespace
        else:
            owner_name = str(owner) if owner else namespace
        url = item.get("html_url") or item.get("clone_url") or item.get("url") or item.get("ssh") or ""
        if isinstance(url, dict):
            url = url.get("html") or url.get("clone") or ""
        if not name:
            continue
        parsed = normalize_remote_url(str(url)) if url else None
        if parsed is None:
            parsed = RemoteId(realm=host, namespace=str(owner_name), project=str(name))
            url = f"https://{host}/{owner_name}/{name}"
        if parsed.namespace.lower() != namespace.lower() and str(owner_name).lower() != namespace.lower():
            continue
        remotes.append(
            RemoteRepo(
                remote=RemoteId(realm=host, namespace=namespace, project=parsed.project),
                url=str(url),
                is_fork=bool(item.get("fork") or item.get("Fork") or item.get("isFork")),
                parent=_tea_parent_slug(item),
                source="tea",
            )
        )
    return remotes


def _tea_parent_slug(item: dict) -> str:
    parent = item.get("parent") or item.get("Parent") or item.get("base") or {}
    if isinstance(parent, str) and "/" in parent:
        return parent
    if not isinstance(parent, dict):
        return ""
    full = parent.get("full_name") or parent.get("FullName") or ""
    if full:
        return str(full)
    owner = parent.get("owner") or parent.get("Owner") or {}
    if isinstance(owner, dict):
        owner_name = owner.get("login") or owner.get("username") or ""
    else:
        owner_name = str(owner or "")
    name = parent.get("name") or parent.get("Name") or ""
    if owner_name and name:
        return f"{owner_name}/{name}"
    return ""


_tea_host_dead: dict[str, str] = {}


def tea_list_repos(host: str, namespace: str) -> tuple[list[RemoteRepo], str]:
    ok_tool, detail = tool_status("tea")
    if not ok_tool:
        return [], f"`tea` unavailable ({detail})"
    if host in _tea_host_dead:
        return [], _tea_host_dead[host]

    login = tea_login_for_host(host)
    if not login:
        msg = (
            f"no tea login matched host `{host}` "
            f"(configure: tea login add -u https://{host})"
        )
        _tea_host_dead[host] = msg
        return [], msg

    login_args = ["--login", login]

    attempts = [
        ["repos", "search", "--owner", namespace, "--limit", "1000", "--output", "json", *login_args],
        ["repos", "list", "--owner", namespace, "--limit", "1000", "--output", "json", *login_args],
        ["api", f"/orgs/{namespace}/repos?limit=1000", *login_args],
        ["api", f"/users/{namespace}/repos?limit=1000", *login_args],
    ]

    errors: list[str] = []
    for args in attempts:
        result = run_tea(*args)
        if result.returncode == 124:
            msg = f"`tea` timed out for host `{host}`"
            _tea_host_dead[host] = msg
            return [], msg
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "failed").strip().splitlines()
            errors.append(err[0] if err else f"`tea {' '.join(args[:2])}` failed")
            continue
        raw = (result.stdout or "").strip()
        if not raw:
            errors.append(f"`tea {' '.join(args[:2])}` returned empty output")
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"`tea {' '.join(args[:2])}` returned invalid JSON")
            continue
        repos = _tea_parse_repos(host, namespace, payload)
        return repos, ""

    msg = "; ".join(errors[:3]) if errors else "`tea` inventory failed"
    # Don't blackhole the whole host on a single empty owner; only on hard failures above.
    return [], msg


def list_repos_with_fallback(host: str, namespace: str) -> tuple[list[RemoteRepo], str, list[str]]:
    """Try gh then tea. Returns (repos, error_if_all_failed, tried_providers)."""
    tried: list[str] = []
    errors: list[str] = []

    for provider in PROVIDERS:
        ok, detail = tool_status(provider)
        if not ok:
            tried.append(provider)
            errors.append(f"{provider}: {detail}")
            continue
        tried.append(provider)
        if provider == "gh":
            repos, err = gh_list_repos(host, namespace)
        else:
            repos, err = tea_list_repos(host, namespace)
        if not err:
            return repos, "", tried
        errors.append(f"{provider}: {err}")

    return [], " | ".join(errors), tried


def namespaces_to_query(locals_: list[LocalRepo], include_all_orgs: bool) -> dict[str, set[str]]:
    by_realm: dict[str, set[str]] = {}

    for loc in locals_:
        if loc.remote is None:
            continue
        by_realm.setdefault(loc.remote.realm, set()).add(loc.remote.namespace)

    login = gh_authenticated_login("github.com")
    if login:
        by_realm.setdefault("github.com", set()).add(login)

    if include_all_orgs:
        ok, _ = tool_status("gh")
        if ok:
            orgs = run_gh("api", "user/orgs", "--jq", ".[].login", host="github.com")
            if orgs.returncode == 0:
                for org in orgs.stdout.splitlines():
                    org = org.strip()
                    if org:
                        by_realm.setdefault("github.com", set()).add(org)

    return by_realm


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def remote_https_url(realm: str, namespace: str, project: str = "", origin_url: str = "") -> str:
    """Best-effort https URL for a remote project or namespace page."""
    raw = (origin_url or "").strip()
    if raw:
        if raw.startswith("git@"):
            # git@host:owner/repo.git
            m = re.match(r"^git@([^:]+):(.+)$", raw)
            if m:
                host, path = m.group(1), m.group(2).removesuffix(".git")
                return f"https://{host}/{path}"
        if raw.startswith("ssh://git@"):
            m = re.match(r"^ssh://git@([^/]+)/(.+)$", raw)
            if m:
                host, path = m.group(1), m.group(2).removesuffix(".git")
                return f"https://{host}/{path}"
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw.removesuffix(".git").rstrip("/")
    if not realm or realm == "—":
        return ""
    if project and namespace and namespace != "—":
        return f"https://{realm}/{namespace}/{project}"
    if namespace and namespace != "—":
        return f"https://{realm}/{namespace}"
    return f"https://{realm}"


def local_href(local_path: str) -> str:
    """Markdown href relative to `.reports/` so Ctrl+Click opens in the editor."""
    norm = local_path.replace("\\", "/").strip()
    if norm in {"", ".", "—"}:
        return ".."
    return f"../{norm}"


def md_code_link(label: str, href: str) -> str:
    if not href or label in {"", "—"}:
        return "—" if label in {"", "—"} else f"`{md_escape(label)}`"
    safe_label = md_escape(label).replace("]", "\\]")
    # Escape ')' in URLs for markdown links
    safe_href = href.replace(")", "%29")
    return f"[`{safe_label}`]({safe_href})"


def kind_label(is_fork: bool | None, parent: str = "", realm: str = "") -> str:
    """Colored yes/no; parent uses a Markdown link so Ctrl+Click works in the editor."""
    if is_fork is None:
        return "—"
    yes_pill = (
        '<span style="display:inline-block;padding:1px 8px;border-radius:999px;'
        'background:#8250df;color:#fff;font-size:12px;font-weight:600">yes</span>'
    )
    no_pill = (
        '<span style="display:inline-block;padding:1px 8px;border-radius:999px;'
        'background:#1a7f37;color:#fff;font-size:12px;font-weight:600">no</span>'
    )
    if not is_fork:
        return no_pill
    if not parent or parent == "—":
        return yes_pill
    parts = parent.split("/", 1)
    if len(parts) == 2:
        parent_url = remote_https_url(realm or "github.com", parts[0], parts[1], "")
    else:
        parent_url = ""
    if parent_url:
        # Markdown link (not HTML <a>) so Ctrl+Click works like Project / Local path cells.
        return f"{yes_pill} ({md_code_link(parent, parent_url)})"
    return f"{yes_pill} (`{md_escape(parent)}`)"


def fork_fields(remote: RemoteRepo | None) -> tuple[str, str]:
    """Return (kind_html, parent_slug). parent_slug is owner/repo for forks."""
    if remote is None:
        return "—", "—"
    parent = remote.parent if remote.is_fork else ""
    kind = kind_label(bool(remote.is_fork), parent, realm=remote.remote.realm)
    return kind, (parent or "—") if remote.is_fork else "—"


def build_rows(
    locals_: list[LocalRepo],
    remotes: dict[str, RemoteRepo],
    queried: set[str],
) -> list[Row]:
    rows: list[Row] = []
    matched_keys: set[str] = set()

    locals_by_key: dict[str, list[LocalRepo]] = {}
    for loc in locals_:
        if loc.remote is None:
            rows.append(
                Row(
                    realm="—",
                    namespace="—",
                    project="—",
                    local_path=loc.rel,
                    alias="—",
                    relation=loc.relation,
                    is_submodule="yes" if loc.is_submodule else "no",
                    status="local-no-origin",
                    origin_url=loc.origin_url,
                )
            )
            continue
        locals_by_key.setdefault(loc.remote.key, []).append(loc)

    for key, group in sorted(locals_by_key.items()):
        sample = group[0]
        assert sample.remote is not None
        remote = remotes.get(key)
        in_inventory = key in remotes
        namespace_queried = f"{sample.remote.realm}/{sample.remote.namespace}".lower() in queried

        if in_inventory:
            status = "matched"
            matched_keys.add(key)
        elif namespace_queried:
            status = "local-only"
        else:
            status = "local-unlisted"

        for loc in group:
            assert loc.remote is not None
            kind, parent = fork_fields(remote)
            rows.append(
                Row(
                    realm=loc.remote.realm,
                    namespace=loc.remote.namespace,
                    project=loc.remote.project,
                    local_path=loc.rel,
                    alias=loc.alias or "—",
                    relation=loc.relation,
                    is_submodule="yes" if loc.is_submodule else "no",
                    status=status,
                    kind=kind,
                    parent=parent,
                    origin_url=loc.origin_url or (remote.url if remote else ""),
                )
            )

    for key, remote in sorted(remotes.items()):
        if key in matched_keys:
            continue
        kind, parent = fork_fields(remote)
        rows.append(
            Row(
                realm=remote.remote.realm,
                namespace=remote.remote.namespace,
                project=remote.remote.project,
                local_path="—",
                alias="—",
                relation="—",
                is_submodule="—",
                status="remote-only",
                kind=kind,
                parent=parent,
                origin_url=remote.url,
            )
        )

    def local_tree_key(path: str) -> tuple:
        """Depth-first alphabetical key: parent, then children, then next sibling."""
        norm = path.replace("\\", "/").strip().lower()
        if norm in {"", ".", "—"}:
            return ()  # workspace root
        return tuple(p for p in norm.split("/") if p)

    def is_workspace_root_row(r: Row) -> bool:
        norm = r.local_path.replace("\\", "/").strip()
        return norm in {".", ""}

    status_order = {
        "matched": 0,
        "local-only": 1,
        "local-unlisted": 2,
        "local-no-origin": 3,
        "remote-only": 4,
    }

    def row_sort_key(r: Row) -> tuple:
        has_local = r.local_path not in {"", "—"}
        if has_local:
            # 0 = locals; within locals, 0 = workspace root, then DFS path order.
            return (
                0,
                0 if is_workspace_root_row(r) else 1,
                local_tree_key(r.local_path),
                status_order.get(r.status, 9),
                r.project.lower(),
            )
        # Remote-only after all local rows; alphabetical by namespace/project.
        return (
            1,
            1,
            (r.realm.lower(), r.namespace.lower(), r.project.lower()),
            status_order.get(r.status, 9),
            "",
        )

    rows.sort(key=row_sort_key)
    return rows


def render_table(rows: list[Row]) -> list[str]:
    lines = [
        "| Namespace | Project | Fork | Alias | Local path | Relation | isSubmodule | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        alias = f"`{r.alias}`" if r.alias not in {"", "—"} else "—"
        if r.realm in {"", "—"} and r.namespace in {"", "—"}:
            ns_label = "—"
            ns_href = ""
        elif r.realm in {"", "—"}:
            ns_label = r.namespace
            ns_href = remote_https_url("", r.namespace, "", r.origin_url)
        elif r.namespace in {"", "—"}:
            ns_label = r.realm
            ns_href = f"https://{r.realm}" if r.realm else ""
        else:
            ns_label = f"{r.realm}/{r.namespace}"
            ns_href = remote_https_url(r.realm, r.namespace, "", "")

        project_href = remote_https_url(r.realm, r.namespace, r.project, r.origin_url)
        local_cell = (
            "—"
            if r.local_path in {"", "—"}
            else md_code_link(r.local_path, local_href(r.local_path))
        )

        lines.append(
            "| "
            + " | ".join(
                [
                    md_code_link(ns_label, ns_href) if ns_label != "—" else "—",
                    md_code_link(r.project, project_href) if r.project not in {"", "—"} else "—",
                    r.kind,
                    md_escape(alias),
                    local_cell,
                    md_escape(r.relation),
                    md_escape(r.is_submodule),
                    md_escape(r.status),
                ]
            )
            + " |"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare remote repos (gh/tea) with local/nested git projects."
    )
    parser.add_argument(
        "--all-orgs",
        action="store_true",
        help="Also list every GitHub org membership (can be large).",
    )
    parser.add_argument(
        "--namespace",
        action="append",
        default=[],
        metavar="HOST/OWNER",
        help="Extra namespace to inventory, e.g. github.com/reanimatedmanx (repeatable).",
    )
    parser.add_argument(
        "--skip-fork-parents",
        action="store_true",
        help="Skip resolving fork parent full names (faster).",
    )
    args = parser.parse_args()

    root = repo_root()
    print(f"Discovering local git projects under {root}...")
    locals_ = discover_local(root)
    print(f"  Found {len(locals_)} local repositories")

    print("Checking inventory tools...")
    for name in PROVIDERS:
        ok, detail = tool_status(name)
        print(f"  {name}: {'ok' if ok else 'unavailable'} ({detail})")

    by_realm = namespaces_to_query(locals_, include_all_orgs=args.all_orgs)
    for raw in args.namespace:
        raw = raw.strip().replace("\\", "/")
        if "/" not in raw:
            by_realm.setdefault("github.com", set()).add(raw)
            continue
        host, ns = raw.split("/", 1)
        by_realm.setdefault(host.lower(), set()).add(ns)

    remotes: dict[str, RemoteRepo] = {}
    queried: set[str] = set()
    inventory_notes: list[str] = []
    soft_errors: list[SoftError] = []
    sources_used: set[str] = set()

    for host, namespaces in sorted(by_realm.items()):
        for ns in sorted(namespaces):
            print(f"  Listing {host}/{ns} (gh -> tea)...")
            listed, err, tried = list_repos_with_fallback(host, ns)
            key = f"{host}/{ns}".lower()
            if err:
                soft_errors.append(SoftError(host=host, namespace=ns, message=err, tried=tried))
                inventory_notes.append(
                    f"`{host}/{ns}` — inventory failed after trying {', '.join(tried)}; "
                    f"locals kept as `local-unlisted`. ({err})"
                )
                print(f"    -> soft error (tried {', '.join(tried)})")
                continue
            queried.add(key)
            for repo in listed:
                remotes[repo.remote.key] = repo
                sources_used.add(repo.source)
            provider = listed[0].source if listed else (tried[-1] if tried else "?")
            print(f"    -> {len(listed)} remote repo(s) via {provider}")
            if not listed:
                inventory_notes.append(f"`{host}/{ns}` — 0 repositories returned.")

    if args.skip_fork_parents:
        print("  Skipping fork parent enrichment (--skip-fork-parents)")
    else:
        enrich_fork_parents(remotes)

    rows = build_rows(locals_, remotes, queried)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1

    fork_count = sum(1 for r in remotes.values() if r.is_fork)
    parent_count = sum(1 for r in remotes.values() if r.is_fork and r.parent)

    body: list[str] = [
        "## Summary",
        "",
        f"- Local repositories scanned: **{len(locals_)}**",
        f"- Remote repositories inventored: **{len(remotes)}**"
        + (f" (via {', '.join(sorted(sources_used))})" if sources_used else ""),
        f"- Forks inventored: **{fork_count}** (parents resolved: **{parent_count}**)",
        f"- Table rows: **{len(rows)}**",
        f"- Soft inventory errors: **{len(soft_errors)}**",
        "",
        "### Tools",
        "",
    ]
    for name in PROVIDERS:
        ok, detail = tool_status(name)
        body.append(f"- `{name}`: {'available' if ok else 'unavailable'} — {detail}")
    body.extend(["", "### Status counts", ""])
    for status in ("matched", "remote-only", "local-only", "local-unlisted", "local-no-origin"):
        if status in counts:
            body.append(f"- `{status}`: **{counts[status]}**")
    body.append("")

    body.extend(
        [
            "### Matching rules",
            "",
            "- Identity comes from `git remote get-url origin` (normalized host / owner / project), not the folder name.",
            "- `Namespace` / `Project` cells link to the remote host pages; `Local path` links are relative "
            "from `.reports/` (Ctrl+Click in the editor opens the folder).",
            "- `Fork` is a colored label: purple **yes (`owner/repo`)** with closest parent, or green **no** for originals.",
            "- Closest parent comes from GitHub `parent.full_name` (enriched after `gh repo list`).",
            "- `Alias` is the on-disk directory name when it differs from the remote `Project` name; otherwise `—`.",
            "- `Relation` is `exact` when basename matches project, or `aliased` when it does not.",
            "- `isSubmodule` is true for paths registered via `git submodule status --recursive`, or when `.git` is a gitfile.",
            "- Remote inventory tries `gh`, then `tea`. Success marks the namespace as queried.",
            "- `local-unlisted` means no CLI could inventory that realm/namespace (soft error); locals are still listed from origin URLs.",
            "- Rows with a local path are sorted in tree order: workspace root first, then each "
            "top-level project (`archive`, `core`, `data`, …) with its nested submodules before the next sibling.",
            "- `remote-only` rows follow all local rows, alphabetically by namespace/project.",
            "",
        ]
    )

    if soft_errors:
        body.extend(["### Soft errors", ""])
        body.append("| Namespace | Tried | Detail |")
        body.append("| --- | --- | --- |")
        for err in soft_errors:
            body.append(
                f"| `{md_escape(err.host)}/{md_escape(err.namespace)}` | "
                f"{md_escape(', '.join(err.tried))} | {md_escape(err.message)} |"
            )
        body.append("")

    if inventory_notes:
        body.extend(["### Inventory notes", ""])
        for note in inventory_notes:
            body.append(f"- {note}")
        body.append("")

    body.extend(["## Comparison", ""])
    body.extend(render_table(rows))

    aliased = [r for r in rows if r.relation == "aliased" and r.alias not in {"", "—"}]
    if aliased:
        body.extend(["", "## Aliased local paths", ""])
        body.extend(
            [
                "| Alias | Project | Local path | Remote |",
                "| --- | --- | --- | --- |",
            ]
        )
        for r in aliased:
            body.append(
                f"| `{md_escape(r.alias)}` | `{md_escape(r.project)}` | `{md_escape(r.local_path)}` | "
                f"`{md_escape(r.realm)}/{md_escape(r.namespace)}/{md_escape(r.project)}` |"
            )

    out = reports_dir(root) / f"repos_{timestamp_slug()}.md"
    write_markdown_report(out, "Remote <-> local repository comparison", root, body)
    print(f"Wrote {out}")

    if soft_errors:
        print(
            f"Soft errors: {len(soft_errors)} namespace(s) could not be inventored (see report).",
            file=sys.stderr,
        )
        for err in soft_errors[:10]:
            print(f"  - {err.host}/{err.namespace}: tried {', '.join(err.tried)}", file=sys.stderr)
        if len(soft_errors) > 10:
            print(f"  ... and {len(soft_errors) - 10} more", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
