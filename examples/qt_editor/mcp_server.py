"""In-process MCP server for the example Qt editor.

This module owns the concurrency design that lets the MCP stdio server
(in a background thread) call into the Qt GUI thread to capture a specific
widget by name.

Design:
  - ``CaptureBridge`` is a QObject living on the Qt main thread. It owns a
    Qt signal ``capture_requested(name, result_event)``.
  - The MCP tool ``capture_widget`` runs on the MCP (background) thread. It
    emits the signal (Qt marshals the emission to the GUI thread), then blocks
    on a ``threading.Event`` until the GUI thread has grabbed the widget and
    filled the result dict.
  - The bridge's slot runs ``QWidget.grab()`` (must run on GUI thread), encodes
    the QPixmap to PNG bytes via QBuffer, and signals the event.

Because FastMCP raises if you re-register a tool with the same name on an
existing instance, we build a *fresh* FastMCP here and register ALL tools
ourselves (the screenshot and filesystem functions are imported from the
``qt_mcp`` package and reused as-is). Only ``capture_widget`` differs from the
base server: it routes through the bridge instead of raising "not available".
"""

from __future__ import annotations

import logging
import threading
from io import BytesIO
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from PySide6.QtCore import QObject, Signal

from qt_mcp import filesystem as fs
from qt_mcp import screenshots as shot

logger = logging.getLogger("qt_editor.mcp_server")

# ---------------------------------------------------------------------------
# Capture bridge — the thread-marshaling glue
# ---------------------------------------------------------------------------


class CaptureBridge(QObject):
    """Marshal widget-capture requests from the MCP thread to the Qt GUI thread.

    Lives on the Qt main thread. The MCP ``capture_widget`` tool calls
    :meth:`capture` from the background thread; this emits ``capture_requested``
    (which Qt delivers to the GUI thread), then blocks on an event until the
    slot has produced the PNG bytes.
    """

    capture_requested = Signal(str, object)

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self.capture_requested.connect(self._on_capture)  # queued across threads

    def _on_capture(self, widget_name: str, state: dict):
        """Slot — runs on the GUI thread. Performs the actual QWidget.grab()."""
        try:
            widget = self._main_window.widget_for_name(widget_name)
            if widget is None:
                state["error"] = (
                    f"No widget with objectName {widget_name!r}. "
                    f"Available: {self._main_window.capturable_names()}"
                )
                state["event"].set()
                return

            # grab() returns a QPixmap; must happen on the GUI thread.
            pixmap = widget.grab()
            # Encode to PNG via Qt's own QBuffer (idiomatic, no byte-order juggling).
            from PySide6.QtCore import QBuffer, QByteArray

            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QBuffer.ReadWrite)
            ok = pixmap.save(buf, "PNG")
            buf.close()
            if not ok:
                raise RuntimeError("QPixmap.save('PNG') returned False")
            state["png"] = bytes(ba)
            state["widget"] = {
                "object_name": widget_name,
                "class": type(widget).__name__,
                "width": pixmap.width(),
                "height": pixmap.height(),
            }
        except Exception as exc:  # pragma: no cover - GUI-thread exception
            logger.exception("capture_widget failed on GUI thread")
            state["error"] = f"GUI-thread capture failed: {exc}"
        finally:
            state["event"].set()

    def capture(self, widget_name: str, timeout: float = 10.0) -> dict:
        """Called from the MCP background thread. Blocks until the GUI thread
        fills ``state``. Returns ``{"png": bytes, "widget": {...}}`` or raises.
        """
        event = threading.Event()
        state: dict = {"event": event, "png": None, "widget": None, "error": None}
        # Emitting a Qt signal from another thread is safe: Qt queues the call
        # onto the receiving thread's event loop.
        self.capture_requested.emit(widget_name, state)
        if not event.wait(timeout=timeout):
            raise shot.CaptureError(
                f"Timed out after {timeout}s waiting for the GUI thread to "
                f"capture widget {widget_name!r}."
            )
        if state["error"] is not None:
            raise shot.CaptureError(state["error"])
        if state["png"] is None:
            raise shot.CaptureError("GUI thread returned no PNG (unknown error).")
        return {"png": state["png"], "widget": state["widget"]}


# ---------------------------------------------------------------------------
# Build the in-process MCP server
# ---------------------------------------------------------------------------


