# Development

This guide covers local setup, manual protocol checks, tool additions, platform testing, and release versioning.

## Repository layout

```text
.
├── docs/
│   ├── ARCHITECTURE.md       # system design and concurrency
│   ├── DEVELOPMENT.md        # contributor workflow
│   ├── INTEGRATION.md        # embedding guide
│   └── TOOLS.md              # authoritative tool contracts
├── examples/
│   ├── qt_editor/
│   │   ├── __init__.py
│   │   ├── editor_window.py  # named PySide6 widgets
│   │   ├── main.py           # Qt main thread and MCP daemon thread
│   │   └── mcp_server.py     # CaptureBridge and 14-tool FastMCP server
│   └── qt-editor.mcp-config.example.json
├── src/
│   └── qt_mcp/
│       ├── __init__.py       # package version and guarded exports
│       ├── agent.py          # Qt-side QLocalSocket agent and widget capture
│       ├── agent_proxy.py    # server-side asyncio Unix socket proxy
│       ├── filesystem.py     # filesystem implementation
│       ├── protocol.py       # newline-delimited JSON frames and socket path
│       ├── screenshots.py    # platform discovery and PNG capture
│       └── server.py         # standalone 15-tool FastMCP server
├── tests/
├── .gitignore
├── mcp-config.example.json
├── pyproject.toml
└── README.md
```

## Set up a development environment

### Linux or macOS shell

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

### Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Run the test suite with:

```bash
.venv/bin/python -m pytest
```

## Run the standalone server

```bash
.venv/bin/python -m qt_mcp.server
```

or:

```bash
.venv/bin/qt-mcp
```

The process waits for MCP JSON-RPC on stdin. Logs are written to stderr. Set `QT_MCP_LOG_LEVEL` to a Python logging level when more detail is needed:

```bash
QT_MCP_LOG_LEVEL=DEBUG .venv/bin/python -m qt_mcp.server
```

Running the command in a terminal confirms imports and startup, but an MCP client or protocol driver is needed to invoke tools.

## Run the example Qt application

PySide6 is optional and must be installed separately:

```bash
.venv/bin/python -m pip install PySide6
```

The `examples` directory must be importable. From the repository root, set `PYTHONPATH` and launch the module:

```bash
PYTHONPATH=examples .venv/bin/python -m qt_editor.main
```

The normal command starts a visible editor and its unchanged 14-tool embedded MCP server. Since stdio belongs to MCP, an MCP client should normally launch it using [`examples/qt-editor.mcp-config.example.json`](../examples/qt-editor.mcp-config.example.json).

For a window-only visual check, disable MCP:

```bash
QT_EDITOR_NO_MCP=1 PYTHONPATH=examples .venv/bin/python -m qt_editor.main
```

On Windows PowerShell:

```powershell
$env:PYTHONPATH = "examples"
$env:QT_EDITOR_NO_MCP = "1"
.venv\Scripts\python.exe -m qt_editor.main
```

### Run the example as a proxy agent

Agent mode runs the Qt app normally and connects it to a separately launched standalone server. Start the example with either form:

```bash
PYTHONPATH=examples .venv/bin/python -m qt_editor.main --agent
```

```bash
QT_EDITOR_AGENT=1 PYTHONPATH=examples .venv/bin/python -m qt_editor.main
```

In another process, launch or drive the standalone server through an MCP client or the stdio driver below. The first call to `attach_status`, `list_capturable_widgets`, or `capture_widget` lazily starts the proxy listener at `/tmp/qt-mcp-<uid>.sock`. Poll `attach_status` until it reports an attachment:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "attach_status",
    "arguments": {}
  }
}
```

Expected state transitions:

```json
{"attached": false, "socket_path": "/tmp/qt-mcp-1000.sock"}
```

```json
{"attached": true, "socket_path": "/tmp/qt-mcp-1000.sock"}
```

Then call `list_capturable_widgets` and `capture_widget`:

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "capture_widget",
    "arguments": {"widget_name": "editor_area"}
  }
}
```

The standalone proxy is Linux-only in v1 and accepts one attached app per socket. Windows named-pipe support is not implemented. The agent's `QLocalSocket` and `readyRead` handler must remain on the Qt main thread so `QWidget.grab()` is safe.

## Manually test MCP over stdio

MCP uses JSON-RPC messages delimited by newlines on stdio. The following small driver starts the standalone server, performs initialization, lists tools, and prints the response. It keeps stdout and stderr separate so server diagnostics cannot be mistaken for protocol data.

```python
import json
import subprocess
import sys


def send(process: subprocess.Popen, message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def receive(process: subprocess.Popen) -> dict:
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("Server closed stdout before replying")
    return json.loads(line)


process = subprocess.Popen(
    [sys.executable, "-m", "qt_mcp.server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

try:
    send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke-test", "version": "0.1"},
            },
        },
    )
    print(json.dumps(receive(process), indent=2))

    send(
        process,
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )
    send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        },
    )
    print(json.dumps(receive(process), indent=2))
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
```

