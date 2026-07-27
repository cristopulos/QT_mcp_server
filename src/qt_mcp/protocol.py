"""Shared request/response protocol for the Qt MCP agent proxy.

Newline-delimited JSON over a Unix domain socket (Linux v1) or named pipe
(Windows, TODO).

Protocol:
  Request:  {"id": <int>, "method": "capture_widget"|"list_capturable_widgets", "params": {...}}
  Response: {"id": <int>, "ok": true, "result": {...}}
            {"id": <int>, "ok": false, "error": "<msg>"}

For ``capture_widget``, ``result`` = ``{"png_b64": "<base64>", "width": <int>, "height": <int>, "format": "PNG"}``.
For ``list_capturable_widgets``, ``result`` = ``{"widgets": ["name1", ...]}``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

METHOD_CAPTURE_WIDGET = "capture_widget"
METHOD_LIST_WIDGETS = "list_capturable_widgets"

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """Raised when a protocol-level error occurs (malformed frame, etc.)."""


# ---------------------------------------------------------------------------
# Socket path
# ---------------------------------------------------------------------------


def default_socket_path() -> str:
    """Return the default Unix domain socket path for the current user.

    Linux: ``/tmp/qt-mcp-<uid>.sock``
    Non-Linux: raises :class:`NotImplementedError` (Windows named-pipe TODO).
    """
    if sys.platform == "linux":
        return os.path.join(tempfile.gettempdir(), f"qt-mcp-{os.getuid()}.sock")
    # TODO: Windows named-pipe support.
    raise NotImplementedError(
        f"Unix domain sockets are not supported on {sys.platform!r}. "
        "Windows named-pipe support is planned (TODO)."
    )


# ---------------------------------------------------------------------------
# Encode / decode helpers (pure stdlib, no asyncio dependency at import time)
# ---------------------------------------------------------------------------


def encode_request(req_id: int, method: str, params: dict[str, Any] | None = None) -> bytes:
    """Encode a JSON-RPC-like request as a newline-terminated byte string."""
    body = {"id": req_id, "method": method, "params": params or {}}
    return json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\n"


def decode_request(data: bytes) -> dict[str, Any]:
    """Decode a request frame.  Returns ``{"id": ..., "method": ..., "params": ...}``."""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON in request: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"Request must be a JSON object, got {type(obj).__name__}")
    if "id" not in obj or "method" not in obj:
        raise ProtocolError("Request missing 'id' or 'method' field")
    return obj


def encode_response(req_id: int, ok: bool, result: Any = None, error: str | None = None) -> bytes:
    """Encode a response as a newline-terminated byte string."""
    body: dict[str, Any] = {"id": req_id, "ok": ok}
    if ok:
        body["result"] = result
    else:
        body["error"] = error or "Unknown error"
    return json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\n"


def decode_response(data: bytes) -> dict[str, Any]:
    """Decode a response frame.  Returns ``{"id": ..., "ok": ..., "result"|"error": ...}``."""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"Response must be a JSON object, got {type(obj).__name__}")
    if "id" not in obj or "ok" not in obj:
        raise ProtocolError("Response missing 'id' or 'ok' field")
    return obj


# ---------------------------------------------------------------------------
# Async frame helpers (require asyncio)
# ---------------------------------------------------------------------------


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one newline-delimited JSON frame from an asyncio stream reader.

    Raises :class:`ProtocolError` on malformed data.
    """
    try:
        data = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("Connection closed while reading frame") from exc
    except asyncio.LimitOverrunError as exc:
        raise ProtocolError(f"Frame exceeds buffer limit: {exc}") from exc
    return decode_response(data)


async def write_frame(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    """Write a dict as a newline-terminated JSON frame to an asyncio stream writer."""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
    writer.write(data)
    await writer.drain()


# ---------------------------------------------------------------------------
# Sync frame helpers (for the Qt-side agent which uses QLocalSocket)
# ---------------------------------------------------------------------------


def read_frame_sync(buffer: bytes) -> tuple[dict[str, Any] | None, bytes]:
    """Try to extract one complete frame from a byte buffer.

    Returns ``(frame_dict, remaining_buffer)`` or ``(None, buffer)`` if no
    complete frame is available yet.
    """
    idx = buffer.find(b"\n")
    if idx == -1:
        return None, buffer
    frame_data = buffer[:idx]
    remaining = buffer[idx + 1 :]
    return decode_request(frame_data), remaining


def write_frame_sync(obj: dict[str, Any]) -> bytes:
    """Encode a dict as a newline-terminated JSON frame (returns bytes)."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
