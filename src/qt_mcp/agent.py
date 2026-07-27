"""Qt-side agent that connects to the standalone ``qt-mcp`` server.

This module is imported by the Qt application (e.g. the example editor) to
register itself with the standalone MCP server via a Unix domain socket.

**Threading invariant**: All socket I/O and ``QWidget.grab()`` calls happen on
the Qt main thread because ``QLocalSocket.readyRead`` fires on the thread that
owns the socket, and we create the socket on the main thread.  This is mandatory
— ``QWidget.grab()`` must be called on the GUI thread.

Usage::

    from qt_mcp.agent import start_agent
    agent = start_agent(window)
    # window.show()
    # app.exec()
"""

from __future__ import annotations

import base64
import logging
import os
import sys

from . import protocol

logger = logging.getLogger(__name__)

# Re-export the canonical default_socket_path from protocol.
default_socket_path = protocol.default_socket_path

# ---------------------------------------------------------------------------
# Reconnect backoff
# ---------------------------------------------------------------------------

_INITIAL_RETRY_DELAY_MS = 500  # 500 ms
_MAX_RETRY_DELAY_MS = 5000  # 5 s
_MAX_RETRIES = 60  # Keep trying for ~2.5 minutes


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Manages the connection from a Qt application to the standalone MCP server.

    Do not construct directly — use :func:`start_agent`.
    """

    def __init__(self, window, socket_path: str) -> None:
        # Lazy import of PySide6 — only when this class is instantiated.
        from PySide6.QtCore import QByteArray, QIODevice, QTimer
        from PySide6.QtGui import QPixmap
        from PySide6.QtNetwork import QLocalSocket
        from PySide6.QtWidgets import QWidget

        self._QByteArray = QByteArray
        self._QIODevice = QIODevice
        self._QPixmap = QPixmap
        self._QWidget = QWidget
        self._QLocalSocket = QLocalSocket
        self._QTimer = QTimer

        self._window = window
        self._socket_path = socket_path
        self._socket: QLocalSocket | None = None
        self._recv_buffer = b""
        self._running = True
        self._retry_count = 0
        self._retry_delay_ms = _INITIAL_RETRY_DELAY_MS
        self._retry_timer: QTimer | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initiate the connection to the standalone MCP server.

        This is called automatically by :func:`start_agent`.
        """
        self._connect()

    def stop(self) -> None:
        """Disconnect from the server cleanly."""
        self._running = False
        if self._retry_timer is not None:
            self._retry_timer.stop()
            self._retry_timer = None
        if self._socket is not None:
            try:
                self._socket.disconnectFromServer()
            except Exception:
                pass
            self._socket = None

    # ------------------------------------------------------------------
    # Internal: connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Create a QLocalSocket and connect to the server."""
        if not self._running:
            return

        socket = self._QLocalSocket()
        socket.setObjectName("qt_mcp_agent_socket")
        socket.readyRead.connect(self._on_ready_read)
        socket.disconnected.connect(self._on_disconnected)
        socket.errorOccurred.connect(self._on_error)

        logger.debug("Connecting to %s", self._socket_path)
        socket.connectToServer(self._socket_path)
        self._socket = socket

    def _on_error(self, socket_error) -> None:
        """Handle socket errors."""
        logger.warning("Socket error: %s", socket_error)
        # Clean up the failed socket.
        if self._socket is not None:
            try:
                self._socket.disconnectFromServer()
            except Exception:
                pass
            self._socket = None
        self._recv_buffer = b""
        # Schedule a reconnect attempt.
        if self._running:
            self._schedule_reconnect()

    def _on_disconnected(self) -> None:
        """Handle disconnection — schedule reconnect with backoff."""
        logger.info("Disconnected from qt-mcp server")
        self._socket = None
        self._recv_buffer = b""
        if self._running:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnect attempt using QTimer (non-blocking)."""
        if not self._running:
            return
        if self._retry_count >= _MAX_RETRIES:
            logger.error("All reconnect attempts exhausted. Giving up.")
            return

        delay = self._retry_delay_ms
        logger.info(
            "Reconnect attempt %d/%d in %dms...",
            self._retry_count + 1,
            _MAX_RETRIES,
            delay,
        )

        timer = self._QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._do_reconnect)
        timer.start(delay)
        self._retry_timer = timer

    def _do_reconnect(self) -> None:
        """Actually attempt to reconnect (called by QTimer)."""
        self._retry_timer = None
        if not self._running:
            return
        self._retry_count += 1
        self._connect()
        # If connectToServer succeeds, the socket will either connect or error
        # asynchronously. We don't check synchronously.
        # On success, _on_ready_read will fire; on failure, _on_error will fire
        # and schedule another retry.
        # Reset backoff on success is handled in _on_ready_read (if we get data)
        # or we can check connected signal.
        # For simplicity, we reset backoff when we successfully connect.
        # We'll use the 'connected' signal for that.
        if self._socket is not None:
            self._socket.connected.connect(self._on_connected)

    def _on_connected(self) -> None:
        """Called when the socket successfully connects to the server."""
        logger.info("Connected to qt-mcp server at %s", self._socket_path)
        self._retry_count = 0
        self._retry_delay_ms = _INITIAL_RETRY_DELAY_MS

    # ------------------------------------------------------------------
    # Internal: message handling
    # ------------------------------------------------------------------

    def _on_ready_read(self) -> None:
        """Slot connected to ``QLocalSocket.readyRead``.

        **Runs on the Qt main thread** — this is critical because
        ``QWidget.grab()`` must be called on the GUI thread.  We read available
        data, accumulate in a buffer, extract complete newline-delimited frames,
        and dispatch each one.
        """
        socket = self._socket
        if socket is None:
            return

        # Read all available bytes.
        raw = bytes(socket.readAll())
        self._recv_buffer += raw

        # Process complete frames.
        while True:
            frame, self._recv_buffer = protocol.read_frame_sync(self._recv_buffer)
            if frame is None:
                break
            try:
                self._dispatch(frame)
            except Exception as exc:
                logger.exception("Error dispatching request: %s", exc)
                # Send error response.
                req_id = frame.get("id", 0)
                self._send_response(req_id, False, error=str(exc))

    def _dispatch(self, request: dict) -> None:
        """Dispatch a request to the appropriate handler."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id", 0)

        if method == protocol.METHOD_CAPTURE_WIDGET:
            self._handle_capture_widget(req_id, params)
        elif method == protocol.METHOD_LIST_WIDGETS:
            self._handle_list_widgets(req_id)
        else:
            self._send_response(req_id, False, error=f"Unknown method: {method}")

    def _handle_capture_widget(self, req_id: int, params: dict) -> None:
        """Handle a ``capture_widget`` request.

        Looks up the widget by ``objectName`` via ``QMainWindow.findChild()``,
        calls ``QWidget.grab()`` on the current thread (the Qt main thread),
        encodes the result as base64 PNG, and sends the response.
        """
        widget_name = params.get("widget_name", "")
        if not widget_name:
            self._send_response(req_id, False, error="Missing 'widget_name' parameter")
            return

        widget = self._window.findChild(self._QWidget, widget_name)
        if widget is None:
            self._send_response(
                req_id,
                False,
                error=f"widget '{widget_name}' not found",
            )
            return

        # QWidget.grab() — MUST be called on the GUI thread.
        # This handler runs on the Qt main thread (readyRead signal), so this is safe.
        pixmap = widget.grab()

        # Encode to PNG via QBuffer.
        ba = self._QByteArray()
        from PySide6.QtCore import QBuffer

        buf = QBuffer(ba)
        buf.open(QBuffer.ReadWrite)
        ok = pixmap.save(buf, "PNG")
        buf.close()

        if not ok:
            self._send_response(req_id, False, error="QPixmap.save('PNG') returned False")
            return

        png_bytes = bytes(ba.data())
        png_b64 = base64.b64encode(png_bytes).decode("ascii")

        result = {
            "png_b64": png_b64,
            "width": pixmap.width(),
            "height": pixmap.height(),
            "format": "PNG",
        }
        self._send_response(req_id, True, result=result)

    def _handle_list_widgets(self, req_id: int) -> None:
        """Handle a ``list_capturable_widgets`` request.

        Walks all ``QWidget`` children of the main window and collects their
        ``objectName`` where non-empty.
        """
        names: list[str] = []
        for child in self._window.findChildren(self._QWidget):
            name = child.objectName()
            if name:
                names.append(name)
        self._send_response(req_id, True, result={"widgets": names})

    def _send_response(self, req_id: int, ok: bool, result=None, error: str | None = None) -> None:
        """Send a response frame over the socket."""
        if self._socket is None:
            return
        frame = protocol.encode_response(req_id, ok, result=result, error=error)
        self._socket.write(frame)
        self._socket.flush()


# ---------------------------------------------------------------------------
# Public convenience function
# ---------------------------------------------------------------------------


def start_agent(window, socket_path: str | None = None) -> Agent:
    """Create and start an :class:`Agent` bound to the given ``QMainWindow``.

    The agent connects to the standalone ``qt-mcp`` server via a Unix domain
    socket and handles ``capture_widget`` / ``list_capturable_widgets`` requests.

    Args:
        window: A ``QMainWindow`` (or any ``QWidget`` with ``findChild``).
        socket_path: Unix domain socket path.  Defaults to
            :func:`default_socket_path`.

    Returns:
        The started :class:`Agent` instance.
    """
    if socket_path is None:
        socket_path = default_socket_path()
    agent = Agent(window, socket_path)
    agent.start()
    return agent
