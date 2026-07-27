# Architecture

`qt-mcp` exposes desktop screenshots and filesystem operations through the Model Context Protocol. It supports OS-level standalone capture, standalone proxy/agent capture, and an embedded Qt application server while keeping a stdio-facing MCP tool model.

## High-level topology

```text
                              supported process topologies

OS-level standalone:
AI client <--- stdio JSON-RPC ---> qt_mcp.server
                                    ├── OS window enumeration and screen capture
                                    └── filesystem operations

Proxy/agent:
AI client <--- stdio JSON-RPC ---> qt_mcp.server
                                    ├── filesystem and OS capture tools
                                    └── platform AgentProxy backend
                                                   |
                                      NDJSON local transport:
                                      Unix socket (Linux/macOS)
                                      QLocalServer pipe (Windows)
                                                   |
                                    Qt app Agent (QLocalSocket)
                                    └── main-thread QWidget.grab()

In-process:
AI client <--- stdio JSON-RPC ---> Qt application process
                                    ├── background FastMCP server
                                    ├── main-thread QApplication
                                    └── CaptureBridge + QWidget.grab()
```

The MCP client starts the configured command and owns its stdin and stdout. Requests and responses flow as JSON-RPC over those streams. Screenshots are returned as MCP image content containing base64-encoded PNG data.

## Standalone modules

```text
src/qt_mcp/
├── __init__.py
├── agent.py
├── agent_proxy.py
├── filesystem.py
├── protocol.py
├── screenshots.py
└── server.py
```

| Module | Responsibility |
|---|---|
| `__init__.py` | Package version and exports. It exports `AgentProxy` and `AgentError` without Qt and guards agent exports so importing `qt_mcp` does not hard-require PySide6. |
| `server.py` | Creates the standalone `FastMCP` instance, registers 15 tool wrappers, converts domain errors to `ToolError`, starts stdio transport, and lazily owns the proxy singleton and background loop. |
| `screenshots.py` | Dispatches OS window discovery, matches titles, captures pixel rectangles, encodes PNG data, and defines `CaptureError`. |
| `filesystem.py` | Resolves paths and implements text reads/writes, directory operations, metadata, glob search, regex content search, and `FilesystemError`. |
| `protocol.py` | Defines the newline-delimited JSON request/response format, platform-specific default socket or pipe name, `ProtocolError`, and synchronous/asynchronous frame helpers. |
| `agent_proxy.py` | Implements the cross-platform server-side `AgentProxy`: `_AsyncioBackend` uses asyncio Unix sockets on Linux/macOS, while `_QtLocalServerBackend` lazily imports PySide6 and runs `QLocalServer` in a daemon-thread `QEventLoop` on Windows. Both enforce one app, correlate requests, track attachment state, and apply timeouts. |
| `agent.py` | Implements the Qt-side `Agent` with lazy PySide6 imports, `QLocalSocket` request handling, GUI-thread widget capture, and `QTimer` reconnect behavior. |

The server layer is deliberately thin. Platform and filesystem behavior lives in ordinary Python functions, making those functions reusable by the embedded example without importing the standalone `FastMCP` instance.

## Cross-platform capture pipeline

```text
Tool call
   |
   v
server.py wrapper
   |
   v
screenshots.py
   |
   +--> sys.platform == "win32"
   |      `--> Win32 EnumWindows/GetWindowRect via stdlib ctypes
   |
   `--> Linux/X11
          +--> wmctrl -lG
          `--> xwininfo -root -tree plus per-window geometry
   |
   v
WindowInfo or selected rectangle
   |
   v
mss grab in physical screen pixels
   |
   v
Pillow conversion and PNG encoding
   |
   v
FastMCP Image(data=png, format="png")
   |
   v