Run it with the development environment's Python. Protocol-version support is negotiated by the installed MCP SDK; if the SDK rejects the sample version, use a version advertised by that SDK or use its official client session API.

A tool call follows the same pattern:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "list_directory",
    "arguments": {
      "path": ".",
      "include_hidden": false
    }
  }
}
```

Do not send ordinary text to the subprocess stdin and do not merge stderr into stdout.

## Add a new tool

### 1. Put behavior in the appropriate module

Keep reusable behavior outside `server.py`:

- Screenshot and window behavior belongs in `screenshots.py` and raises `CaptureError`.
- Filesystem behavior belongs in `filesystem.py` and raises `FilesystemError`.
- Application-specific GUI behavior belongs behind the in-process bridge or in `agent.py`.
- Proxy lifecycle and request correlation belong in `agent_proxy.py`; shared wire-format changes belong in `protocol.py`.

### 2. Add a typed FastMCP wrapper

FastMCP infers the input schema from type hints and defaults. Keep every argument typed, and make optionality and defaults explicit:

```python
@mcp.tool()
def example_metadata(path: str, include_details: bool = False) -> dict:
    """Return example metadata for a path."""
    try:
        return fs.example_metadata(path, include_details=include_details)
    except fs.FilesystemError as exc:
        raise ToolError(str(exc)) from exc
```

Return plain dictionaries for JSON metadata. For a screenshot, return an MCP `Image`:

```python
@mcp.tool()
def example_capture(title: str) -> Image:
    """Capture an example target as PNG."""
    try:
        png = shot.example_capture(title)
    except shot.CaptureError as exc:
        raise ToolError(str(exc)) from exc
    return Image(data=png, format="png")
```

Use `ToolError` for expected caller-facing failures. Preserve the original exception with `raise ... from exc` for diagnostic context.

### 3. Register it in each applicable server

The standalone server and in-process example use separate `FastMCP` instances. Add a shared tool to both only if both modes should expose it; proxy-only tools remain in standalone `server.py` and may require matching protocol and agent handlers. Do not register a duplicate name on an existing instance; FastMCP raises on duplicate tool registration.

### 4. Update contracts and tests

- Add the exact signature, parameter semantics, return shape, and examples to [TOOLS.md](TOOLS.md).
- Update the README tool count if it changes.
- Add or update unit tests for domain behavior and protocol/schema tests for registration.
- Verify errors are returned as MCP tool errors rather than uncaught protocol failures.

## Platform testing

### Linux/X11

- `DISPLAY` must refer to an accessible X11 display.
- Install `wmctrl` for the preferred enumeration path or `xwininfo` from `x11-utils` for fallback enumeration.
- Test title ambiguity, negative monitor coordinates, multi-monitor geometry, and captures extending near screen edges.
- ImageMagick `import` is only a fallback for screen grabbing; it does not replace the window-enumeration requirement.
- Wayland is not supported by the OS-level path. An XWayland environment may behave differently by compositor.

Useful setup on Debian or Ubuntu:

```bash
sudo apt install wmctrl x11-utils
```

### Windows

- Window discovery uses stdlib `ctypes`; do not add `pywin32` unless the architecture changes and there is a demonstrated need.
- Test display scaling, multiple monitors, negative virtual-screen coordinates, minimized windows, and non-ASCII titles.
- No macOS compatibility should be inferred from the Windows implementation. The Windows path has not been tested on macOS, and `sys.platform` dispatch will not select it there.

### Proxy/agent capture

- Proxy mode is Linux-only in v1; the default socket is `/tmp/qt-mcp-<uid>.sock`.
- Verify that `attach_status` changes from false to true when the agent connects and back to false after disconnection.
- Confirm a second app is rejected while one app is attached to the socket.
- Start the app before and after the proxy listener to exercise the `QTimer` reconnect path.
- Confirm `list_capturable_widgets` includes all descendant widgets with non-empty `objectName()` values, including named Qt internals.
- Block the GUI thread and verify the 10-second proxy operation timeout becomes a controlled tool error.
- Import `qt_mcp` in an environment without importing PySide6; explicit agent use is the point where PySide6 is needed.

### In-process Qt capture

Test the example separately from OS-level capture:

- Capture an unobscured widget.
- Cover the application with another window and confirm `capture_widget` still returns the widget rendering.
- Request an unknown name and verify `isError: true` includes the advertised names.
- Force a short timeout while the GUI thread is blocked and verify a controlled tool error.
- Confirm no logging or `print()` output reaches stdout.

## Release and versioning

This repository currently keeps the version in two places:

1. `pyproject.toml`, under `[project].version`.
2. `src/qt_mcp/__init__.py`, as `__version__`.

Before release:

1. Update both values to the same version.
2. Review user-facing documentation and tool counts.
3. Run the full test suite on supported platforms.
4. Build the package and inspect its metadata and contents.
5. Smoke-test the installed `qt-mcp` console entry point in a clean virtual environment.

`src/qt_mcp/server.py` also currently defines a server `__version__`; keep it synchronized while that definition remains in the codebase.
