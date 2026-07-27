"""Entry point for the example Qt editor that hosts the MCP server.

Concurrency model:
  - The **main thread** runs ``QApplication.exec()`` (the Qt event loop). This is
    a Qt hard requirement — QApplication must live on the main thread.
  - A **background thread** runs the MCP stdio server
    (``FastMCP.run(transport="stdio")``), which owns its own asyncio loop.
  - ``capture_widget`` runs on the MCP thread, emits a Qt signal (thread-safe),
    and blocks on a ``threading.Event`` until the Qt GUI thread grabs the widget
    via ``QWidget.grab()`` and fills the result.

Run directly (an AI client launches this as the MCP server process):

    python -m qt_editor.main

The process both shows the editor window AND speaks MCP over stdio. The AI
client's stdin/stdout are the MCP transport; Qt diagnostics go to stderr only.

Run standalone (no MCP, just the window) for a quick visual check:

    QT_EDITOR_NO_MCP=1 python -m qt_editor.main
"""

from __future__ import annotations

import logging
import os
import sys
import threading

# Ensure the project src/ is importable when running from the repo root.
_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication  # noqa: E402

from qt_editor.editor_window import EditorWindow, WINDOW_TITLE  # noqa: E402
from qt_editor.mcp_server import CaptureBridge, build_server  # noqa: E402

# stdout is reserved for the MCP JSON-RPC stream; all logs go to stderr.
logging.basicConfig(
    level=os.environ.get("QT_EDITOR_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("qt_editor.main")


def _run_mcp_in_thread(bridge: CaptureBridge) -> threading.Thread:
    """Build the MCP server and run it (stdio) in a background daemon thread.

    The thread is a daemon so it dies with the process when the Qt window closes.
    """
    mcp = build_server(bridge)

    def _target():
        try:
            logger.info("MCP stdio server starting (background thread)")
            mcp.run(transport="stdio")
        except Exception:
            logger.exception("MCP server crashed")
            # Ask the Qt app to quit so the process exits cleanly.
            try:
                app = QApplication.instance()
                if app is not None:
                    app.quit()
            except Exception:
                pass

    t = threading.Thread(target=_target, name="mcp-stdio", daemon=True)
    t.start()
    return t


def main() -> int:
    no_mcp = os.environ.get("QT_EDITOR_NO_MCP", "") not in ("", "0", "false", "False")

    # Qt attributes for a clean X11 session.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen" if no_mcp and "DISPLAY" not in os.environ else "xcb")

    app = QApplication(sys.argv)
    app.setApplicationName("qt-editor-mcp-demo")

    window = EditorWindow()
    window.show()
    logger.info("Editor window shown: %r", WINDOW_TITLE)

    bridge = CaptureBridge(window)

    if not no_mcp:
        _run_mcp_in_thread(bridge)
        logger.info("MCP server launched; connect an AI client to this process's stdio.")
    else:
        logger.info("QT_EDITOR_NO_MCP set — running window only, no MCP server.")

    code = app.exec()
    logger.info("Qt event loop exited (code=%s)", code)
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())