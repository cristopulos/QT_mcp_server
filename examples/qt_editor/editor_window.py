"""The example Qt editor window.

A multi-panel, editor-like window with distinct, named QWidget children so the
``capture_widget`` MCP tool has meaningful targets:

  - toolbar        — a top QToolBar with a few actions
  - sidebar        — a left QDockWidget with a tree/list and controls
  - editor_area    — a central QPlainTextEdit (the main editing surface)
  - status_bar     — the window's QStatusBar (status messages)

Every widget that should be capturable by name is given an ``objectName``.
``MainWindow.widget_for_name()`` resolves an objectName to the QWidget instance.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

WINDOW_TITLE = "MCP Demo Editor — qt_editor"


class SidebarWidget(QWidget):
    """Left sidebar: a list of items plus a couple of controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar_content")
        layout = QVBoxLayout(self)

        self.file_list = QListWidget()
        self.file_list.setObjectName("sidebar_file_list")
        self.file_list.addItems(
            ["main.cpp", "utils.cpp", "mainwindow.py", "README.md", "CMakeLists.txt"]
        )
        layout.addWidget(self.file_list)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("sidebar_refresh_btn")
        layout.addWidget(self.refresh_btn)

        self.info_label = QLabel("5 files")
        self.info_label.setObjectName("sidebar_info_label")
        layout.addWidget(self.info_label)


class EditorWindow(QMainWindow):
    """The top-level application window with capturable, named widgets."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(900, 600)
        self.setObjectName("main_window")

        # --- Toolbar (capturable as "toolbar") ---
        tb = QToolBar("Main Toolbar")
        tb.setObjectName("toolbar")
        self.addToolBar(tb)
        self.action_new = QAction("New", self)
        self.action_new.setObjectName("action_new")
        self.action_open = QAction("Open", self)
        self.action_open.setObjectName("action_open")
        self.action_save = QAction("Save", self)
        self.action_save.setObjectName("action_save")
        tb.addAction(self.action_new)
        tb.addAction(self.action_open)
        tb.addAction(self.action_save)

        # --- Central editor (capturable as "editor_area") ---
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("editor_area")
        self.editor.setPlaceholderText("Start typing...")
        self.editor.setPlainText(
            "# MCP Demo Editor\n\n"
            "This window is capturable by the qt-mcp server running in-process.\n\n"
            "Try capture_widget(window_title=..., widget_name=\"editor_area\")\n"
            "or widget_name=\"sidebar\" or widget_name=\"toolbar\".\n"
        )
        self.setCentralWidget(self.editor)

        # --- Sidebar dock (capturable as "sidebar") ---
        self.sidebar_dock = QDockWidget("Files", self)
        self.sidebar_dock.setObjectName("sidebar_dock")
        self.sidebar_dock.setWidget(SidebarWidget())
        self.sidebar_dock.setObjectName("sidebar")  # name the dock itself
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.sidebar_dock)

        # --- Status bar (capturable as "status_bar") ---
        self.status = QStatusBar()
        self.status.setObjectName("status_bar")
        self.status.showMessage("Ready")
        self.setStatusBar(self.status)

        # Wire a couple of actions to the status bar for life signs.
        self.action_new.triggered.connect(lambda: self.status.showMessage("New file", 3000))
        self.action_open.triggered.connect(lambda: self.status.showMessage("Open file", 3000))
        self.action_save.triggered.connect(lambda: self.status.showMessage("Saved", 3000))

        # Keep a registry of objectName -> QWidget for the capture bridge.
        # QMainWindow already exposes findChild; this is a convenience index of the
        # widgets we advertise as capturable.
        self._capturable_names = [
            "toolbar",
            "editor_area",
            "sidebar",
            "sidebar_dock",
            "sidebar_content",
            "sidebar_file_list",
            "sidebar_refresh_btn",
            "sidebar_info_label",
            "status_bar",
            "main_window",
        ]

    def widget_for_name(self, name: str):
        """Resolve an objectName to a QWidget.

        Returns the widget or None. Searches the whole widget tree so nested
        names (e.g. ``sidebar_file_list``) resolve correctly.
        """
        if not name:
            return None
        # findChild searches children by objectName.
        w = self.findChild(QWidget, name)
        return w

    def capturable_names(self) -> list[str]:
        return list(self._capturable_names)