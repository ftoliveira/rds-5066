"""Application bootstrap for the S5066 Subnet Console."""
from __future__ import annotations

import sys
from typing import Optional, Sequence

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from . import theme as T
from .model import ConsoleModel
from .window import SubnetConsoleWindow


def _install_default_font(app: QApplication) -> None:
    T.load_fonts()
    f = QFont()
    f.setFamilies(T.SANS_STACK)
    f.setPixelSize(13)
    app.setFont(f)


def run(node: str = "A", accent: str = T.DEFAULT_ACCENT,
        modem_host: Optional[str] = None, modem_port: Optional[str] = None,
        argv: Optional[Sequence[str]] = None) -> int:
    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("S5066 Subnet Console")
    _install_default_font(app)

    model = ConsoleModel(node=node, accent=accent, modem_host=modem_host, modem_port=modem_port)
    win = SubnetConsoleWindow(model)
    win.show()
    return app.exec()
