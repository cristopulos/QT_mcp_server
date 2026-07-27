# Tool reference

This document is the authoritative reference for the tools exposed by `qt-mcp`. Calls are shown as JSON argument objects, as sent in an MCP `tools/call` request. Paths in examples are illustrative and returned paths are resolved absolute paths.

The standalone server (`qt_mcp.server`) exposes 13 tools. The in-process example (`examples.qt_editor.mcp_server.build_server`) exposes the same tools, replaces the `capture_widget` stub with a working implementation, and adds `list_capturable_widgets`.

## Screenshot tools

### `list_windows`

Lists visible top-level windows with titles and physical-pixel geometry.

```python
list_windows() -> dict
```

Parameters: none.

Return shape:

```text
{
  "windows": [
    {
      "id": str,
      "title": str,
      "desktop": int,
      "x": int,
      "y": int,
      "width": int,
      "height": int
    }
  ],
  "count": int
}
```

`desktop` is `-1` when sticky, unknown, or unavailable.

Example call:

```json
{}
```

Example return:

```json
{
  "windows": [
    {
      "id": "0x0380000a",
      "title": "MCP Demo Editor — qt_editor",
      "desktop": 0,
      "x": 120,
      "y": 80,
      "width": 900,
      "height": 600
    }
  ],
  "count": 1
}
```

### `capture_window`

Captures an entire window selected by its title.

```python
capture_window(title: str, exact: bool = False) -> Image
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `title` | `str` | Required | Case-insensitive title substring unless `exact` is true. |
| `exact` | `bool` | `False` | Require a case-sensitive exact title match. |

Return: MCP image content containing PNG bytes (`mimeType: "image/png"`). A non-exact query that matches multiple windows is an error.

Example call:

```json
{
  "title": "MCP Demo Editor",
  "exact": false
}
```

Example return envelope:

```json
{
  "content": [
    {
      "type": "image",
      "data": "iVBORw0KGgoAAAANSUhEUgAA...",
      "mimeType": "image/png"
    }
  ],
  "isError": false
}
```

### `capture_region`

Captures a rectangular screen region, either absolutely or relative to a window.

```python
capture_region(
    x: int,
    y: int,
    width: int,
    height: int,
    title: Optional[str] = None,
) -> Image
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `x` | `int` | Required | Absolute screen X, or X offset from the selected window. |
| `y` | `int` | Required | Absolute screen Y, or Y offset from the selected window. |
| `width` | `int` | Required | Width in pixels; must be positive. |
| `height` | `int` | Required | Height in pixels; must be positive. |
| `title` | `Optional[str]` | `None` | When supplied, resolve this window and treat `x` and `y` as offsets from its top-left corner. |

Return: MCP image content containing PNG bytes (`mimeType: "image/png"`).

Example call:

```json
{
  "x": 10,
  "y": 40,
  "width": 400,
  "height": 300,
  "title": "MCP Demo Editor"
}
```

Example return envelope:

```json
{
  "content": [
    {
      "type": "image",
      "data": "iVBORw0KGgoAAAANSUhEUgAA...",
      "mimeType": "image/png"
    }
  ],
  "isError": false
}
```

### `capture_widget` in the standalone server

Reports that Qt-internal widget capture is unavailable and directs callers to `capture_region`.

```python
capture_widget(window_title: str, widget_name: str) -> Image
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `window_title` | `str` | Required | Target window title. Retained for the intended widget-capture interface. |
| `widget_name` | `str` | Required | Intended Qt `objectName`. |

Return: this standalone implementation does not return an image successfully. It raises `ToolError`, represented by MCP as `isError: true`, with fallback guidance.

Example call:

```json
{
  "window_title": "MCP Demo Editor",
  "widget_name": "editor_area"
}
```

Example error return:

```json
{
  "content": [
    {
      "type": "text",
      "text": "Qt-internal widget capture is not available. The target Qt application has no capture agent loaded. Use capture_region with x/y/width/height (relative to the window via the title argument) instead."
    }
  ],
  "isError": true
}
```

### `capture_widget` in the in-process example

Captures a named widget through `QWidget.grab()` on the Qt GUI thread.

```python
capture_widget(
    window_title: str,
    widget_name: str,
    timeout: float = 10.0,
) -> Image
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `window_title` | `str` | Required | Title or substring used to confirm that the application window is discoverable. |
| `widget_name` | `str` | Required | Advertised Qt `objectName`; use `list_capturable_widgets`. |
| `timeout` | `float` | `10.0` | Seconds to wait for GUI-thread capture. |

