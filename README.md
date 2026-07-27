# Qt MCP

An MCP (Model Context Protocol) server for inspecting Qt applications through screenshots and performing basic filesystem operations.

`qt-mcp` 0.2.0 lets an MCP-capable AI client see a Qt or desktop application, inspect complete windows, selected regions, or named widgets, and read or update nearby project files. It uses the MCP Python SDK v1.28.1 high-level `FastMCP` API and stdio transport. The architecture is inspired by [VibeUE](https://github.com/kevinpbuckley/VibeUE), adapted for Qt and general desktop windows.

## Features at a glance

### Screenshot tools

- Enumerate visible top-level windows and their geometry.
- Capture a whole window by exact or partial title.
- Capture an absolute screen rectangle or a rectangle relative to a window.
- Capture a named `QWidget` precisely through the standalone proxy/agent or the embedded server.
- Let the standalone server wait for an agent-enabled Qt app to attach over a Unix domain socket.
- Return screenshots directly as PNG image content to the MCP client.

### Filesystem tools

- Read and write text files.
- List and create directories.
- Move, rename, and delete files or directory trees.
- Inspect file metadata.
- Search recursively by glob or regular-expression content.

See the complete [tool reference](docs/TOOLS.md) for exact signatures and return shapes.

## Platform support

| Platform | Window enumeration | Screen grab | Extra system dependency |
|---|---|---|---|
| Linux/X11 | `wmctrl -lG`, with `xwininfo` fallback | `mss` | `wmctrl` or `x11-utils` |
| Windows | Win32 `EnumWindows` through stdlib `ctypes` | `mss` | None |

Wayland and macOS are not currently supported by the OS-level window-discovery path. Linux also has an optional ImageMagick `import` fallback if `mss` cannot grab the display.

## Requirements

- Python 3.10 or newer.
- An active desktop session.
- Linux: an X11 session with `DISPLAY` set, plus `wmctrl` or `xwininfo` from `x11-utils`.
- Windows: no additional system package.

On Debian or Ubuntu:

```bash
sudo apt install wmctrl
# Alternatively: sudo apt install x11-utils
```

## Quick start

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m qt_mcp.server
```

On Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m qt_mcp.server
```

The installed console script is also available as `.venv/bin/qt-mcp` on Linux or `.venv\Scripts\qt-mcp.exe` on Windows. The server uses stdin and stdout for MCP JSON-RPC, so it normally should be launched by an MCP client rather than an interactive terminal.

## Proxy mode

A Qt app can remain a normal application and opt into exact widget capture with one call:

```python
from qt_mcp.agent import start_agent
agent = start_agent(window)
```

The MCP client launches standalone `qt_mcp.server`, which binds `/tmp/qt-mcp-<uid>.sock` on Linux and proxies `capture_widget(widget_name)` to the attached app's GUI thread. See the complete [proxy/agent integration recipe](docs/INTEGRATION.md#proxyagent-mode-attach-to-a-running-qt-app).

## Connect an MCP client

Claude Code, Cursor, Antigravity, and other stdio MCP clients use the same command-and-arguments pattern. Put the server entry in the client's MCP configuration file; the exact configuration-file location varies by client.

Use the **absolute path to the virtual environment's Python executable**. GUI clients often start with a restricted `PATH`, so relying on `python` or `qt-mcp` being globally discoverable is fragile.

### Linux

```json
{
  "mcpServers": {
    "qt-mcp": {
      "command": "/home/you/projects/Gui Editor MCP/.venv/bin/python",
      "args": ["-m", "qt_mcp.server"]
    }
  }
}
```

### Windows

```json
{
  "mcpServers": {
    "qt-mcp": {
      "command": "C:/Users/you/projects/Gui Editor MCP/.venv/Scripts/python.exe",
      "args": ["-m", "qt_mcp.server"]
    }
  }
}
```

Forward slashes avoid JSON backslash escaping on Windows. A Linux template is also available in [`mcp-config.example.json`](mcp-config.example.json).

## Three operating modes

| Mode | Process model | Tools | Widget capture | Best for |
|---|---|---:|---|---|
| Standalone OS capture, `qt_mcp.server` | Separate stdio server observes desktop windows | 15 | OS-level windows and regions | Zero-change setup and quick inspection |
| Standalone proxy + Qt agent | The same standalone server proxies to one attached Qt app | 15 | Real `QWidget.grab()` through `capture_widget(widget_name)` | Reusable server plus a normal independently launched Qt app |
| In-process example, `examples/qt_editor/` | Qt app embeds FastMCP in a background thread | 14 | Real `QWidget.grab()` through its own tool signature | Tight coupling where the app itself is the MCP server |

The standalone server always advertises 15 tools; its three proxy tools start the Linux socket listener lazily and require an attached agent for widget operations. OS-level mode requires no target-app changes. Proxy and in-process modes both capture occluded or off-screen widgets precisely. Follow the [integration guide](docs/INTEGRATION.md), and see the implementations in [`src/qt_mcp/agent.py`](src/qt_mcp/agent.py) and [`examples/qt_editor/`](examples/qt_editor/).

## Example workflow

```python
# 1. Find the Qt app window
list_windows()
# -> {"windows": [{"id": "0x038...", "title": "My Qt App — main.cpp", ...}], ...}

# 2. Capture the whole window (the vision model receives the PNG)
capture_window(title="My Qt App")

# 3. Capture a panel relative to that window
capture_region(title="My Qt App", x=10, y=40, width=400, height=300)

# 4. Save inspection notes through the filesystem tools
write_file(path="/tmp/notes.md", content="Top panel shows the toolbar...")
```

This screenshot-to-inspection-to-filesystem loop lets the client correlate the rendered interface with the files that produce it.

## Project layout

```text
.
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── INTEGRATION.md
│   └── TOOLS.md
├── examples/
│   ├── qt_editor/
│   │   ├── __init__.py
│   │   ├── editor_window.py
│   │   ├── main.py
│   │   └── mcp_server.py
│   └── qt-editor.mcp-config.example.json
├── src/qt_mcp/
│   ├── __init__.py
│   ├── agent.py
│   ├── agent_proxy.py
│   ├── filesystem.py
│   ├── protocol.py
│   ├── screenshots.py
│   └── server.py
├── tests/
├── mcp-config.example.json
├── pyproject.toml
└── README.md
```

## Documentation

- [Tool reference](docs/TOOLS.md) — exact signatures, parameters, return shapes, and calls for all standalone and in-process tools.
- [Integration guide](docs/INTEGRATION.md) — how to use OS-level capture, attach an agent to the standalone proxy, or embed a server in your Qt app.
- [Architecture](docs/ARCHITECTURE.md) — capture pipeline, module responsibilities, concurrency model, dependencies, and errors.
- [Development](docs/DEVELOPMENT.md) — contributor setup, manual stdio testing, adding tools, platform checks, and releases.

## Roadmap

- Add Windows named-pipe support for proxy/agent mode.
- Add Wayland OS-level capture through platform-appropriate tools or portals.
- Add macOS window discovery and capture.
- Generalize `CaptureBridge` into a reusable package component.
- Add configurable screenshot persistence.
- Expand automated platform and protocol tests.

## License

MIT
