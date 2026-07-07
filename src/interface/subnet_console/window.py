"""SubnetConsoleWindow — frameless main window that assembles the console.

Layout (top → bottom): title bar · menu bar · toolbar · body (sidebar + content
stack) · status bar. Navigation is driven entirely by
:meth:`ConsoleModel.set_screen`; the window just switches the stack and repaints
the outer frame when the accent changes.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from . import theme as T
from .model import SCREENS, ConsoleModel
from .widgets.menubar import build_menubar
from .widgets.sidebar import Sidebar
from .widgets.statusbar import StatusBar
from .widgets.titlebar import TitleBar
from .widgets.toolbar import Toolbar


def _build_screens(model: ConsoleModel) -> dict:
    """Instantiate each content screen (real module if present, else placeholder)."""
    from .widgets.screens.base import Placeholder
    from .widgets.screens.dashboard import DashboardScreen
    from .widgets.screens.monitor import MonitorScreen
    from .widgets.screens.chat import ChatScreen
    from .widgets.screens.mail import MailScreen
    from .widgets.screens.ipclient import IpClientScreen
    from .widgets.screens.filexfer import FileTransferScreen
    from .widgets.screens.radio import RadioScreen
    from .widgets.screens.sissocket import SisSocketScreen
    from .widgets.screens.modem import ModemScreen
    from .widgets.screens.config import ConfigScreen

    return {
        "dashboard": DashboardScreen(model),
        "monitor": MonitorScreen(model),
        "chat": ChatScreen(model),
        "mail": MailScreen(model),
        "ipclient": IpClientScreen(model),
        "filexfer": FileTransferScreen(model),
        "radio": RadioScreen(model),
        "sissocket": SisSocketScreen(model),
        "modem": ModemScreen(model),
        "config": ConfigScreen(model),
    }


class SubnetConsoleWindow(QFrame):
    def __init__(self, model: ConsoleModel):
        super().__init__()
        self.model = model
        self.setWindowTitle("STANAG 5066 Subnet Console")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1500, 956)
        self.setMinimumSize(1160, 720)
        self._apply_frame_style()

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)   # let the frame border show
        root.setSpacing(0)

        root.addWidget(TitleBar(self, model))
        root.addWidget(build_menubar(self, model))
        root.addWidget(Toolbar(model))

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addWidget(Sidebar(model))

        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background:{T.CONTENT_BG};")
        self._screens = _build_screens(model)
        self._index = {}
        for i, name in enumerate(SCREENS):
            self._stack.addWidget(self._screens[name])
            self._index[name] = i
        bl.addWidget(self._stack, 1)
        root.addWidget(body, 1)

        root.addWidget(StatusBar(model))

        model.screen_changed.connect(self._on_screen)
        model.accent_changed.connect(self._apply_frame_style)
        self._on_screen(model.screen)

    def _apply_frame_style(self):
        self.setStyleSheet(
            "SubnetConsoleWindow{background:%s;border:1px solid %s;border-radius:7px;}"
            % (T.WINDOW_BG, T.WINDOW_BORDER)
        )

    def _on_screen(self, name: str):
        if name in self._index:
            self._stack.setCurrentIndex(self._index[name])
