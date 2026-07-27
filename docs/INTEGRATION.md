# Integrating qt-mcp into a Qt application

There are three ways to expose a Qt application to an MCP client. Use OS-level capture for a zero-change start, proxy/agent mode to keep the application and MCP server as separate processes while gaining precise widget capture, or in-process embedding when the application itself should own the MCP server.

## Strategy comparison

| Strategy | Application changes | Capture targets | Occluded or off-screen widgets | Coordinate model | Recommended use |
|---|---|---|---|---|---|
| OS-level standalone server | None | Visible windows and rectangular screen regions | No; it captures current desktop pixels | Absolute screen coordinates, or offsets relative to a window | Quick inspection and third-party applications |
| Proxy/agent | Call `start_agent(window)` and run the standalone server separately | Windows, regions, and discovered named widgets | Yes, for widget capture through `QWidget.grab()` | Widget `objectName` for proxy capture | Reusable standalone server with a normal Qt app lifecycle |
| In-process embedded server | Add a bridge and start FastMCP from the app | Windows, regions, and explicitly advertised named widgets | Yes, through `QWidget.grab()` | Widget identity through `objectName` | Tight coupling where the Qt app is also the MCP server process |

## Strategy 1: use the standalone server

Run `qt-mcp` as a separate process and let it observe the Qt application from outside:

```bash
.venv/bin/python -m qt_mcp.server
```

The client can call `list_windows`, `capture_window`, and `capture_region`. No source change or Qt dependency is required in the server. This is the fastest way to inspect an existing application, but OS-level capture only sees pixels currently presented by the desktop. Covered, minimized, clipped, or off-screen widgets cannot be captured reliably, and region coordinates ultimately refer to the screen.

