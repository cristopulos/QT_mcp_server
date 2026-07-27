"""Server-side agent proxy for the Qt MCP standalone server.

Manages an IPC endpoint (Unix domain socket on Linux/macOS, named pipe on
Windows) that accepts one Qt app connection.  Proxies ``capture_widget`` and
``list_capturable_widgets`` requests over the socket using newline-delimited
JSON frames.

Usage (internal to ``server.py``)::

    proxy = AgentProxy()
    await proxy.start()
    await proxy.wait_for_attach(timeout=30.0)
    result = await proxy.capture_widget("editor_area")
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from typing import Any

from . import protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentError(Exception):
    """Raised when a proxy operation fails (no app attached, timeout, etc.)."""


# ---------------------------------------------------------------------------
# AgentProxy — platform-dispatching public class
# ---------------------------------------------------------------------------


class AgentProxy:
    """IPC server that proxies widget-capture requests to a single attached Qt
    application.

    Platform dispatch:
      - **Linux / macOS**: asyncio Unix domain socket server (no Qt dependency).
      - **Windows**: ``QLocalServer`` (Qt named-pipe) running in a dedicated
        thread with its own ``QEventLoop``.  Requires PySide6 at runtime on
        Windows (lazy import).

    All public methods are ``async`` and identical across platforms.
    """

    def __init__(self, socket_path: str | None = None) -> None:
        self._socket_path = socket_path or protocol.default_socket_path()

        if sys.platform in ("linux", "darwin"):
            self._backend: _AsyncioBackend | _QtLocalServerBackend = _AsyncioBackend(self._socket_path)
        elif sys.platform == "win32":
            self._backend = _QtLocalServerBackend(self._socket_path)
        else:
            raise NotImplementedError(
                f"AgentProxy not implemented for platform {sys.platform!r}. "
                "Supported: linux, darwin, win32."
            )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_attached(self) -> bool:
        return self._backend.is_attached

    @property
    def socket_path(self) -> str:
        return self._socket_path

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the IPC server and begin listening."""
        await self._backend.start()

    async def stop(self) -> None:
        """Stop the server and disconnect the attached app."""
        await self._backend.stop()

    async def wait_for_attach(self, timeout: float | None = None) -> None:
        """Wait for a Qt app to connect.

        Raises :class:`asyncio.TimeoutError` on timeout.
        """
        await self._backend.wait_for_attach(timeout)

    # ------------------------------------------------------------------
    # Proxy methods
    # ------------------------------------------------------------------

    async def capture_widget(self, widget_name: str, timeout: float = 10.0) -> dict[str, Any]:
        """Request a widget capture from the attached Qt app.

        Returns the result dict with ``png_b64``, ``width``, ``height``, ``format``.

        Raises :class:`AgentError` if no app is attached or the request times out.
        """
        if not self.is_attached:
            raise AgentError(
                "No Qt app is attached. Start a Qt app that calls "
                "qt_mcp.agent.start_agent(window)."
            )
        return await self._backend.capture_widget(widget_name, timeout)

    async def list_capturable_widgets(self, timeout: float = 10.0) -> list[str]:
        """Request the list of capturable widget names from the attached Qt app.

        Raises :class:`AgentError` if no app is attached or the request times out.
        """
        if not self.is_attached:
            raise AgentError(
                "No Qt app is attached. Start a Qt app that calls "
                "qt_mcp.agent.start_agent(window)."
            )
        result = await self._backend.list_capturable_widgets(timeout)
        return result["widgets"]


# ---------------------------------------------------------------------------
# Linux / macOS backend — asyncio Unix domain socket
# ---------------------------------------------------------------------------


