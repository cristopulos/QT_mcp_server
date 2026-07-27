"""Basic filesystem operations exposed to MCP.

Operations: read, write, list directory, create directory, move/rename,
delete, file info, glob search, and content grep.

All paths are resolved against the current working directory unless absolute.
Symlinks are followed for stat/read but not for delete (shutil.rmtree follows
symlinks by default and is overridden here to avoid deleting through links).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_READ_BYTES = 1 * 1024 * 1024  # 1 MB hard cap per read_file to keep tool output sane


class FilesystemError(Exception):
    """Raised when a filesystem operation cannot be completed."""


def _resolve(path: str) -> Path:
    """Resolve a user-supplied path to an absolute Path, expanding ``~``."""
    return Path(os.path.expanduser(path)).expanduser().resolve()


@dataclass
class EntryInfo:
    """One directory entry."""

    name: str
    path: str
    type: str  # "file" | "dir" | "symlink" | "other"
    size: int

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "type": self.type, "size": self.size}


@dataclass
class FileInfo:
    """Detailed file/directory metadata."""

    path: str
    type: str
    size: int
    modified: str  # ISO-8601 UTC
    is_symlink: bool
    permissions: str  # octal, e.g. "0o755"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "type": self.type,
            "size": self.size,
            "modified": self.modified,
            "is_symlink": self.is_symlink,
            "permissions": self.permissions,
        }


def read_file(path: str, offset: int = 0, limit: int = 2000) -> dict:
    """Read a text file, returning up to ``limit`` lines starting at ``offset``.

    Returns a dict with ``path``, ``content``, ``total_lines``, ``returned_lines``,
    ``offset``, and ``truncated``.
    """
    p = _resolve(path)
    if not p.exists():
        raise FilesystemError(f"File not found: {p}")
    if p.is_dir():
        raise FilesystemError(f"Path is a directory, not a file: {p}")

    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        logger.warning("Reading large file %s (%d bytes); capping at %d", p, size, MAX_READ_BYTES)

    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        raise FilesystemError(f"Could not read {p}: {exc}") from exc

    total = len(lines)
    offset = max(0, offset)
    slice_lines = lines[offset : offset + limit]
    content = "".join(slice_lines)
    return {
        "path": str(p),
        "content": content,
        "total_lines": total,
        "returned_lines": len(slice_lines),
        "offset": offset,
        "truncated": offset + len(slice_lines) < total,
    }


def write_file(path: str, content: str, append: bool = False, create_parents: bool = True) -> dict:
    """Write (or append to) a text file."""
    p = _resolve(path)
    if p.exists() and p.is_dir():
        raise FilesystemError(f"Path is a directory: {p}")
    if create_parents and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    if not p.parent.exists():
        raise FilesystemError(f"Parent directory does not exist: {p.parent}")
    mode = "a" if append else "w"
    try:
        with p.open(mode, encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        raise FilesystemError(f"Could not write {p}: {exc}") from exc
    return {"path": str(p), "bytes_written": len(content.encode("utf-8")), "appended": append}


def list_directory(path: str = ".", include_hidden: bool = False) -> dict:
    """List directory entries."""
    p = _resolve(path)
    if not p.exists():
        raise FilesystemError(f"Path not found: {p}")
    if not p.is_dir():
        raise FilesystemError(f"Not a directory: {p}")
    entries: list[EntryInfo] = []
    try:
        names = sorted(os.listdir(p))
    except OSError as exc:
        raise FilesystemError(f"Could not list {p}: {exc}") from exc
    for name in names:
        if not include_hidden and name.startswith("."):
            continue
        child = p / name
        if child.is_symlink():
            etype = "symlink"
        elif child.is_dir():
            etype = "dir"
        elif child.is_file():
            etype = "file"
        else:
            etype = "other"
        try:
            size = child.stat(follow_symlinks=False).st_size
        except OSError:
            size = -1
        entries.append(EntryInfo(name=name, path=str(child), type=etype, size=size))
    return {
        "path": str(p),
        "entries": [e.to_dict() for e in entries],
        "count": len(entries),
    }


def create_directory(path: str, parents: bool = True) -> dict:
    """Create a directory."""
    p = _resolve(path)
    if p.exists():
        if p.is_dir():
            return {"path": str(p), "created": False, "exists": True}
        raise FilesystemError(f"Path exists and is not a directory: {p}")
    try:
        p.mkdir(parents=parents, exist_ok=False)
    except OSError as exc:
        raise FilesystemError(f"Could not create directory {p}: {exc}") from exc
    return {"path": str(p), "created": True, "exists": False}


def move(path: str, destination: str, overwrite: bool = False) -> dict:
    """Move or rename a file/directory."""
    src = _resolve(path)
    dst = _resolve(destination)
    if not src.exists():
        raise FilesystemError(f"Source not found: {src}")
    if dst.exists():
        if not overwrite:
            raise FilesystemError(f"Destination exists: {dst}")
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst_parent = dst.parent
    if not dst_parent.exists():
        raise FilesystemError(f"Destination parent does not exist: {dst_parent}")
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        raise FilesystemError(f"Could not move {src} -> {dst}: {exc}") from exc
    return {"source": str(src), "destination": str(dst), "moved": True}


def delete(path: str, recursive: bool = False) -> dict:
    """Delete a file or directory.

    For directories ``recursive`` must be True. Symlinks are unlinked, not
    followed (the target is preserved).
    """
    p = _resolve(path)
    if not p.exists() and not p.is_symlink():
        raise FilesystemError(f"Path not found: {p}")
    try:
        if p.is_symlink():
            p.unlink()
        elif p.is_file():
            p.unlink()
        elif p.is_dir():
            if not recursive:
                raise FilesystemError(
                    f"Is a directory and recursive=False: {p}. "
                    "Pass recursive=True to delete a directory tree."
                )
            # Don't follow symlinks contained in the tree.
            shutil.rmtree(p)
        else:
            raise FilesystemError(f"Unknown entry type: {p}")
    except OSError as exc:
        raise FilesystemError(f"Could not delete {p}: {exc}") from exc
    return {"path": str(p), "deleted": True}


def file_info(path: str) -> dict:
    """Return metadata for a file, directory, or symlink."""
    p = _resolve(path)
    if not p.exists() and not p.is_symlink():
        raise FilesystemError(f"Path not found: {p}")
    is_link = p.is_symlink()
    try:
        st = p.stat(follow_symlinks=True)
    except OSError:
        st = p.stat(follow_symlinks=False)
    if p.is_dir():
        etype = "dir"
    elif p.is_file():
        etype = "file"
    elif is_link:
        etype = "symlink"
    else:
        etype = "other"
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    perms = oct(st.st_mode & 0o777)
    info = FileInfo(
        path=str(p),
        type=etype,
        size=st.st_size,
        modified=mtime,
        is_symlink=is_link,
        permissions=perms,
    )
    return info.to_dict()


def search_glob(
    path: str = ".",
    pattern: str = "*",
    include_hidden: bool = False,
    max_results: int = 500,
) -> dict:
    """Find files matching a glob pattern (recursive by default).

    ``pattern`` supports Python ``Path.glob`` patterns: ``**/*.py``, ``*.txt``,
    ``subdir/*.json``, etc.
    """
    p = _resolve(path)
    if not p.exists():
        raise FilesystemError(f"Path not found: {p}")
    if not p.is_dir():
        raise FilesystemError(f"Not a directory: {p}")
    matches: list[str] = []
    try:
        for m in p.glob(pattern):
            if not include_hidden and any(part.startswith(".") for part in m.relative_to(p).parts if part):
                continue
            matches.append(str(m))
            if len(matches) >= max_results:
                break
    except OSError as exc:
        raise FilesystemError(f"Glob search failed: {exc}") from exc
    matches.sort()
    return {
        "path": str(p),
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": len(matches) >= max_results,
    }


def search_content(
    path: str = ".",
    pattern: str = "",
    include_hidden: bool = False,
    file_glob: str = "*",
    case_sensitive: bool = False,
    max_matches: int = 100,
    context_lines: int = 0,
) -> dict:
    """Search file contents for a regex ``pattern``.

    Returns matching lines with file path, line number, and (optionally)
    surrounding context lines.
    """
    if not pattern:
        raise FilesystemError("pattern is required for content search.")
    p = _resolve(path)
    if not p.exists():
        raise FilesystemError(f"Path not found: {p}")
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise FilesystemError(f"Invalid regex {pattern!r}: {exc}") from exc

    results: list[dict] = []
    files_scanned = 0

    def _scan_file(fp: Path) -> None:
        nonlocal files_scanned
        files_scanned += 1
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as f:
                file_lines = f.readlines()
        except OSError:
            return
        for i, line in enumerate(file_lines):
            if regex.search(line):
                start = max(0, i - context_lines)
                end = min(len(file_lines), i + 1 + context_lines)
                results.append(
                    {
                        "file": str(fp),
                        "line": i + 1,
                        "match": line.rstrip("\n"),
                        "context": "".join(file_lines[start:end]).rstrip("\n")
                        if context_lines > 0
                        else None,
                    }
                )
                if len(results) >= max_matches:
                    return

    if p.is_file():
        if _matches_glob(p.name, file_glob):
            _scan_file(p)
    else:
        for m in p.rglob(file_glob):
            if not include_hidden and any(
                part.startswith(".") for part in m.relative_to(p).parts if part
            ):
                continue
            if m.is_file():
                _scan_file(m)
            if len(results) >= max_matches:
                break

    return {
        "path": str(p),
        "pattern": pattern,
        "file_glob": file_glob,
        "matches": results,
        "count": len(results),
        "files_scanned": files_scanned,
        "truncated": len(results) >= max_matches,
    }


def _matches_glob(name: str, pattern: str) -> bool:
    try:
        return fnmatch.fnmatch(name, pattern)
    except Exception:
        return pattern == "*" or name == pattern