"""Qt MCP server entrypoint.

Exposes three tool groups over stdio using the Model Context Protocol:

  1. Qt screenshot tools — list windows, capture a window, capture a screen
     region (optionally relative to a window), and a placeholder for Qt-internal
     widget capture.
  2. Filesystem tools — read, write, list, mkdir, move, delete, file info,
     glob search, and content grep.
  3. Proxy/agent tools — capture_widget, list_capturable_widgets, and
     attach_status that communicate with a Qt app via a Unix domain socket
     (Linux/macOS) or named pipe (Windows).  These require a Qt app that has
     called ``qt_mcp.agent.start_agent(window)``.

The screenshot tools are Linux/X11 oriented (wmctrl + xwininfo + mss).  They
fall back to ImageMagick ``import`` if ``mss`` cannot grab the display.

Run as a stdio MCP server::

    python -m qt_mcp.server

or via the installed ``qt-mcp`` console script.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from . import filesystem as fs
from . import screenshots as shot

__version__ = "0.3.0"

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
# Proxy/agent tools (Linux/macOS/Windows, require a Qt app with start_agent)
# ---------------------------------------------------------------------------

# Module-level singleton guard for the AgentProxy.
_proxy_instance = None
_proxy_started = False
_proxy_loop = None
_proxy_thread = None


def _get_proxy():
    """Lazily create and start the AgentProxy singleton.

    The proxy runs its own asyncio event loop in a background daemon thread
    so it does not interfere with the MCP server's event loop.

    Returns the proxy or raises ToolError if the platform is not supported.
    """
    global _proxy_instance, _proxy_started, _proxy_loop, _proxy_thread

    if _proxy_started and _proxy_instance is not None:
        return _proxy_instance

    if sys.platform not in ("linux", "darwin", "win32"):
        raise ToolError(
            "capture_widget (proxy) is not supported on this platform; "
            "use the OS-level capture_window tool instead"
        )

    # Lazy import to avoid requiring asyncio socket support at import time.
    from . import agent_proxy as ap

    if _proxy_instance is None:
        _proxy_instance = ap.AgentProxy()

    # Start the proxy in a background thread with its own event loop.
    import threading as _threading

    _proxy_loop = asyncio.new_event_loop()

    def _run_proxy():
        asyncio.set_event_loop(_proxy_loop)
        try:
            _proxy_loop.run_forever()
        except Exception:
            pass

    _proxy_thread = _threading.Thread(target=_run_proxy, name="agent-proxy", daemon=True)
    _proxy_thread.start()

    # Start the server in the proxy's event loop.
    try:
        fut = asyncio.run_coroutine_threadsafe(_proxy_instance.start(), _proxy_loop)
        fut.result(timeout=10.0)
    except ap.AgentError as exc:
        raise ToolError(str(exc)) from exc
    except TimeoutError:
        raise ToolError("Timed out starting agent proxy")
    except OSError as exc:
        # Socket path in use from a prior crash — remove stale socket and retry.
        import os as _os

        if _os.path.exists(_proxy_instance.socket_path):
            _os.unlink(_proxy_instance.socket_path)
            try:
                fut = asyncio.run_coroutine_threadsafe(_proxy_instance.start(), _proxy_loop)
                fut.result(timeout=10.0)
            except Exception as exc2:
                raise ToolError(f"Failed to start agent proxy: {exc2}") from exc2
        else:
            raise ToolError(f"Failed to start agent proxy: {exc}") from exc
    except ImportError as exc:
        # On Windows, PySide6 is required for the QLocalServer backend.
        if "PySide6" in str(exc):
            raise ToolError(
                "Proxy mode on Windows requires PySide6: pip install PySide6"
            ) from exc
        raise

    _proxy_started = True
    return _proxy_instance


def _run_async_in_proxy(coro):
    """Run a coroutine in the proxy's event loop and return the result."""
    global _proxy_loop
    if _proxy_loop is None or not _proxy_loop.is_running():
        raise ToolError("Agent proxy event loop is not running")
    fut = asyncio.run_coroutine_threadsafe(coro, _proxy_loop)
    try:
        return fut.result(timeout=15.0)
    except TimeoutError:
        raise ToolError("Agent proxy request timed out (app may be frozen)")


@mcp.tool()
def capture_widget(widget_name: str) -> Image:
    """Capture a specific Qt widget by its objectName via the proxy agent.

    Requires a Qt application that has called ``qt_mcp.agent.start_agent(window)``
    and is connected to this server's Unix domain socket.

    Args:
        widget_name: The widget's ``objectName``. Use ``list_capturable_widgets``
            to discover valid names.

    Returns the widget as a PNG image.
    """
    proxy = _get_proxy()
    if not proxy.is_attached:
        raise ToolError(
            "No Qt app is attached. Start a Qt app that calls "
            "qt_mcp.agent.start_agent(window)."
        )

    try:
        result = _run_async_in_proxy(proxy.capture_widget(widget_name))
    except Exception as exc:
        raise ToolError(str(exc)) from exc

    png_bytes = base64.b64decode(result["png_b64"])
    logger.info(
        "Proxied capture_widget(%r) -> %dx%d PNG",
        widget_name,
        result["width"],
        result["height"],
    )
    return Image(data=png_bytes, format="png")


@mcp.tool()
def list_capturable_widgets() -> dict:
    """List the objectNames of widgets in the attached Qt app.

    Requires a Qt application that has called ``qt_mcp.agent.start_agent(window)``
    and is connected to this server's Unix domain socket.

    Returns: ``{"widgets": ["name1", ...], "count": N}``.
    """
    proxy = _get_proxy()
    if not proxy.is_attached:
        raise ToolError(
            "No Qt app is attached. Start a Qt app that calls "
            "qt_mcp.agent.start_agent(window)."
        )

    try:
        names = _run_async_in_proxy(proxy.list_capturable_widgets())
    except Exception as exc:
        raise ToolError(str(exc)) from exc

    return {"widgets": names, "count": len(names)}


@mcp.tool()
def attach_status() -> dict:
    """Check whether a Qt app is currently attached via the proxy agent.

    Always works, no error. Useful for clients to poll.

    Returns: ``{"attached": bool, "socket_path": str}``.
    """
    try:
        proxy = _get_proxy()
        return {"attached": proxy.is_attached, "socket_path": proxy.socket_path}
    except ToolError:
        return {"attached": False, "socket_path": ""}
    except Exception:
        return {"attached": False, "socket_path": ""}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Qt MCP server over stdio."""
    logger.info("Starting qt-mcp %s (stdio transport)", __version__)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()