Return: MCP image content containing PNG bytes (`mimeType: "image/png"`). Unlike OS-level capture, this can capture an occluded or off-screen widget.

Example call:

```json
{
  "window_title": "MCP Demo Editor",
  "widget_name": "editor_area",
  "timeout": 10.0
}
```

Example return envelope:

```json
{
  "content": [
    {
      "type": "image",
      "data": "iVBORw0KGgoAAAANSUhEUgAA...",
      "mimeType": "image/png"
    }
  ],
  "isError": false
}
```

### `list_capturable_widgets` in the in-process example

Lists the widget names accepted by the in-process `capture_widget` tool.

```python
list_capturable_widgets() -> dict
```

Parameters: none.

Return shape: `{"widgets": [str], "count": int}`.

Example call:

```json
{}
```

Example return:

```json
{
  "widgets": [
    "toolbar",
    "editor_area",
    "sidebar",
    "status_bar",
    "main_window"
  ],
  "count": 5
}
```

## Filesystem tools

The filesystem tools accept absolute paths or paths relative to the server process's working directory. They expand `~` and return resolved absolute paths.

### `read_file`

Reads a page of lines from a UTF-8 text file.

```python
read_file(path: str, offset: int = 0, limit: int = 2000) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | Required | File to read. |
| `offset` | `int` | `0` | Zero-based line offset; negative values are normalized to zero. |
| `limit` | `int` | `2000` | Maximum number of lines to return. |

Return shape: `{"path": str, "content": str, "total_lines": int, "returned_lines": int, "offset": int, "truncated": bool}`.

Example call:

```json
{
  "path": "README.md",
  "offset": 0,
  "limit": 2
}
```

Example return:

```json
{
  "path": "/home/you/qt-mcp/README.md",
  "content": "# Qt MCP\n\n",
  "total_lines": 180,
  "returned_lines": 2,
  "offset": 0,
  "truncated": true
}
```

### `write_file`

Writes or appends UTF-8 text.

```python
write_file(
    path: str,
    content: str,
    append: bool = False,
    create_parents: bool = True,
) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | Required | Destination file. |
| `content` | `str` | Required | Text to write. |
| `append` | `bool` | `False` | Append instead of replacing the file. |
| `create_parents` | `bool` | `True` | Create missing parent directories. |

Return shape: `{"path": str, "bytes_written": int, "appended": bool}`. `bytes_written` is the UTF-8 byte length of `content`.

Example call:

```json
{
  "path": "/tmp/qt-mcp/notes.txt",
  "content": "Toolbar inspected.\n",
  "append": false,
  "create_parents": true
}
```

Example return:

```json
{
  "path": "/tmp/qt-mcp/notes.txt",
  "bytes_written": 19,
  "appended": false
}
```

### `list_directory`

Lists direct children of a directory.

```python
list_directory(path: str = ".", include_hidden: bool = False) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | `"."` | Directory to list. |
| `include_hidden` | `bool` | `False` | Include names beginning with `.`. |

Return shape: `{"path": str, "entries": [{"name": str, "path": str, "type": str, "size": int}], "count": int}`. Entry `type` is `file`, `dir`, `symlink`, or `other`; size may be `-1` if metadata cannot be read.

Example call:

```json
{
  "path": "src/qt_mcp",
  "include_hidden": false
}
```

Example return:

```json
{
  "path": "/home/you/qt-mcp/src/qt_mcp",
  "entries": [
    {
      "name": "server.py",
      "path": "/home/you/qt-mcp/src/qt_mcp/server.py",
      "type": "file",
      "size": 10420
    }
  ],
  "count": 1
}
```

### `create_directory`

Creates a directory.

```python
create_directory(path: str, parents: bool = True) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | Required | Directory to create. |
| `parents` | `bool` | `True` | Create missing parent directories. |

Return shape: `{"path": str, "created": bool, "exists": bool}`. If the directory already exists, `created` is false and `exists` is true.

Example call:

```json
{
  "path": "/tmp/qt-mcp/output",
  "parents": true
}
```

Example return:

```json
{
  "path": "/tmp/qt-mcp/output",
  "created": true,
  "exists": false
}
```

### `move`

Moves or renames a file or directory.

```python
move(path: str, destination: str, overwrite: bool = False) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | Required | Existing source. |
| `destination` | `str` | Required | Destination whose parent must already exist. |
| `overwrite` | `bool` | `False` | Delete an existing destination before moving. |

Return shape: `{"source": str, "destination": str, "moved": bool}`.

Example call:

```json
{
  "path": "/tmp/qt-mcp/draft.txt",
  "destination": "/tmp/qt-mcp/final.txt",
  "overwrite": false
}
```

Example return:

```json
{
  "source": "/tmp/qt-mcp/draft.txt",
  "destination": "/tmp/qt-mcp/final.txt",
  "moved": true
}
```

### `delete`

Deletes a file, symlink, or directory tree.

```python
delete(path: str, recursive: bool = False) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | Required | Entry to delete. Symlinks are unlinked without deleting their targets. |
| `recursive` | `bool` | `False` | Must be true for directories; files do not require it. |

