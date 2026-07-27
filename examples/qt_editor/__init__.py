"""Example Qt application that hosts the qt-mcp MCP server in-process.

This package demonstrates "App hosts MCP + widget capture": the Qt app runs the
MCP stdio server in a background thread and implements ``capture_widget`` for
real using ``QWidget.grab()`` on the GUI thread.
"""