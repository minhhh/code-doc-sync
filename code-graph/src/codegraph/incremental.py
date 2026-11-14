"""Incremental graph update logic.

Detects changed files via git diff, re-parses only changed + impacted files,
and updates the graph accordingly. Also supports CLI invocation for hooks.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from .graph import GraphStore
from .parser import CodeParser, EdgeInfo, NodeInfo

_MAX_PARSE_WORKERS = int(os.environ.get("CODE_GRAPH_PARSE_WORKERS", str(min(os.cpu_count() or 4, 8))))


def _select_executor_kind() -> str:
    """Return 'process' or 'thread' for parallel parsing.

    Defaults to ``process`` (the original behavior, fastest on Linux/macOS).
    Auto-switches to ``thread`` when running on Windows with stdin not
    attached to a TTY — that combination indicates a non-interactive stdio
    host, where
    ``ProcessPoolExecutor`` workers inherit the parent's pipe handles and
    leak as zombies after the pool closes (issues #46, #136).

    Override explicitly with ``CODE_GRAPH_PARSE_EXECUTOR={process,thread}``.

    Tree-sitter parsing in the worker releases the GIL during native
    parsing, so the speedup loss for falling back to threads is small
    (typically <30% on the full-build path) and the trade is worth it
    to avoid the deadlock + zombie process accumulation.
    """
    explicit = os.environ.get("CODE_GRAPH_PARSE_EXECUTOR", "").strip().lower()
    if explicit in ("process", "thread"):
        return explicit
    if sys.platform == "win32" and not sys.stdin.isatty():
        return "thread"
    return "process"


def _make_executor(max_workers: int):
    """Construct the parallel-parse executor selected by [_select_executor_kind]."""
    if _select_executor_kind() == "thread":
        return concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    return concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)

logger = logging.getLogger(__name__)


def _run_rescript_resolver(store: GraphStore) -> Optional[dict]:
    """Run the ReScript cross-module resolver, swallowing any failure so
    build never fails because of it. Returns stats or None on error.
    """
    try:
        from .languages.rescript import resolve_rescript_cross_module
        return resolve_rescript_cross_module(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("ReScript cross-module resolver failed: %s", exc)
        return None


def _run_spring_resolver(store: GraphStore) -> Optional[dict]:
    """Run the Spring DI call resolver, swallowing any failure so
    build never fails because of it. Returns stats or None on error.
    """
    try:
        from .languages.spring import resolve_spring_di_calls
        return resolve_spring_di_calls(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Spring DI resolver failed: %s", exc)
        return None


def _run_temporal_resolver(store: GraphStore) -> Optional[dict]:
    """Run the Temporal workflow/activity call resolver, swallowing any failure so
    build never fails because of it. Returns stats or None on error.
    """
    try:
        from .languages.temporal import resolve_temporal_calls
        return resolve_temporal_calls(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Temporal resolver failed: %s", exc)
        return None


def _run_receiver_resolver(store: GraphStore) -> Optional[dict]:
    """Resolve member calls whose receiver has a declared type.

    Swallows failures so a resolver bug can never fail a build.
    """
    try:
        from .receivers import resolve_typed_receivers
        return resolve_typed_receivers(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Typed receiver resolver failed: %s", exc)
        return None

# Default ignore patterns (in addition to .gitignore).
#
# `<dir>/**` patterns are matched at any depth by _should_ignore, so
# `node_modules/**` also excludes `packages/app/node_modules/react/index.js`
# inside monorepos. See: #91
DEFAULT_IGNORE_PATTERNS = [
    ".code-graph/**",
    "node_modules/**",
    ".git/**",
    ".svn/**",
    "__pycache__/**",
    "*.pyc",
    ".venv/**",
    "venv/**",
    "dist/**",
    "build/**",
    ".next/**",
    "target/**",
    # PHP / Laravel / Composer
    "vendor/**",
    "bootstrap/cache/**",
    "public/build/**",
    # Ruby / Bundler
    ".bundle/**",
    # Java / Kotlin / Gradle
    ".gradle/**",
    "*.jar",
    # Dart / Flutter
    ".dart_tool/**",
    ".pub-cache/**",
    # General
    "coverage/**",
    ".cache/**",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "*.db",
    "*.sqlite",
    "*.db-journal",
    "*.db-wal",
]


def find_svn_root(start: Path | None = None) -> Optional[Path]:
    """Walk up from start to find the SVN working copy root.

    For SVN 1.7+, there is a single ``.svn`` at the WC root.
    For older SVN, every directory has ``.svn`` — we return the topmost one
    found so that the WC root is correctly identified.
    """
    current = start or Path.cwd()
    candidate: Optional[Path] = None
    while current != current.parent:
        if (current / ".svn").exists():
            candidate = current
        current = current.parent
    if (current / ".svn").exists():
        candidate = current
    return candidate


def find_repo_root(
    start: Path | None = None,
    stop_at: Path | None = None,
) -> Optional[Path]:
    """Walk up from ``start`` to find the nearest ``.git`` directory or SVN working copy root.

    Args:
        start: Starting directory.  Defaults to ``Path.cwd()``.
        stop_at: Optional boundary — if provided, the walk examines
            ``stop_at`` for a ``.git`` directory and then stops without
            crossing above it.  Useful for tests that create a synthetic
            repo under ``tmp_path`` (so the walk does not accidentally
            climb into a developer's home-directory dotfiles repo) and
            for any production caller that wants to bound the ancestor
            walk — e.g. CI containers with
            bind-mounted volumes, embedded sandboxes.  See #241.

    Returns:
        The first ancestor containing ``.git`` or an SVN working copy,
        or ``None`` if no ancestor up to and including ``stop_at`` (when
        set) or the filesystem root (when ``stop_at is None``) contains one.
    """
    current = start or Path.cwd()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        if stop_at is not None and current == stop_at:
            return None
        current = current.parent
    if (current / ".git").exists():
        return current
    # No Git root found — try SVN
    return find_svn_root(start)


def detect_vcs(root: Path) -> str:
    """Return ``'git'``, ``'svn'``, or ``'none'`` based on VCS markers at *root*."""
    if (root / ".git").exists():
        return "git"
    if (root / ".svn").exists():
        return "svn"
    return "none"


def find_project_root(
    start: Path | None = None,
    stop_at: Path | None = None,
) -> Path:
    """Find the project root.

    Resolution order (highest precedence first):

    1. ``CODE_GRAPH_REPO_ROOT`` environment variable — explicit override for
       anyone scripting the CLI from outside the repo (CI jobs, daemons,
       CI jobs). See: #155
    2. Git repository root via :func:`find_repo_root` from ``start``,
       honoring ``stop_at`` if provided.
    3. ``start`` itself (or cwd if no start given).

    ``stop_at`` is forwarded to :func:`find_repo_root` so callers that
    want to bound the ancestor walk (typically tests; see #241) can do so
    without having to call ``find_repo_root`` directly.
    """
    env_override = os.environ.get("CODE_GRAPH_REPO_ROOT", "").strip()
    if env_override:
        p = Path(env_override).expanduser().resolve()
        if p.exists():
            return p
    root = find_repo_root(start, stop_at=stop_at)
    if root:
        return root
    return start or Path.cwd()


def _write_data_dir_gitignore(data_dir: Path) -> None:
    """Write .gitignore file in data directory if it doesn't exist.

    The gitignore contains a single '*' to prevent accidental commits.
    """
    inner_gitignore = data_dir / ".gitignore"
    if not inner_gitignore.exists():
        try:
            # `encoding="utf-8"` is REQUIRED — the em-dash in the header is
            # U+2014 which falls outside cp1252.  On Windows, calling
            # write_text without an encoding silently uses the system default
            # codepage, producing a file that subsequently fails to decode as
            # UTF-8 (see issue #239).
            inner_gitignore.write_text(
                "# Auto-generated by code-graph — do not commit database files.\n"
                "# The graph.db contains absolute paths and code structure metadata.\n"
                "*\n",
                encoding="utf-8",
            )
        except OSError:
            # Data dir might be read-only (rare); that's OK, it's a best-effort guard.
            pass


def get_data_dir(repo_root: Path) -> Path:
    """Return the directory where this project's graph data lives.

    Resolution priority:
    1. CODE_GRAPH_DATA_DIR environment variable (global override)
    2. Default: <repo>/.code-graph/

    By default, ``<repo_root>/.code-graph``. If the
    ``CODE_GRAPH_DATA_DIR`` environment variable is set, it is used verbatim
    instead — letting you keep graphs outside the working tree (useful
    for ephemeral workspaces, Docker volumes, or shared caches). See: #155

    The directory is created if it does not already exist; an inner
    ``.gitignore`` (with ``*``) is written so any accidentally-nested
    files never get committed. Both are idempotent.
    """
    # Check environment variable
    env_override = os.environ.get("CODE_GRAPH_DATA_DIR", "").strip()
    if env_override:
        data_dir = Path(env_override).expanduser().resolve()
    else:
        data_dir = repo_root / ".code-graph"

    data_dir.mkdir(parents=True, exist_ok=True)
    _write_data_dir_gitignore(data_dir)

    return data_dir


def get_db_path(repo_root: Path) -> Path:
    """Determine the database path for a repository.

    Respects ``CODE_GRAPH_DATA_DIR`` (see :func:`get_data_dir`). Migrates a
    legacy top-level ``.code-graph.db`` file into the new
    directory when it exists (WAL/SHM side-files are discarded).
    """
    crg_dir = get_data_dir(repo_root)
    new_db = crg_dir / "graph.db"

    # Migrate legacy database if present (only meaningful when the
    # legacy file sits at the repo root — if CODE_GRAPH_DATA_DIR is set we
    # skip the migration because there's no relationship between the
    # legacy location and the new one).
    legacy_db = repo_root / ".code-graph.db"
    if legacy_db.exists() and not new_db.exists():
        legacy_db.rename(new_db)
    # Discard stale WAL/SHM side-files from the old location
    for suffix in ("-wal", "-shm", "-journal"):
        side = repo_root / f".code-graph.db{suffix}"
        if side.exists():
            side.unlink()

    return new_db


def ensure_repo_gitignore_excludes_crg(repo_root: Path) -> str:
    """Ensure repo-level .gitignore excludes ``.code-graph/``.

    Returns one of:
    - ``created``: .gitignore was created with the entry
    - ``updated``: entry was appended to existing .gitignore
    - ``already-present``: no changes were needed
    """
    gitignore_path = repo_root / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""

    for raw_line in existing.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == ".code-graph" or line.startswith(".code-graph/"):
            return "already-present"

    block = "# Added by code-graph\n.code-graph/\n"
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    gitignore_path.write_text(existing + prefix + block, encoding="utf-8")

    if existing:
        return "updated"
    return "created"


def _load_ignore_patterns(repo_root: Path) -> list[str]:
    """Load ignore patterns from .code-graphignore file."""
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    ignore_file = repo_root / ".code-graphignore"
    if ignore_file.exists():
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def _is_match(path: str, pattern: str) -> bool:
    """Check if a path matches a pattern (handles glob and nested dir)."""
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        # Only single-segment dir patterns (no "/" inside the prefix)
        # qualify for nested matching.
        # Example: 'node_modules/**' matches 'packages/app/node_modules/react/index.js'
        if "/" not in prefix and prefix:
            if prefix in PurePosixPath(path).parts:
                return True
    return False


def _should_ignore(path: str, patterns: list[str]) -> bool:
    """Check if a path matches any ignore pattern, respecting negation (!) overrides.

    Patterns are processed in order; the FIRST match wins and terminates the check.
    Handles nested occurrences of ``<dir>/**`` patterns. See: #91

    Important: Because of first-match-wins behavior, negation patterns (`!`) must
    be placed BEFORE the broader ignore patterns that would otherwise match them.
    Example:
        - Correct: ["!node_modules/keep.py", "node_modules/**"] -> keeps keep.py
        - Incorrect: ["node_modules/**", "!node_modules/keep.py"] -> ignores keep.py
    """
    for p in patterns:
        negated = p.startswith("!")
        pattern = p[1:] if negated else p

        if _is_match(path, pattern):
            return not negated

    return False


def _is_binary(path: Path) -> bool:
    """Quick heuristic: check if file appears to be binary."""
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True


_GIT_TIMEOUT = int(os.environ.get("CODE_GRAPH_GIT_TIMEOUT", "30"))  # seconds, configurable

# When True, `git ls-files --recurse-submodules` is used so that files
# inside git submodules are included in the graph.  Opt-in via env var;
# can also be overridden per-call through function parameters.
_RECURSE_SUBMODULES = os.environ.get("CODE_GRAPH_RECURSE_SUBMODULES", "").lower() in ("1", "true", "yes")


def _git_branch_info(repo_root: Path) -> tuple[str, str]:
    """Return (branch_name, head_sha) for the current repo state."""
    branch = ""
    sha = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True, encoding='utf-8',            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True, encoding='utf-8',            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return branch, sha


def _svn_revision_info(repo_root: Path) -> tuple[str, str]:
    """Return (branch_path, revision_str) for the current SVN working copy."""
    branch = ""
    rev = ""
    try:
        result = subprocess.run(
            ["svn", "info", "--non-interactive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("URL: "):
                    url = line[5:].strip()
                    # Extract trunk/branches/tags segment from SVN URL
                    for marker in ("/branches/", "/tags/", "/trunk"):
                        if marker in url:
                            idx = url.index(marker)
                            branch = url[idx:].lstrip("/")
                            break
                    if not branch and url:
                        branch = url.rstrip("/").split("/")[-1]
                elif line.startswith("Revision: "):
                    rev = line[10:].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return branch, rev


_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9_.~^/@{}\-]+$")
_SAFE_SVN_REV = re.compile(r"^r?\d+(:r?\d+|:HEAD|:BASE|:COMMITTED)?$", re.IGNORECASE)


def _store_vcs_metadata(repo_root: Path, store: "GraphStore") -> None:
    """Persist VCS branch/revision info into the graph metadata table."""
    vcs = detect_vcs(repo_root)
    if vcs == "git":
        branch, sha = _git_branch_info(repo_root)
        if branch:
            store.set_metadata("git_branch", branch)
        if sha:
            store.set_metadata("git_head_sha", sha)
    elif vcs == "svn":
        branch, rev = _svn_revision_info(repo_root)
        if branch:
            store.set_metadata("svn_branch", branch)
        if rev:
            store.set_metadata("svn_revision", rev)


def get_changed_files(repo_root: Path, base: str = "HEAD~1") -> list[str]:
    """Get list of changed files via git diff or svn status.

    For SVN working copies the *base* parameter is ignored; modified/added/
    deleted files are detected from ``svn status``.  Pass an SVN revision
    range (e.g. ``"r100:HEAD"``) as *base* to compare against a specific
    revision instead.
    """
    if detect_vcs(repo_root) == "svn":
        return _get_svn_changed_files(repo_root, base if _SAFE_SVN_REV.match(base) else None)
    # Git path
    if not _SAFE_GIT_REF.match(base):
        logger.warning("Invalid git ref rejected: %s", base)
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base, "--"],
            capture_output=True,
            text=True, encoding='utf-8',            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            # Fallback: try diff against empty tree (initial commit)
            result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True,
                text=True, encoding='utf-8',                cwd=str(repo_root),
                timeout=_GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        return files
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _get_svn_changed_files(repo_root: Path, rev_range: str | None = None) -> list[str]:
    """Return changed files in an SVN working copy.

    When *rev_range* is given (e.g. ``"r100:HEAD"``), ``svn diff --summarize``
    is used to list files changed between those revisions.  Otherwise
    ``svn status`` reports working-copy modifications.
    """
    try:
        if rev_range:
            result = subprocess.run(
                ["svn", "diff", "--summarize", "--non-interactive", "-r", rev_range],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(repo_root), timeout=_GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                logger.warning("svn diff --summarize failed (rc=%d): %s",
                               result.returncode, result.stderr[:200])
                return []
            files = []
            for line in result.stdout.splitlines():
                # Format: "M       path/to/file"  (first char is status)
                if len(line) >= 2 and line[0] in ("M", "A", "D"):
                    files.append(line[1:].strip())
            return files
        else:
            result = subprocess.run(
                ["svn", "status", "--non-interactive"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(repo_root), timeout=_GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
            files = []
            for line in result.stdout.splitlines():
                if len(line) < 2:
                    continue
                status_char = line[0]
                # M=modified, A=added, D=deleted, R=replaced, C=conflicted
                if status_char in ("M", "A", "D", "R", "C"):
                    # SVN status: 8 fixed-width columns then the path
                    path = line[8:].strip() if len(line) > 8 else line[1:].strip()
                    files.append(path)
            return files
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_staged_and_unstaged(repo_root: Path) -> list[str]:
    """Get all modified files (staged + unstaged + untracked)."""
    if detect_vcs(repo_root) == "svn":
        return _get_svn_changed_files(repo_root)
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True, encoding='utf-8',            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        files = []
        for line in result.stdout.splitlines():
            if len(line) > 3:
                entry = line[3:].strip()
                # Handle renamed files: "R  old -> new"
                if " -> " in entry:
                    entry = entry.split(" -> ", 1)[1]
                files.append(entry)
        return files
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_all_tracked_files(
    repo_root: Path,
    recurse_submodules: bool | None = None,
) -> list[str]:
    """Get all files tracked by git or svn.

    Args:
        repo_root: Repository root directory.
        recurse_submodules: If True, pass ``--recurse-submodules`` to
            ``git ls-files`` so that files inside git submodules are
            included.  When *None* (default), falls back to the
            ``CODE_GRAPH_RECURSE_SUBMODULES`` environment variable.
            (Ignored for SVN working copies.)
    """
    if detect_vcs(repo_root) == "svn":
        return _get_svn_all_tracked_files(repo_root)

    if recurse_submodules is None:
        recurse_submodules = _RECURSE_SUBMODULES

    cmd = ["git", "ls-files"]
    if recurse_submodules:
        cmd.append("--recurse-submodules")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True, encoding='utf-8',            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _get_svn_all_tracked_files(repo_root: Path) -> list[str]:
    """Return SVN-versioned files by walking the working copy.

    Uses ``svn list -R`` to get the server-side file list, falling back to
    a filesystem walk (which is also the fallback in :func:`collect_all_files`).
    """
    try:
        result = subprocess.run(
            ["svn", "list", "--recursive", "--non-interactive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=60,  # svn list queries the server
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            # svn list returns paths relative to the WC URL; directories end with "/"
            files = [
                f.strip()
                for f in result.stdout.splitlines()
                if f.strip() and not f.strip().endswith("/")
            ]
            if files:
                return files
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: let collect_all_files do a filesystem walk
    return []


def collect_all_files(
    repo_root: Path,
    recurse_submodules: bool | None = None,
) -> list[str]:
    """Collect all parseable files in the repo, respecting ignore and include patterns.

    Args:
        repo_root: Repository root directory.
        recurse_submodules: If True, include files from git submodules.
            When *None*, falls back to ``CODE_GRAPH_RECURSE_SUBMODULES`` env var.
        include_patterns: Optional list of patterns to explicitly include.
            If provided, only files matching one or more of these patterns
            (and not ignored) will be collected.
    """
    ignore_patterns = _load_ignore_patterns(repo_root)
    # Prune patterns are only the leading non-negated patterns.
    # We stop as soon as we see a "!" because we can no longer safely
    # prune a directory if a later pattern might "rescue" a file inside it.
    prune_patterns = []
    for p in ignore_patterns:
        if p.startswith("!"):
            break
        prune_patterns.append(p)

    parser = CodeParser(repo_root)
    files = []

    def _get_candidates():
        # 2. Fallback: walk directory (lazy generator with pruning)
        repo_root_str = str(repo_root)
        for root, dirs, _files in os.walk(repo_root_str):
            # Calculate relative path for matching
            rel_root = os.path.relpath(root, repo_root_str)
            if rel_root == ".":
                rel_root = ""

            # Prune ignored directories in-place!
            # Use prune_patterns (ignoring negations) for safe subtree skipping.
            dirs[:] = [
                d for d in dirs
                if not _should_ignore(os.path.join(rel_root, d), prune_patterns)
            ]

            for f in _files:
                rel_path = os.path.join(rel_root, f)
                yield rel_path


    # Prefer git ls-files for tracked files
    tracked = None
    if (repo_root / ".git").exists():
        tracked = get_all_tracked_files(repo_root, recurse_submodules)

    if tracked:
        candidates = tracked
    else:
        # Fallback: walk directory
        candidates = list(_get_candidates())

    for rel_path in candidates:
        if _should_ignore(rel_path, ignore_patterns):
            continue
        # Skip paths that would exceed OS filename limits (macOS: 255 bytes
        # per component, ~1024 total; Windows: 260 total).
        try:
            full_path = repo_root / rel_path
        except (OSError, ValueError):
            logger.debug("Skipping path that cannot be constructed: %s", rel_path)
            continue
        if len(str(full_path)) > 1000 or any(len(p.encode()) > 255 for p in full_path.parts):
            logger.debug("Skipping overlong path: %s", rel_path[:120])
            continue
        if not full_path.is_file():
            continue
        if full_path.is_symlink():
            continue
        if _is_binary(full_path):
            continue
        if parser.detect_language(full_path) is None:
            continue
            
        files.append(rel_path)

    return files


_MAX_DEPENDENT_HOPS = int(os.environ.get("CODE_GRAPH_DEPENDENT_HOPS", "2"))
_MAX_DEPENDENT_FILES = 500


def _single_hop_dependents(store: GraphStore, file_path: str) -> set[str]:
    """Find files that directly depend on *file_path* (single hop)."""
    dependents: set[str] = set()
    edges = store.get_edges_by_target(file_path)
    for e in edges:
        if e.kind == "IMPORTS_FROM":
            dependents.add(e.file_path)

    nodes = store.get_nodes_by_file(file_path)
    for node in nodes:
        for e in store.get_edges_by_target(node.qualified_name):
            if e.kind in ("CALLS", "IMPORTS_FROM", "INHERITS", "IMPLEMENTS"):
                dependents.add(e.file_path)

    dependents.discard(file_path)
    return dependents


class DependentList(list):
    """A ``list[str]`` with a ``.truncated`` flag.

    When :func:`find_dependents` hits ``_MAX_DEPENDENT_FILES`` it truncates
    the result and sets ``truncated = True`` so callers can distinguish a
    complete expansion from a capped one.  See issue #261.

    This is a transparent ``list`` subclass — existing callers that iterate,
    ``len()``, or slice continue to work unchanged; only callers that
    specifically check ``.truncated`` benefit from the signal.
    """

    truncated: bool

    def __init__(self, items: list, *, truncated: bool = False) -> None:
        super().__init__(items)
        self.truncated = truncated


def find_dependents(
    store: GraphStore,
    file_path: str,
    max_hops: int = _MAX_DEPENDENT_HOPS,
) -> DependentList:
    """Find files that import from or depend on the given file.

    Performs up to *max_hops* iterations of expansion (default 2).
    Stops early if the total exceeds 500 files.

    Returns a :class:`DependentList` — a regular ``list[str]`` that also
    carries a ``.truncated`` flag.  When ``truncated is True`` the
    returned list is capped at ``_MAX_DEPENDENT_FILES`` and the full
    set of dependents was not explored.  See issue #261.
    """
    all_dependents: set[str] = set()
    visited: set[str] = {file_path}
    frontier: set[str] = {file_path}
    for _hop in range(max_hops):
        next_frontier: set[str] = set()
        for fp in frontier:
            deps = _single_hop_dependents(store, fp)
            new_deps = deps - visited
            all_dependents.update(new_deps)
            next_frontier.update(new_deps)
        visited.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
        if len(all_dependents) > _MAX_DEPENDENT_FILES:
            logger.warning(
                "Dependent expansion capped at %d files for %s",
                len(all_dependents),
                file_path,
            )
            return DependentList(
                list(all_dependents)[:_MAX_DEPENDENT_FILES],
                truncated=True,
            )
    return DependentList(list(all_dependents))


def _parse_single_file(
    args: tuple[str, str],
) -> tuple[str, list, list, str | None, str]:
    """Parse one file in a worker process.

    Returns ``(rel_path, nodes, edges, error_or_none, file_hash)``.
    Must be a module-level function so ``ProcessPoolExecutor`` can
    serialise it across processes.
    """
    rel_path, repo_root_str = args
    abs_path = Path(repo_root_str) / rel_path
    try:
        raw = abs_path.read_bytes()
        fhash = hashlib.sha256(raw).hexdigest()
        parser = CodeParser(Path(repo_root_str))
        nodes, edges = parser.parse_bytes(abs_path, raw)
        return (rel_path, nodes, edges, None, fhash)
    except Exception as e:
        return (rel_path, [], [], str(e), "")


def _store_batch_size() -> int:
    """Files per write transaction, from ``CODE_GRAPH_STORE_BATCH_SIZE``.

    ``1`` (the default) keeps the historical per-file transaction. Larger
    values buffer parsed files and hand them to ``store_file_batch``, trading
    memory for fewer commits on big repos.
    """
    raw = os.environ.get("CODE_GRAPH_STORE_BATCH_SIZE", "1").strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring CODE_GRAPH_STORE_BATCH_SIZE=%r, expected an integer", raw,
        )
        return 1
    return max(1, value)


class _PendingWrites:
    """Buffer parsed files and flush them through the store in batches."""

    def __init__(self, store: GraphStore, batch_size: int) -> None:
        self._store = store
        self._batch_size = batch_size
        self._pending: list[tuple[str, list[NodeInfo], list[EdgeInfo], str]] = []

    def add(
        self,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        fhash: str,
    ) -> None:
        if self._batch_size == 1:
            self._store.store_file_nodes_edges(file_path, nodes, edges, fhash)
            return
        self._pending.append((file_path, nodes, edges, fhash))
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        # Hand over the list and start a new one, so a caller holding the
        # batch (a test, a wrapper) does not see it emptied underneath it.
        pending, self._pending = self._pending, []
        self._store.store_file_batch(pending)


def full_build(
    repo_root: Path,
    store: GraphStore,
    recurse_submodules: bool | None = None,
) -> dict:
    """Full rebuild of the entire graph.

    Args:
        repo_root: Repository root directory.
        store: Graph database store.
        recurse_submodules: If True, include files from git submodules.
            When *None*, falls back to ``CODE_GRAPH_RECURSE_SUBMODULES`` env var.
    """
    parser = CodeParser(repo_root)
    files = collect_all_files(repo_root, recurse_submodules)

    # Purge stale data from files no longer on disk
    existing_files = set(store.get_all_files())
    current_abs = {str(repo_root / f) for f in files}
    stale_files = existing_files - current_abs
    for stale in stale_files:
        store.remove_file_data(stale)
    # Ensure deletions are persisted before store_file_nodes_edges()
    # starts its own explicit transaction via BEGIN IMMEDIATE.
    if stale_files:
        store.commit()

    total_nodes = 0
    total_edges = 0
    errors = []
    file_count = len(files)

    use_serial = os.environ.get("CODE_GRAPH_SERIAL_PARSE", "") == "1"
    writer = _PendingWrites(store, _store_batch_size())

    if use_serial or file_count < 8:
        # Serial fallback (for debugging or tiny repos)
        for i, rel_path in enumerate(files, 1):
            full_path = repo_root / rel_path
            try:
                source = full_path.read_bytes()
                fhash = hashlib.sha256(source).hexdigest()
                nodes, edges = parser.parse_bytes(full_path, source)
                writer.add(str(full_path), nodes, edges, fhash)
                total_nodes += len(nodes)
                total_edges += len(edges)
            except (OSError, PermissionError) as e:
                errors.append({"file": rel_path, "error": str(e)})
            except Exception as e:
                logger.warning("Error parsing %s: %s", rel_path, e)
                errors.append({"file": rel_path, "error": str(e)})
            if i % 50 == 0 or i == file_count:
                logger.info("Progress: %d/%d files parsed", i, file_count)
    else:
        # Parallel parsing — store calls remain serial (SQLite single-writer).
        # Executor kind auto-selected: process on Linux/macOS/Windows-TTY,
        # thread on Windows non-TTY stdio to avoid pipe-handle inheritance
        # deadlock (issues #46, #136). Override via CODE_GRAPH_PARSE_EXECUTOR env.
        args_list = [(rel_path, str(repo_root)) for rel_path in files]
        with _make_executor(_MAX_PARSE_WORKERS) as executor:
            for i, (rel_path, nodes, edges, error, fhash) in enumerate(
                executor.map(_parse_single_file, args_list, chunksize=20),
                1,
            ):
                if error:
                    logger.warning("Error parsing %s: %s", rel_path, error)
                    errors.append({"file": rel_path, "error": error})
                    continue
                full_path = repo_root / rel_path
                writer.add(str(full_path), nodes, edges, fhash)
                total_nodes += len(nodes)
                total_edges += len(edges)
                if i % 200 == 0 or i == file_count:
                    logger.info("Progress: %d/%d files parsed", i, file_count)

    writer.flush()
    store.set_metadata("last_updated", time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.set_metadata("last_build_type", "full")
    _store_vcs_metadata(repo_root, store)
    store.commit()

    rescript_stats = _run_rescript_resolver(store)
    spring_stats = _run_spring_resolver(store)
    temporal_stats = _run_temporal_resolver(store)
    receiver_stats = _run_receiver_resolver(store)

    return {
        "files_parsed": len(files),
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "errors": errors,
        "rescript_resolution": rescript_stats,
        "spring_resolution": spring_stats,
        "temporal_resolution": temporal_stats,
        "receiver_resolution": receiver_stats,
    }


def incremental_update(
    repo_root: Path,
    store: GraphStore,
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
) -> dict:
    """Incremental update: re-parse changed + dependent files only."""
    parser = CodeParser(repo_root)
    ignore_patterns = _load_ignore_patterns(repo_root)

    # Determine changed files
    if changed_files is None:
        changed_files = get_changed_files(repo_root, base)
    print(f"Changed files: {changed_files}")

    if not changed_files:
        return {
            "files_updated": 0,
            "total_nodes": 0,
            "total_edges": 0,
            "changed_files": [],
            "dependent_files": [],
        }

    # Find dependent files (files that import from changed files)
    dependent_files: set[str] = set()
    for rel_path in changed_files:
        full_path = str(repo_root / rel_path)
        deps = find_dependents(store, full_path)
        for d in deps:
            # Convert back to relative path if needed
            try:
                dependent_files.add(str(Path(d).relative_to(repo_root)))
            except ValueError:
                dependent_files.add(d)

    # Combine changed + dependent
    all_files = set(changed_files) | dependent_files

    total_nodes = 0
    total_edges = 0
    errors = []

    # Separate deleted/unparseable files from files that need re-parsing
    to_parse: list[str] = []
    removed_any = False
    for rel_path in all_files:
        if _should_ignore(rel_path, ignore_patterns):
            continue
        abs_path = repo_root / rel_path
        if not abs_path.is_file():
            store.remove_file_data(str(abs_path))
            removed_any = True
            continue
        if parser.detect_language(abs_path) is None:
            continue
        # Quick hash check to skip unchanged files
        try:
            raw = abs_path.read_bytes()
            fhash = hashlib.sha256(raw).hexdigest()
            existing_nodes = store.get_nodes_by_file(str(abs_path))
            if existing_nodes and existing_nodes[0].file_hash == fhash:
                continue
        except (OSError, PermissionError):
            pass
        to_parse.append(rel_path)

    # Persist deletions before store_file_nodes_edges() opens its own
    # explicit transaction — avoids nested transaction errors.
    if removed_any:
        store.commit()

    use_serial = os.environ.get("CODE_GRAPH_SERIAL_PARSE", "") == "1"
    writer = _PendingWrites(store, _store_batch_size())

    if use_serial or len(to_parse) < 8:
        for rel_path in to_parse:
            abs_path = repo_root / rel_path
            try:
                source = abs_path.read_bytes()
                fhash = hashlib.sha256(source).hexdigest()
                nodes, edges = parser.parse_bytes(abs_path, source)
                writer.add(str(abs_path), nodes, edges, fhash)
                total_nodes += len(nodes)
                total_edges += len(edges)
            except (OSError, PermissionError) as e:
                errors.append({"file": rel_path, "error": str(e)})
            except Exception as e:
                logger.warning("Error parsing %s: %s", rel_path, e)
                errors.append({"file": rel_path, "error": str(e)})
    else:
        # See full-build comment above for executor kind rationale.
        args_list = [(rel_path, str(repo_root)) for rel_path in to_parse]
        with _make_executor(_MAX_PARSE_WORKERS) as executor:
            for rel_path, nodes, edges, error, fhash in executor.map(
                _parse_single_file,
                args_list,
                chunksize=20,
            ):
                if error:
                    logger.warning("Error parsing %s: %s", rel_path, error)
                    errors.append({"file": rel_path, "error": error})
                    continue
                writer.add(str(repo_root / rel_path), nodes, edges, fhash)
                total_nodes += len(nodes)
                total_edges += len(edges)

    writer.flush()
    store.set_metadata("last_updated", time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.set_metadata("last_build_type", "incremental")
    _store_vcs_metadata(repo_root, store)
    store.commit()

    # Only re-run language-specific resolvers when the relevant files changed.
    rescript_changed = any(
        rp.endswith((".res", ".resi")) for rp in all_files
    )
    rescript_stats = (
        _run_rescript_resolver(store) if rescript_changed else None
    )

    spring_changed = any(rp.endswith(".java") for rp in all_files)
    spring_stats = _run_spring_resolver(store) if spring_changed else None
    temporal_stats = _run_temporal_resolver(store) if spring_changed else None
    receiver_stats = _run_receiver_resolver(store)

    return {
        "files_updated": len(all_files),
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "changed_files": list(changed_files),
        "dependent_files": list(dependent_files),
        "errors": errors,
        "rescript_resolution": rescript_stats,
        "spring_resolution": spring_stats,
        "temporal_resolution": temporal_stats,
        "receiver_resolution": receiver_stats,
    }