MCP image content
```

### Window enumeration

`list_windows()` dispatches on `sys.platform`:

- Windows calls `EnumWindows`, filters visible titled windows, and obtains rectangles with `GetWindowRect`. The code requests DPI awareness so coordinates correspond to the physical pixels expected by `mss`.
- Linux prefers `wmctrl -lG`, which supplies IDs, desktops, titles, and geometry in one command. If unavailable, `xwininfo -root -tree` discovers windows and additional `xwininfo -id` calls resolve geometry.

Window title matching is case-insensitive substring matching by default. Ambiguous partial matches fail instead of selecting an arbitrary window. `capture_window` can request an exact, case-sensitive match.

### Pixel capture and encoding

`mss` grabs a rectangle described by top, left, width, and height. Its BGRA data is converted by Pillow to RGB and encoded into an in-memory PNG. On Linux, ImageMagick `import` is an optional fallback if `mss` is unavailable or cannot access the display.

`capture_region` either uses absolute screen coordinates or resolves a window and adds the requested offsets to its top-left coordinate. In both cases, the final operation is an OS-level screen grab; it records what is visible at those pixels.

## Proxy/agent concurrency and protocol

The standalone proxy preserves process separation while moving widget rendering into the Qt process that owns the widgets.

```text
Standalone server                     Qt application main thread
-----------------                     --------------------------
MCP process starts
attach_status/list/capture called
    |
    +-- lazily start daemon asyncio loop
    +-- bind /tmp/qt-mcp-<uid>.sock
    |                                      application launches
    |                                      start_agent(window)
    |<======== QLocalSocket connects ======|
    |   is_attached = true                 |
    |                                      |
MCP client calls capture_widget             |
    |                                      |
    +-- send NDJSON request frame =========>|
    |                                      | readyRead fires
    |                                      | findChild(QWidget, name)
    |                                      | QWidget.grab()
    |                                      | QPixmap -> QBuffer -> PNG
    |                                      | base64 encode
    |<======= NDJSON response frame ========|
    |
    +-- base64 decode
    `-- return FastMCP Image to client
```

`server.py` creates the `AgentProxy` singleton lazily on the first proxy-tool call. Linux and macOS use `_AsyncioBackend` with an asyncio Unix domain socket. Windows uses `_QtLocalServerBackend`, which lazily imports PySide6 and runs `QLocalServer` in a daemon thread with its own `QEventLoop`; `concurrent.futures.Future` and `loop.run_in_executor` keep the public proxy methods awaitable without blocking asyncio. Both backends accept one attached application at a time, correlate request IDs with pending futures, and apply a 10-second operation timeout.

The Qt-side `Agent` creates `QLocalSocket` on the GUI thread. Because `readyRead` is delivered to the socket's owning thread, request dispatch and `QWidget.grab()` both run on the Qt main thread. Capture responses contain base64 PNG data plus width, height, and format fields; the MCP layer converts the PNG back into `Image` content.

If the server is unavailable or disconnects, the agent uses single-shot `QTimer` callbacks for non-blocking reconnect attempts with backoff, resetting the retry state after a successful connection. The default endpoint is `/tmp/qt-mcp-<uid>.sock` on Linux/macOS and `qt-mcp-<username>` on Windows, where Qt resolves the short name to `\\.\pipe\qt-mcp-<username>` internally.

## In-process concurrency model

The example app in `examples/qt_editor/` adds exact widget capture without violating Qt's thread affinity rules.

```text
Main thread                               MCP background thread
-----------                               ---------------------
QApplication
EditorWindow
CaptureBridge QObject                     FastMCP stdio loop
    |                                           |
    |                         capture_widget request
    |                                           |
    |<---- emit capture_requested(name, state)--|
    |      Qt queues delivery to GUI thread     |
    |                                           | wait(Event)
CaptureBridge._on_capture()                     |
    |
    +--> resolve objectName                     |
    +--> QWidget.grab()                         |
    +--> QPixmap.save(QBuffer, "PNG")           |
    +--> state["png"] = bytes                   |
    `--> Event.set() -------------------------->|
                                                |
                                      return FastMCP Image
