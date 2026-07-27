"""Qt MCP — an MCP server for interacting with Qt applications via screenshots and basic filesystem operations."""

from __future__ import annotations

__version__ = "0.3.0"

# Export proxy types (no PySide6 dependency).
from .agent_proxy import AgentError, AgentProxy  # noqa: F401

# Export agent types — guarded so importing qt_mcp doesn't hard-require PySide6.
try:
    from .agent import Agent, start_agent  # noqa: F401
except ImportError:
    # PySide6 not installed; agent module is available via explicit import.
    pass