def build_server(bridge: CaptureBridge) -> FastMCP:
    """Construct a fresh FastMCP instance with the qt-mcp tools + a working
    ``capture_widget`` routed through the bridge.
    """
    mcp = FastMCP("qt-mcp-editor")

    # --- Screenshot tools (reuse the package functions) ---

    @mcp.tool()
    def list_windows() -> dict:
        """List visible top-level windows with id, title, and geometry."""
        try:
            windows = shot.list_windows()
        except shot.CaptureError as exc:
            raise ToolError(str(exc)) from exc
        return {"windows": [w.to_dict() for w in windows], "count": len(windows)}

    @mcp.tool()
    def capture_window(title: str, exact: bool = False) -> Image:
        """Capture an entire window identified by (partial) title → PNG image."""
        try:
            result = shot.capture_window(title, exact=exact)
        except shot.CaptureError as exc:
            raise ToolError(str(exc)) from exc
        return Image(data=result["png"], format="png")

    @mcp.tool()
    def capture_region(
        x: int,
        y: int,
        width: int,
        height: int,
        title: Optional[str] = None,
    ) -> Image:
        """Capture a screen rectangle (absolute or relative to a window)."""
        try:
            result = shot.capture_region(x, y, width, height, title=title)
        except shot.CaptureError as exc:
            raise ToolError(str(exc)) from exc
        return Image(data=result["png"], format="png")

    @mcp.tool()
    def capture_widget(
        window_title: str,
        widget_name: str,
        timeout: float = 10.0,
    ) -> Image:
        """Capture a specific Qt widget by its objectName → PNG image.

        This is the in-process implementation: it marshals to the Qt GUI thread
        and uses ``QWidget.grab()``, so it captures the widget exactly even if
        it is partially off-screen or occluded by another window.

        Args:
            window_title: Title (or substring) of the editor window. Used to
                confirm the target; the widget is resolved within this app's
                main window.
            widget_name: The widget's ``objectName``. Call list_capturable_widgets
                to discover valid names.
            timeout: Seconds to wait for the GUI thread (default 10).

        Returns the widget as a PNG image.
        """
        # Locate the window to confirm the caller means this app (best-effort).
        try:
            shot.find_window(window_title)
        except shot.CaptureError as exc:
            raise ToolError(
                f"Could not find window {window_title!r}: {exc}. "
                f"Make sure the example Qt editor is running and visible."
            ) from exc
        try:
            result = bridge.capture(widget_name, timeout=timeout)
        except shot.CaptureError as exc:
            raise ToolError(str(exc)) from exc
        logger.info(
            "Captured widget %r (%s %dx%d)",
            widget_name,
            result["widget"]["class"],
            result["widget"]["width"],
            result["widget"]["height"],
        )
        return Image(data=result["png"], format="png")

    @mcp.tool()
    def list_capturable_widgets() -> dict:
        """List the objectNames of widgets in this app that capture_widget can target."""
        names = bridge._main_window.capturable_names()
        return {"widgets": names, "count": len(names)}

    # --- Filesystem tools (reuse the package functions verbatim) ---

    @mcp.tool()
    def read_file(path: str, offset: int = 0, limit: int = 2000) -> dict:
        """Read a text file (paginated)."""
        try:
            return fs.read_file(path, offset=offset, limit=limit)
        except fs.FilesystemError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def write_file(
        path: str, content: str, append: bool = False, create_parents: bool = True
    ) -> dict:
        """Write or append text to a file."""
        try:
            return fs.write_file(path, content, append=append, create_parents=create_parents)
        except fs.FilesystemError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def list_directory(path: str = ".", include_hidden: bool = False) -> dict:
        """List directory entries."""
        try:
            return fs.list_directory(path, include_hidden=include_hidden)
        except fs.FilesystemError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def create_directory(path: str, parents: bool = True) -> dict:
        """Create a directory."""
        try:
            return fs.create_directory(path, parents=parents)
        except fs.FilesystemError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def move(path: str, destination: str, overwrite: bool = False) -> dict:
        """Move or rename a file/directory."""
        try:
            return fs.move(path, destination, overwrite=overwrite)
        except fs.FilesystemError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def delete(path: str, recursive: bool = False) -> dict:
        """Delete a file or directory tree."""
        try:
            return fs.delete(path, recursive=recursive)
        except fs.FilesystemError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def file_info(path: str) -> dict:
        """Return metadata for a file/directory/symlink."""
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
        """Find files matching a glob pattern (recursive)."""
        try:
            return fs.search_glob(path, pattern, include_hidden=include_hidden, max_results=max_results)
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
        """Search file contents for a regex pattern (recursive)."""
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

    return mcp