Configure the client with the absolute virtual-environment Python path as shown in the [README](../README.md#connect-an-mcp-client).

## Proxy/agent mode (attach to a running Qt app)

Proxy/agent mode separates the MCP server from the GUI process without giving up `QWidget.grab()` precision. The MCP client launches the normal standalone `qt-mcp` server. On the first proxy-tool call, that server lazily starts the platform backend: an asyncio Unix domain socket on Linux/macOS or a Qt `QLocalServer` named pipe on Windows. A Qt application opts in by creating an agent on the GUI thread:

```python
from qt_mcp.agent import start_agent

agent = start_agent(window)
```

The application remains a normal Qt app: it owns no FastMCP server and does not reserve its stdin or stdout for MCP. The standalone server remains reusable; stop one attached app and another agent can attach to the same server. In-process mode is more tightly coupled: the application itself is the MCP stdio server and owns both event loops.

### Complete app-side recipe

Install PySide6 in the application's environment and give capturable child widgets non-empty `objectName` values. The proxy agent discovers all descendant `QWidget` instances with non-empty names, including Qt internals; it does not use a separate allowlist.

```python
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
from qt_mcp.agent import start_agent


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("My Agent-Enabled Qt App")

        preview = QLabel("Build preview")
        preview.setObjectName("build_preview")
        self.setCentralWidget(preview)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    # Keep the returned Agent alive for as long as the window is running.
    agent = start_agent(window)
    window._qt_mcp_agent = agent

    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
```

`start_agent(window, socket_path=None) -> Agent` starts connecting immediately and returns an object whose `stop()` method disconnects cleanly. By default, `qt_mcp.protocol.default_socket_path()` returns `/tmp/qt-mcp-<uid>.sock` on Linux/macOS and `qt-mcp-<username>` on Windows. Override the path on the app side when needed:

```python
agent = start_agent(window, socket_path="/tmp/my-qt-app-mcp.sock")
```

The server-side `AgentProxy` must use the same override. The shipped standalone CLI currently constructs its singleton with the default path; a custom path therefore requires constructing/configuring `AgentProxy(socket_path=...)` in your own server integration. For the stock `qt_mcp.server`, use the default path on both sides.

### Windows notes

Proxy mode on Windows requires PySide6 in the standalone server environment: `pip install PySide6`. The default pipe name is `qt-mcp-<username>`; Qt resolves it to `\\.\pipe\qt-mcp-<username>` internally. The app side remains unchanged because `QLocalSocket` is cross-platform. Single-attached-app enforcement and the 10-second request timeout apply as on Linux and macOS.

### MCP-client configuration

Configure the MCP client to launch the standalone server, not the Qt app:

```json
{
  "mcpServers": {
    "qt-mcp": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "qt_mcp.server"]
    }
  }
}
```

Then start the agent-enabled Qt application as a normal process. The standalone server may start before or after the app: the agent reconnects through a non-blocking `QTimer` backoff if the socket is not ready or the server restarts. Use `attach_status()` to poll until it returns `{"attached": true, ...}`, then call `list_capturable_widgets()` and `capture_widget(widget_name="build_preview")`.

The standalone server exposes 15 tools in this mode. Its OS-level and filesystem tools continue working whether or not an agent is attached.

### Run the example in agent mode

From the repository root:

```bash
PYTHONPATH=examples .venv/bin/python -m qt_editor.main --agent
```

The equivalent environment-variable form is:

```bash
QT_EDITOR_AGENT=1 PYTHONPATH=examples .venv/bin/python -m qt_editor.main
```

In agent mode, the example creates its `QMainWindow`, calls `qt_mcp.agent.start_agent(window)`, and runs only the Qt event loop. It does **not** call `examples.qt_editor.mcp_server.build_server`; the MCP client must launch standalone `qt_mcp.server` separately.

### Threading and transport invariants

- `QWidget.grab()` must execute on the Qt main thread. The agent creates `QLocalSocket` on that thread; its `readyRead` slot therefore reads and dispatches capture requests on the GUI thread.
- Requests and responses are newline-delimited JSON frames over a Unix domain socket on Linux/macOS or a Qt local named pipe on Windows. PNG bytes are base64-encoded in the private agent response and converted back to an MCP `Image` by the server.
- On Linux/macOS, the standalone server runs its asyncio Unix-socket proxy in a background daemon thread. On Windows, it runs `QLocalServer` in a daemon thread with its own `QEventLoop`; the public proxy API remains awaitable.
- One app may attach to a socket at a time in v1. A second connection is rejected with `Already attached to another app`.
- Proxy mode works on Linux, macOS, and Windows. Windows uses a Qt `QLocalServer` named pipe and requires PySide6 on the server (`pip install PySide6`); the app-side `QLocalSocket` agent is unchanged. Use OS-level `capture_window` or in-process mode on other platforms.

## In-process embedding (the app is the server)

The embedded pattern runs `QApplication` on the main thread and FastMCP in a daemon background thread. A `CaptureBridge` transfers widget-capture work to the GUI thread and returns PNG bytes to the MCP thread.

The full reference implementation is in [`examples/qt_editor/`](../examples/qt_editor/). The steps below reduce it to the essential pieces.

### 1. Name capturable widgets

Assign a stable `objectName` to each widget that the client may target:

```python
self.preview = QLabel("Build preview")
self.preview.setObjectName("build_preview")
```

Use application-level names rather than generated names. Renaming an advertised object breaks callers just like renaming an API field.

### 2. Resolve names from the main window

Use `findChild(QWidget, name)` so nested widgets can be found:

```python
def widget_for_name(self, name: str):
    if name == self.objectName():
        return self
    return self.findChild(QWidget, name)
```

The explicit self check is necessary if the main window itself is advertised: `findChild` searches descendants, not the object on which it is called.

### 3. Advertise supported names

Keep an allowlist instead of exposing every internal Qt object:

```python
def capturable_names(self) -> list[str]:
    return ["main_window", "build_preview"]
```

The `list_capturable_widgets` tool returns this list. An allowlist makes the MCP-facing widget interface deliberate and stable.

### 4. Marshal capture to the GUI thread

`CaptureBridge` is a `QObject` created on the GUI thread. Its signal carries a widget name and shared state. The MCP thread emits the signal and waits on a `threading.Event`; Qt queues the connected slot onto the receiver's GUI thread. The slot calls `QWidget.grab()`, encodes the `QPixmap` through `QBuffer`, stores the PNG bytes, and releases the waiting thread.

### 5. Build a fresh FastMCP server

Define `build_server(bridge)` and register the tools that your application exposes. Reuse functions from `qt_mcp.screenshots` and `qt_mcp.filesystem`, but route `capture_widget` through the bridge and add `list_capturable_widgets`.

The minimal example below registers the two application-specific tools to keep the listing readable. To expose all 14 example tools, copy or adapt [`examples/qt_editor/mcp_server.py`](../examples/qt_editor/mcp_server.py), whose `build_server` registers the full screenshot and filesystem set.

### 6. Run the two event loops on their required threads

Create `QApplication`, the main window, and `CaptureBridge` on the main thread. Run `mcp.run(transport="stdio")` in a daemon thread, then enter `app.exec()` on the main thread.

### 7. Point the MCP client at the app launcher

Unlike standalone mode, the MCP server process is the application process itself:

```json
{
  "mcpServers": {
    "my-qt-app": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "your.app"]
    }
  }
}
```

On Windows, use the absolute `.venv/Scripts/python.exe` path.

## Minimal complete embedded application

Save this as a module your virtual environment can import, install `qt-mcp` and PySide6, and point the client at `python -m your.app`.

```python
from __future__ import annotations

import logging
import sys
import threading

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from PySide6.QtCore import QByteArray, QBuffer, QObject, Signal, Slot
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QWidget


# stdout belongs exclusively to MCP JSON-RPC.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("my_qt_app")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("My MCP Qt App")
        self.setObjectName("main_window")

        self.preview = QLabel("Build preview")
        self.preview.setObjectName("build_preview")
        self.setCentralWidget(self.preview)
        self.resize(480, 240)

    def widget_for_name(self, name: str):
        if name == self.objectName():
            return self
        return self.findChild(QWidget, name)

    def capturable_names(self) -> list[str]:
        return ["main_window", "build_preview"]


class CaptureBridge(QObject):
    capture_requested = Signal(str, object)

    def __init__(self, main_window: MainWindow) -> None:
        super().__init__()
        self._main_window = main_window
        self.capture_requested.connect(self._on_capture)

    @Slot(str, object)
    def _on_capture(self, widget_name: str, state: dict) -> None:
        try:
            widget = self._main_window.widget_for_name(widget_name)
            if widget is None:
                raise RuntimeError(
                    f"Unknown widget {widget_name!r}; "
                    f"available: {self._main_window.capturable_names()}"
                )

            pixmap = widget.grab()
            data = QByteArray()
            buffer = QBuffer(data)
            if not buffer.open(QBuffer.ReadWrite):
                raise RuntimeError("Could not open PNG buffer")
            try:
                if not pixmap.save(buffer, "PNG"):
                    raise RuntimeError("QPixmap.save('PNG') returned False")
            finally:
                buffer.close()
            state["png"] = bytes(data)
        except Exception as exc:
            state["error"] = str(exc)
        finally:
            state["event"].set()

    def capture(self, widget_name: str, timeout: float = 10.0) -> bytes:
        event = threading.Event()
        state = {"event": event, "png": None, "error": None}
        self.capture_requested.emit(widget_name, state)

        if not event.wait(timeout):
            raise RuntimeError(
                f"Timed out after {timeout}s capturing {widget_name!r}"
            )
        if state["error"] is not None:
            raise RuntimeError(state["error"])
        if state["png"] is None:
            raise RuntimeError("GUI thread returned no PNG")
        return state["png"]


def build_server(bridge: CaptureBridge) -> FastMCP:
    mcp = FastMCP("my-qt-app")

    @mcp.tool()
    def capture_widget(
        window_title: str,
        widget_name: str,
        timeout: float = 10.0,
    ) -> Image:
        """Capture a named widget as PNG."""
        # This one-window example accepts window_title for API compatibility.
        if window_title not in bridge._main_window.windowTitle():
            raise ToolError(f"Window does not match {window_title!r}")
        try:
            png = bridge.capture(widget_name, timeout=timeout)
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        return Image(data=png, format="png")

    @mcp.tool()
    def list_capturable_widgets() -> dict:
        """List widget objectNames accepted by capture_widget."""
        names = bridge._main_window.capturable_names()
        return {"widgets": names, "count": len(names)}

    return mcp


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    bridge = CaptureBridge(window)
    mcp = build_server(bridge)

    def run_mcp() -> None:
        try:
            mcp.run(transport="stdio")
        except Exception:
            logger.exception("MCP server stopped unexpectedly")
            app.quit()

    thread = threading.Thread(target=run_mcp, name="mcp-stdio", daemon=True)
    thread.start()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
```

To add OS-level and filesystem tools to this application, copy the corresponding `@mcp.tool()` wrappers from [`examples/qt_editor/mcp_server.py`](../examples/qt_editor/mcp_server.py). Keep their signatures aligned with the [tool reference](TOOLS.md).

## Threading rules

These constraints are correctness requirements, not optimizations:

1. **`QApplication` must run on the main thread.** Create Qt widgets and execute `QApplication.exec()` there.
2. **FastMCP owns an asyncio loop.** Run `mcp.run(transport="stdio")` in a daemon background thread rather than trying to combine it with the Qt event loop.
3. **Never call `QWidget.grab()` from the MCP thread.** `capture_widget` executes on the MCP thread. It must emit a Qt signal to a `QObject` living on the GUI thread and wait for the GUI-thread slot to finish.
4. **Cross-thread signal delivery is queued automatically.** Emitting `capture_requested` from the MCP thread schedules the receiver's slot on its Qt thread. The `threading.Event` provides the synchronous result boundary expected by the tool call.
5. **Always release the event.** Set it in a `finally` block so exceptions cannot leave the MCP request waiting until timeout.
6. **Reserve stdout for MCP.** MCP stdio is a JSON-RPC byte stream. Send logging, tracebacks, and diagnostics to stderr only. A single ordinary `print()` to stdout can corrupt the protocol.

Do not run a blocking capture before the Qt event loop starts: the queued slot cannot execute until the GUI event loop is processing events.

## Why `QWidget.grab()` is more precise

OS-level capture copies pixels from the desktop compositor or X11 root display. Those pixels may contain another window, clipping, or nothing if the target is outside the visible desktop. A widget's geometry must also be translated into screen coordinates.

`QWidget.grab()` asks Qt to render that widget into a `QPixmap`. The result is scoped to the widget and does not depend on whether another window covers it or whether all of it is on-screen. This makes object names a stable interface for requests such as “capture the build preview” instead of requiring brittle coordinate calculations.

## FastMCP duplicate-tool gotcha

This caveat applies to the in-process recipe; proxy/agent mode uses the standalone server's already-registered proxy tools.

A tool name can be registered only once on a `FastMCP` instance. Registering another function under an existing name raises an exception. Therefore, do not import `qt_mcp.server.mcp` and try to replace its `capture_widget` tool.

Build a fresh `FastMCP` instance and register all desired tools yourself. The example's `build_server(bridge)` does exactly this: it reuses the underlying functions in `qt_mcp.screenshots` and `qt_mcp.filesystem`, but creates new wrappers and supplies the working widget implementation.

## Next steps

- Read [`examples/qt_editor/mcp_server.py`](../examples/qt_editor/mcp_server.py) for the full 14-tool server.
- Consult [TOOLS.md](TOOLS.md) before exposing or changing tool signatures.
- See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete process and capture pipeline.