Return shape: `{"path": str, "deleted": bool}`.

Example call:

```json
{
  "path": "/tmp/qt-mcp/output",
  "recursive": true
}
```

Example return:

```json
{
  "path": "/tmp/qt-mcp/output",
  "deleted": true
}
```

### `file_info`

Returns metadata for a file, directory, or symlink.

```python
file_info(path: str) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | Required | Entry to inspect. |

Return shape: `{"path": str, "type": str, "size": int, "modified": str, "is_symlink": bool, "permissions": str}`. `modified` is ISO-8601 UTC; `permissions` is an octal string.

Example call:

```json
{
  "path": "pyproject.toml"
}
```

Example return:

```json
{
  "path": "/home/you/qt-mcp/pyproject.toml",
  "type": "file",
  "size": 812,
  "modified": "2026-07-27T10:30:00+00:00",
  "is_symlink": false,
  "permissions": "0o644"
}
```

### `search_glob`

Finds paths recursively with Python `Path.glob` patterns.

```python
search_glob(
    path: str = ".",
    pattern: str = "*",
    include_hidden: bool = False,
    max_results: int = 500,
) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | `"."` | Root directory. |
| `pattern` | `str` | `"*"` | Glob such as `**/*.py` or `src/**/*.json`. |
| `include_hidden` | `bool` | `False` | Include paths with hidden components. |
| `max_results` | `int` | `500` | Maximum matches returned. |

Return shape: `{"path": str, "pattern": str, "matches": [str], "count": int, "truncated": bool}`.

Example call:

```json
{
  "path": ".",
  "pattern": "**/*.py",
  "include_hidden": false,
  "max_results": 500
}
```

Example return:

```json
{
  "path": "/home/you/qt-mcp",
  "pattern": "**/*.py",
  "matches": [
    "/home/you/qt-mcp/src/qt_mcp/server.py",
    "/home/you/qt-mcp/src/qt_mcp/screenshots.py"
  ],
  "count": 2,
  "truncated": false
}
```

### `search_content`

Searches text recursively with a regular expression.

```python
search_content(
    path: str = ".",
    pattern: str = "",
    include_hidden: bool = False,
    file_glob: str = "*",
    case_sensitive: bool = False,
    max_matches: int = 100,
    context_lines: int = 0,
) -> dict
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | `"."` | Directory or single file to search. |
| `pattern` | `str` | `""` | Required non-empty Python regular expression. |
| `include_hidden` | `bool` | `False` | Include paths with hidden components. |
| `file_glob` | `str` | `"*"` | Restrict files, for example `*.py`. |
| `case_sensitive` | `bool` | `False` | Use case-sensitive regex matching. |
| `max_matches` | `int` | `100` | Maximum matching lines returned. |
| `context_lines` | `int` | `0` | Number of surrounding lines included before and after each match. |

Return shape: `{"path": str, "pattern": str, "file_glob": str, "matches": [{"file": str, "line": int, "match": str, "context": Optional[str]}], "count": int, "files_scanned": int, "truncated": bool}`. `context` is null when `context_lines` is zero.

Example call:

```json
{
  "path": "src",
  "pattern": "ToolError",
  "include_hidden": false,
  "file_glob": "*.py",
  "case_sensitive": true,
  "max_matches": 100,
  "context_lines": 1
}
```

Example return:

```json
{
  "path": "/home/you/qt-mcp/src",
  "pattern": "ToolError",
  "file_glob": "*.py",
  "matches": [
    {
      "file": "/home/you/qt-mcp/src/qt_mcp/server.py",
      "line": 28,
      "match": "from mcp.server.fastmcp.exceptions import ToolError",
      "context": "from mcp.server.fastmcp import FastMCP, Image\nfrom mcp.server.fastmcp.exceptions import ToolError\n"
    }
  ],
  "count": 1,
  "files_scanned": 4,
  "truncated": false
}
```

## Errors

Capture and filesystem failures are converted to `ToolError`. MCP clients receive these tool failures with `isError: true` and a textual explanation. Common causes include ambiguous window titles, missing X11 enumeration tools, invalid dimensions or regexes, missing paths, and insufficient filesystem permissions.