```

The main thread creates `QApplication`, the window, and `CaptureBridge`, then runs `QApplication.exec()`. A daemon thread creates or runs the FastMCP stdio server and owns its asyncio loop. The bridge's receiver lives on the GUI thread, so Qt's automatic cross-thread signal handling queues `_on_capture` there.

The shared state object contains a `threading.Event`, result bytes, metadata, and an error field. The MCP thread blocks for at most the requested timeout. The GUI slot sets the event in `finally`, ensuring that failures are returned rather than deadlocking the request.

## Why widget capture is different

OS capture copies desktop pixels. It cannot recover a widget hidden by another window, clipped by its parent, minimized with its window, or outside the visible screen. It also requires geometry translation from widget-local to screen coordinates.

`QWidget.grab()` renders the selected Qt widget to a `QPixmap` on the GUI thread. The capture is naturally bounded to that object and remains usable when the widget is occluded or off-screen. A stable `objectName` therefore becomes a precise application-level capture identifier.

The standalone process cannot call `QWidget.grab()` directly because it has no access to another process's QObject tree. In proxy mode, the attached agent performs the call inside the target Qt process and returns PNG data over the socket. In-process mode performs the same GUI-thread operation through `CaptureBridge` without a socket.

## Dependencies

| Dependency | Scope | Purpose |
|---|---|---|
| `mcp>=1.28.1,<2` | Runtime | FastMCP server, stdio transport, `Image`, and `ToolError`. |
| `mss>=9.0.0` | Runtime | Cross-platform physical-pixel screen grabs. |
| `pillow>=10.0.0` | Runtime | Converts `mss` pixel data and encodes PNG images. |
| PySide6 | Example/integration; Windows proxy server | Qt application, signals, widgets, pixmaps, in-memory PNG encoding, and the Windows `QLocalServer` backend. |

PySide6 is intentionally not a core dependency because filesystem and OS-level capture remain Qt-free on every platform, and Linux/macOS use the Qt-free proxy backend. Importing `qt_mcp` does not import PySide6. The Qt app imports `qt_mcp.agent`, while the standalone server imports PySide6 lazily only when proxy tools start the Windows backend.

The Windows implementation needs neither `pywin32` nor `pygetwindow`. The required APIs are available through Python's standard-library `ctypes`, keeping the runtime dependency set smaller. Linux window discovery remains external-command based and requires `wmctrl` or `xwininfo`.

## Tool registration

The standalone server creates a module-level `FastMCP("qt-mcp")`, registers 15 tools, and decorates each wrapper with `@mcp.tool()`. FastMCP derives input schemas from Python type hints and defaults. Its proxy tools lazily start `AgentProxy`; the remaining tools do not require an attached app.

The embedded example creates a fresh instance in `build_server(bridge)`. It cannot mutate the standalone instance by registering another `capture_widget`, because FastMCP rejects duplicate tool names. Instead, the example registers all desired wrappers on its own instance and routes widget capture through its bridge.

## Error handling

Domain modules raise explicit exceptions:

- `CaptureError` for window discovery, title matching, invalid capture geometry, screen-grab, and widget-capture failures.
- `FilesystemError` for invalid paths, missing entries, invalid regular expressions, unsafe directory deletion requests, and I/O failures.
- `AgentError` for missing attachments, disconnections, proxy startup failures, remote agent errors, and request timeouts.
- `ProtocolError` for malformed newline-delimited JSON frames or incomplete stream reads.

Each MCP tool wrapper catches its domain exception and raises `ToolError` with the same message:

```python
try:
    result = shot.capture_window(title, exact=exact)
except shot.CaptureError as exc:
    raise ToolError(str(exc)) from exc
```

FastMCP serializes `ToolError` as a tool result with `isError: true`, allowing the client to distinguish an expected tool failure from a broken protocol connection. Unexpected exceptions are not intentionally hidden and should be diagnosed through stderr logs.

## Stream discipline

stdio transport reserves stdin and stdout for MCP JSON-RPC. Application and server diagnostics must be sent to stderr. This is especially important for an embedded GUI process, where a normal `print()` to stdout can insert non-protocol bytes into the client connection.

See [INTEGRATION.md](INTEGRATION.md) for an implementation recipe and [TOOLS.md](TOOLS.md) for the protocol-facing contracts.
