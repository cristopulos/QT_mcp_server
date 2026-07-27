"""Server-side agent proxy for the Qt MCP standalone server.

Manages a Unix domain socket (Linux v1) that accepts one Qt app connection.
Proxies ``capture_widget`` and ``list_capturable_widgets`` requests over the
socket using newline-delimited JSON frames.

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
from typing import Any

from . import protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentError(Exception):
    """Raised when a proxy operation fails (no app attached, timeout, etc.)."""


# ---------------------------------------------------------------------------
# AgentProxy
# ---------------------------------------------------------------------------


class AgentProxy:
    """Asyncio Unix socket server that proxies widget-capture requests to a
    single attached Qt application.

    Thread-safety: all public async methods must be called from the same asyncio
    event loop that runs the server.
    """

    def __init__(self, socket_path: str | None = None) -> None:
        self._socket_path = socket_path or protocol.default_socket_path()
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

    @property
    def socket_path(self) -> str:
        return self._socket_path

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Unix socket server and begin listening.

        If the socket file already exists (from a prior crash), remove it and
        retry once.
        """
        if sys.platform != "linux":
            raise NotImplementedError(
                f"AgentProxy is Linux-only in v1 (got {sys.platform!r}). "
                "Windows named-pipe support is planned (TODO)."
            )

        # Remove stale socket file.
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

        logger.info("AgentProxy listening on %s", self._socket_path)

    async def stop(self) -> None:
        """Stop the server and disconnect the attached app."""
        self._closed = True
        self._attached_event.clear()

        # Cancel all pending futures.
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

        # Clean up socket file.
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
        """Request a widget capture from the attached Qt app.

        Returns the result dict with ``png_b64``, ``width``, ``height``, ``format``.

        Raises :class:`AgentError` if no app is attached or the request times out.
        """
        if not self.is_attached:
            raise AgentError(
                "No Qt app is attached. Start a Qt app that calls "
                "qt_mcp.agent.start_agent(window)."
            )
        return await self._send_request(
            protocol.METHOD_CAPTURE_WIDGET,
            {"widget_name": widget_name},
            timeout=timeout,
        )

    async def list_capturable_widgets(self, timeout: float = 10.0) -> list[str]:
        """Request the list of capturable widget names from the attached Qt app.

        Raises :class:`AgentError` if no app is attached or the request times out.
        """
        if not self.is_attached:
            raise AgentError(
                "No Qt app is attached. Start a Qt app that calls "
                "qt_mcp.agent.start_agent(window)."
            )
        result = await self._send_request(
            protocol.METHOD_LIST_WIDGETS,
            {},
            timeout=timeout,
        )
        return result["widgets"]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 10.0,
    ) -> Any:
        """Send a request and wait for the matching response."""
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
        """Handle a new connection from a Qt app.

        Enforces single-attached-app: if already connected, reject and close.
        """
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

        # Accept the connection.
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

            # Fail all pending futures.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AgentError("Qt app disconnected"))
            self._pending.clear()

            try:
                writer.close()
            except Exception:
                pass
