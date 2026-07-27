# Architecture

`qt-mcp` exposes desktop screenshots and filesystem operations through the Model Context Protocol. It supports an external standalone process and an embedded Qt application process while keeping the same stdio-facing tool model.

## High-level topology

```text
                                  one of two server process forms

AI client  <--- stdio JSON-RPC --->  standalone qt_mcp.server
                                      ├── OS window enumeration
                                      ├── screen capture
                                      └── filesystem operations

AI client  <--- stdio JSON-RPC --->  in-process Qt application
                                      ├── background FastMCP server
                                      ├── main-thread QApplication
                                      ├── QWidget capture bridge
                                      ├── OS window/screen capture
                                      └── filesystem operations
```

The MCP client starts the configured command and owns its stdin and stdout. Requests and responses flow as JSON-RPC over those streams. Screenshots are returned as MCP image content containing base64-encoded PNG data.

## Standalone modules

```text
src/qt_mcp/
├── __init__.py
├── server.py
├── screenshots.py
└── filesystem.py
```

| Module | Responsibility |
|---|---|
| `__init__.py` | Package metadata, including the package version. |
| `server.py` | Creates the standalone `FastMCP` instance, registers 13 tool wrappers, converts domain errors to `ToolError`, and starts stdio transport. |
| `screenshots.py` | Dispatches window discovery by platform, matches titles, captures pixel rectangles, encodes PNG data, and defines `CaptureError`. Its standalone widget function is intentionally a stub. |
| `filesystem.py` | Resolves paths and implements text reads/writes, directory operations, metadata, glob search, regex content search, and `FilesystemError`. |

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

The standalone process cannot call `QWidget.grab()` because it has no access to the target process's QObject tree. Its `capture_widget` is a stub that returns guidance to use `capture_region`. The embedded app owns its widgets and can provide the real implementation.

## Dependencies

| Dependency | Scope | Purpose |
|---|---|---|
| `mcp>=1.28.1,<2` | Runtime | FastMCP server, stdio transport, `Image`, and `ToolError`. |
| `mss>=9.0.0` | Runtime | Cross-platform physical-pixel screen grabs. |
| `pillow>=10.0.0` | Runtime | Converts `mss` pixel data and encodes PNG images. |
| PySide6 | Example/integration only | Qt application, signals, widgets, pixmaps, and in-memory PNG encoding. |

PySide6 is intentionally not a core dependency because the standalone server can inspect Qt or non-Qt desktop windows without importing Qt.

The Windows implementation needs neither `pywin32` nor `pygetwindow`. The required APIs are available through Python's standard-library `ctypes`, keeping the runtime dependency set smaller. Linux window discovery remains external-command based and requires `wmctrl` or `xwininfo`.

## Tool registration

The standalone server creates a module-level `FastMCP("qt-mcp")` and decorates each wrapper with `@mcp.tool()`. FastMCP derives input schemas from Python type hints and defaults.

The embedded example creates a fresh instance in `build_server(bridge)`. It cannot mutate the standalone instance by registering another `capture_widget`, because FastMCP rejects duplicate tool names. Instead, the example registers all desired wrappers on its own instance and routes widget capture through its bridge.

## Error handling

Domain modules raise explicit exceptions:

- `CaptureError` for window discovery, title matching, invalid capture geometry, screen-grab, and widget-capture failures.
- `FilesystemError` for invalid paths, missing entries, invalid regular expressions, unsafe directory deletion requests, and I/O failures.

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