class _AsyncioBackend:
    """Asyncio Unix domain socket server (Linux / macOS)."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._attached_event = asyncio.Event()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._closed = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_attached(self) -> bool:
        return self._writer is not None and not self._closed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Unix socket server and begin listening.

        If the socket file already exists (from a prior crash), remove it and
        retry once.
        """
        if os.path.exists(self._socket_path):
            logger.warning("Removing stale socket: %s", self._socket_path)
            os.unlink(self._socket_path)

        try:
            self._server = await asyncio.start_unix_server(
                self._on_connect,
                path=self._socket_path,
            )
        except OSError as exc:
            raise AgentError(f"Failed to start Unix socket server at {self._socket_path}: {exc}") from exc

        logger.info("AgentProxy listening on %s (asyncio Unix socket)", self._socket_path)

    async def stop(self) -> None:
        """Stop the server and disconnect the attached app."""
        self._closed = True
        self._attached_event.clear()

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AgentError("Qt app disconnected"))
        self._pending.clear()

        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass

    async def wait_for_attach(self, timeout: float | None = None) -> None:
        """Wait for a Qt app to connect.

        Raises :class:`asyncio.TimeoutError` on timeout.
        """
        async with asyncio.timeout(timeout):
            await self._attached_event.wait()

    # ------------------------------------------------------------------
    # Proxy methods
    # ------------------------------------------------------------------

    async def capture_widget(self, widget_name: str, timeout: float = 10.0) -> dict[str, Any]:
        return await self._send_request(
            protocol.METHOD_CAPTURE_WIDGET,
            {"widget_name": widget_name},
            timeout=timeout,
        )

    async def list_capturable_widgets(self, timeout: float = 10.0) -> dict[str, Any]:
        return await self._send_request(
            protocol.METHOD_LIST_WIDGETS,
            {},
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 10.0,
    ) -> Any:
        req_id = self._next_id
        self._next_id += 1

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        try:
            frame = protocol.encode_request(req_id, method, params)
            assert self._writer is not None
            self._writer.write(frame)
            await self._writer.drain()

            try:
                async with asyncio.timeout(timeout):
                    response = await fut
            except asyncio.TimeoutError:
                raise AgentError(
                    f"{method} timed out after {timeout}s (app may be frozen)"
                )

            if not response.get("ok", False):
                raise AgentError(response.get("error", "Unknown error from app"))
            return response.get("result")
        finally:
            self._pending.pop(req_id, None)

    async def _on_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peername = writer.get_extra_info("peername")
        logger.info("New connection from %s", peername)

        if self.is_attached:
            logger.warning("Rejecting second connection from %s", peername)
            try:
                reject = protocol.encode_response(0, False, error="Already attached to another app")
                writer.write(reject)
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return

        self._reader = reader
        self._writer = writer
        self._attached_event.set()
        logger.info("Qt app attached on %s", self._socket_path)

        try:
            while not self._closed:
                try:
                    data = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    logger.info("Qt app disconnected (incomplete read)")
                    break
                except asyncio.LimitOverrunError:
                    logger.warning("Frame too large, skipping")
                    continue

                try:
                    response = protocol.decode_response(data)
                except protocol.ProtocolError as exc:
                    logger.warning("Malformed frame from app: %s", exc)
                    continue

                req_id: int | None = response.get("id")
                fut = self._pending.get(req_id) if req_id is not None else None
                if fut is not None and not fut.done():
                    fut.set_result(response)
                else:
                    logger.debug("Received response for unknown/finished id %s", req_id)
        except Exception as exc:
            logger.error("Connection handler error: %s", exc)
        finally:
            logger.info("Qt app detached")
            self._reader = None
            self._writer = None
            self._attached_event.clear()

            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AgentError("Qt app disconnected"))
            self._pending.clear()

            try:
                writer.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Windows backend — QLocalServer in a dedicated thread
# ---------------------------------------------------------------------------


