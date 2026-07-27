"""Qt MCP server entrypoint.

Exposes two tool groups over stdio using the Model Context Protocol:

  1. Qt screenshot tools — list windows, capture a window, capture a screen
     region (optionally relative to a window), and a placeholder for Qt-internal
     widget capture.
  2. Filesystem tools — read, write, list, mkdir, move, delete, file info,
     glob search, and content grep.

The screenshot tools are Linux/X11 oriented (wmctrl + xwininfo + mss).  They
fall back to ImageMagick ``import`` if ``mss`` cannot grab the display.

Run as a stdio MCP server::

    python -m qt_mcp.server

or via the installed ``qt-mcp`` console script.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from . import filesystem as fs
from . import screenshots as shot

__version__ = "0.1.0"

# stdout is reserved for the MCP JSON-RPC protocol; all diagnostics go to stderr.
logging.basicConfig(
    level=os.environ.get("QT_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("qt_mcp.server")

mcp = FastMCP("qt-mcp")


# ---------------------------------------------------------------------------
# Screenshot tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_windows() -> dict:
    """List all visible top-level X11 windows with id, title, and geometry.

    Use this first to discover the exact window title you need for
    capture_window or capture_region. Each entry has: id, title, desktop,
    x, y, width, height.
    """
    try:
        windows = shot.list_windows()
    except shot.CaptureError as exc:
        raise ToolError(str(exc)) from exc
    return {"windows": [w.to_dict() for w in windows], "count": len(windows)}


@mcp.tool()
def capture_window(title: str, exact: bool = False) -> Image:
    """Capture an entire window identified by its title (case-insensitive substring match).

    Args:
        title: Window title or substring of it (case-insensitive). Use
            list_windows to find candidates. When multiple windows match, the
            call fails and lists the matching titles — provide a more specific
            substring or set exact=True.
        exact: If True, match the title exactly (case-sensitive).

    Returns the screenshot as a PNG image. The window's geometry is logged but
    not returned alongside the image (call list_windows for geometry details).
    """
    try:
        result = shot.capture_window(title, exact=exact)
    except shot.CaptureError as exc:
        raise ToolError(str(exc)) from exc
    logger.info(
        "Captured window %r (%dx%d at %d,%d)",
        result["window"]["title"],
        result["window"]["width"],
        result["window"]["height"],
        result["window"]["x"],
        result["window"]["y"],
    )
    return Image(data=result["png"], format="png")


@mcp.tool()
def capture_region(
    x: int,
    y: int,
    width: int,
    height: int,
    title: Optional[str] = None,
) -> Image:
    """Capture a rectangular screen region as a PNG image.

    Args:
        x: Top-left X. Absolute screen coordinate, or relative to the window
            identified by ``title`` when that is given.
        y: Top-left Y. Absolute screen coordinate, or relative to the window
            identified by ``title`` when that is given.
        width: Region width in pixels.
        height: Region height in pixels.
        title: Optional window title. When set, x/y are treated as offsets from
            that window's top-left corner (use list_windows to find the title).

    Returns the region as a PNG image.
    """
    try:
        result = shot.capture_region(x, y, width, height, title=title)
    except shot.CaptureError as exc:
        raise ToolError(str(exc)) from exc
    region = result["region"]
    logger.info(
        "Captured region %dx%d at %d,%d (relative_to_window=%s)",
        region["width"],
        region["height"],
        region["x"],
        region["y"],
        region.get("relative_to_window"),
    )
    return Image(data=result["png"], format="png")


@mcp.tool()
def capture_widget(window_title: str, widget_name: str) -> Image:
    """Capture a specific Qt widget by its object name.

    NOTE: This requires a Qt-internal capture agent loaded inside the target
    Qt application, which is not yet wired up in this server. The call will
    currently return an error explaining how to fall back to capture_region
    with explicit coordinates.
    """
    try:
        result = shot.capture_widget(window_title, widget_name)
    except shot.CaptureError as exc:
        raise ToolError(str(exc)) from exc
    return Image(data=result["png"], format="png")


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(path: str, offset: int = 0, limit: int = 2000) -> dict:
    """Read a text file. Returns up to ``limit`` lines starting at line ``offset`` (1-indexed offset, line 1 = offset 0).

    Args:
        path: File path (absolute, or relative to the server's working dir). ``~`` is expanded.
        offset: Line number to start at (0 = first line). Use this to page through large files.
        limit: Maximum number of lines to return (default 2000).

    Returns: path, content, total_lines, returned_lines, offset, truncated.
    """
    try:
        return fs.read_file(path, offset=offset, limit=limit)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def write_file(
    path: str,
    content: str,
    append: bool = False,
    create_parents: bool = True,
) -> dict:
    """Write or append text to a file.

    Args:
        path: Destination file path. ``~`` is expanded.
        content: Text content to write.
        append: If True, append to the file instead of overwriting.
        create_parents: Create parent directories if they do not exist.

    Returns: path, bytes_written, appended.
    """
    try:
        return fs.write_file(path, content, append=append, create_parents=create_parents)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def list_directory(path: str = ".", include_hidden: bool = False) -> dict:
    """List entries in a directory.

    Args:
        path: Directory path (default: current directory). ``~`` is expanded.
        include_hidden: Include dotfiles/dotdirs (default False).

    Returns: path, entries (name/path/type/size), count.
    """
    try:
        return fs.list_directory(path, include_hidden=include_hidden)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def create_directory(path: str, parents: bool = True) -> dict:
    """Create a directory.

    Args:
        path: Directory path to create. ``~`` is expanded.
        parents: Create parent directories as needed (default True).

    Returns: path, created, exists.
    """
    try:
        return fs.create_directory(path, parents=parents)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def move(path: str, destination: str, overwrite: bool = False) -> dict:
    """Move or rename a file or directory.

    Args:
        path: Source path.
        destination: Destination path.
        overwrite: Overwrite destination if it exists (default False; errors otherwise).

    Returns: source, destination, moved.
    """
    try:
        return fs.move(path, destination, overwrite=overwrite)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def delete(path: str, recursive: bool = False) -> dict:
    """Delete a file or directory.

    Args:
        path: Path to delete. Symlinks are unlinked (their targets are preserved).
        recursive: Required True to delete a directory tree. Files are deleted
            regardless of this flag.

    Returns: path, deleted.
    """
    try:
        return fs.delete(path, recursive=recursive)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def file_info(path: str) -> dict:
    """Return metadata for a file, directory, or symlink.

    Returns: path, type, size, modified (ISO-8601 UTC), is_symlink, permissions.
    """
    try:
        return fs.file_info(path)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def search_glob(
    path: str = ".",
    pattern: str = "*",
    include_hidden: bool = False,
    max_results: int = 500,
) -> dict:
    """Find files matching a glob pattern (recursive).

    Args:
        path: Directory to search in (default current dir).
        pattern: Glob pattern, e.g. ``**/*.py``, ``*.txt``, ``src/**/*.json``.
        include_hidden: Include dotfiles (default False).
        max_results: Cap on number of matches (default 500).

    Returns: path, pattern, matches, count, truncated.
    """
    try:
        return fs.search_glob(
            path, pattern, include_hidden=include_hidden, max_results=max_results
        )
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def search_content(
    path: str = ".",
    pattern: str = "",
    include_hidden: bool = False,
    file_glob: str = "*",
    case_sensitive: bool = False,
    max_matches: int = 100,
    context_lines: int = 0,
) -> dict:
    """Search file contents for a regex pattern (recursive).

    Args:
        path: Directory (or single file) to search.
        pattern: Regex pattern to search for (required).
        include_hidden: Include dotfiles (default False).
        file_glob: Only search files matching this glob (default ``*``, all files).
        case_sensitive: Case-sensitive match (default False).
        max_matches: Cap on number of result lines (default 100).
        context_lines: Lines of context to include around each match (default 0).

    Returns: path, pattern, file_glob, matches, count, files_scanned, truncated.
    """
    try:
        return fs.search_content(
            path,
            pattern,
            include_hidden=include_hidden,
            file_glob=file_glob,
            case_sensitive=case_sensitive,
            max_matches=max_matches,
            context_lines=context_lines,
        )
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Qt MCP server over stdio."""
    logger.info("Starting qt-mcp %s (stdio transport)", __version__)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()