class _QtLocalServerBackend:
    """QLocalServer (Qt named-pipe) backend for Windows.

    Runs a ``QEventLoop`` in a dedicated daemon thread.  The MCP tool thread
    blocks on a ``concurrent.futures.Future`` that the Qt event-loop thread
    resolves when a response frame arrives.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._thread: threading.Thread | None = None
        self._loop: Any = None  # QEventLoop
        self._app: Any = None  # QApplication
        self._server: Any = None  # QLocalServer
        self._socket: Any = None  # QLocalSocket (connected client)
        self._attached_event = threading.Event()
        self._pending: dict[int, Any] = {}  # id -> concurrent.futures.Future
        self._next_id = 1
        self._closed = False
        self._lock = threading.Lock()
        self._recv_buffer = b""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_attached(self) -> bool:
        return self._socket is not None and not self._closed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the QLocalServer in a background thread."""
        from concurrent.futures import Future

        self._closed = False
        self._attached_event.clear()

        start_fut: Future = Future()

        def _run() -> None:
            # Lazy import of PySide6 — only on Windows, only when proxy starts.
            from PySide6.QtCore import QEventLoop
            from PySide6.QtNetwork import QLocalServer, QLocalSocket
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                app = QApplication([])
            self._app = app

            loop = QEventLoop()
            self._loop = loop

            server = QLocalServer()
            self._server = server

            # Remove stale pipe before listening.
            QLocalServer.removeServer(self._socket_path)

            def on_new_connection() -> None:
                if not server.hasPendingConnections():
                    return
                sock = server.nextPendingConnection()
                if sock is None:
                    return

                if self.is_attached:
                    logger.warning("Rejecting second connection")
                    reject = protocol.encode_response(0, False, error="Already attached to another app")
                    sock.write(reject)
                    sock.flush()
                    sock.disconnectFromServer()
                    return

                self._socket = sock
                self._recv_buffer = b""
                sock.readyRead.connect(self._on_ready_read)
                sock.disconnected.connect(self._on_disconnected)
                self._attached_event.set()
                logger.info("Qt app attached on %s", self._socket_path)

            server.newConnection.connect(on_new_connection)

            ok = server.listen(self._socket_path)
            if not ok:
                start_fut.set_exception(
                    AgentError(
                        f"QLocalServer failed to listen on {self._socket_path}: "
                        f"{server.errorString()}"
                    )
                )
                return

            logger.info("AgentProxy listening on %s (QLocalServer)", self._socket_path)
            start_fut.set_result(None)

            loop.exec()

            # Cleanup after loop exits.
            if self._socket is not None:
                try:
                    self._socket.disconnectFromServer()
                except Exception:
                    pass
                self._socket = None
            if self._server is not None:
                self._server.close()
                self._server = None
            QLocalServer.removeServer(self._socket_path)

        self._thread = threading.Thread(target=_run, name="qt-proxy", daemon=True)
        self._thread.start()

        try:
            start_fut.result(timeout=10.0)
        except TimeoutError:
            raise AgentError("Timed out starting QLocalServer backend")

    async def stop(self) -> None:
        """Stop the server and disconnect the attached app."""
        self._closed = True
        self._attached_event.clear()

        with self._lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AgentError("Qt app disconnected"))
            self._pending.clear()

        if self._loop is not None:
            from PySide6.QtCore import QMetaObject, Qt

            QMetaObject.invokeMethod(
                self._loop,
                "quit",
                Qt.ConnectionType.QueuedConnection,
            )

        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        self._socket = None
        self._server = None
        self._loop = None
        self._app = None

    async def wait_for_attach(self, timeout: float | None = None) -> None:
        """Wait for a Qt app to connect.

        Raises :class:`asyncio.TimeoutError` on timeout.
        """
        import time

        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self._attached_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                raise asyncio.TimeoutError()
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Proxy methods
    # ------------------------------------------------------------------

    async def capture_widget(self, widget_name: str, timeout: float = 10.0) -> dict[str, Any]:
        return await self._send_request(
            protocol.METHOD_CAPTURE_WIDGET,
            {"widget_name": widget_name},
            timeout=timeout,
        )

    async def list_capturable_widgets(self, timeout: float = 10.0) -> dict[str, Any]:
        return await self._send_request(
            protocol.METHOD_LIST_WIDGETS,
            {},
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 10.0,
    ) -> Any:
        from concurrent.futures import Future

        req_id = self._next_id
        self._next_id += 1

        fut: Future = Future()
        with self._lock:
            self._pending[req_id] = fut

        try:
            frame = protocol.encode_request(req_id, method, params)

            # Write to the Qt socket. QLocalSocket.write() is reentrant but not
            # thread-safe. We protect with the lock and write directly since
            # pipe writes are safe in practice.
            with self._lock:
                if self._socket is not None:
                    self._socket.write(frame)
                    self._socket.flush()

            # Wait for the response with timeout, offloading to a thread.
            loop = asyncio.get_running_loop()
            try:
                response = await loop.run_in_executor(None, lambda: fut.result(timeout=timeout))
            except TimeoutError:
                raise AgentError(
                    f"{method} timed out after {timeout}s (app may be frozen)"
                )

            if not response.get("ok", False):
                raise AgentError(response.get("error", "Unknown error from app"))
            return response.get("result")
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

    def _on_ready_read(self) -> None:
        """Slot connected to ``QLocalSocket.readyRead``.

        Runs on the Qt event loop thread.  Reads available data, accumulates
        in a buffer, extracts complete newline-delimited frames, and resolves
        pending futures.
        """
        sock = self._socket
        if sock is None:
            return

        raw = bytes(sock.readAll())
        self._recv_buffer += raw

        while True:
            idx = self._recv_buffer.find(b"\n")
            if idx == -1:
                break
            frame_data = self._recv_buffer[:idx]
            self._recv_buffer = self._recv_buffer[idx + 1 :]

            if not frame_data.strip():
                continue

            try:
                response = protocol.decode_response(frame_data)
            except protocol.ProtocolError as exc:
                logger.warning("Malformed frame from app: %s", exc)
                continue

            req_id: int | None = response.get("id")
            with self._lock:
                fut = self._pending.get(req_id) if req_id is not None else None
            if fut is not None and not fut.done():
                fut.set_result(response)
            else:
                logger.debug("Received response for unknown/finished id %s", req_id)

    def _on_disconnected(self) -> None:
        """Handle client disconnection."""
        logger.info("Qt app detached")
        self._socket = None
        self._recv_buffer = b""
        self._attached_event.clear()

        with self._lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AgentError("Qt app disconnected"))
            self._pending.clear()
