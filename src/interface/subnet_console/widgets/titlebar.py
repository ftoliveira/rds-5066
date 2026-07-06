"""Custom (frameless) title bar with macOS-style traffic-light controls."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QPushButton

from .. import __version__
from .. import theme as T
from ..model import ConsoleModel
from . import common as C


def _light(color: str, border: str, on_click) -> QPushButton:
    b = QPushButton()
    b.setFixedSize(12, 12)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet(
        "QPushButton{background:%s;border:1px solid %s;border-radius:6px;}"
        "QPushButton:hover{background:%s;}" % (color, border, T.tint(color, 0.15))
    )
    b.clicked.connect(on_click)
    return b


class TitleBar(QFrame):
    def __init__(self, window, model: ConsoleModel):
        super().__init__()
        self._win = window
        self.model = model
        self._drag_offset = None
        self.setFixedHeight(34)
        C.scoped(self,
                 "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 %s,stop:1 %s);"
                 "border:none;border-bottom:1px solid %s;" % (T.TITLE_TOP, T.TITLE_BOTTOM, T.TITLE_BORDER))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(9)

        dots = C.row(
            _light(T.TL_RED, T.TL_RED_BORDER, window.close),
            _light(T.TL_AMBER, T.TL_AMBER_BORDER, window.showMinimized),
            _light(T.TL_GREEN, T.TL_GREEN_BORDER, self._toggle_max),
            spacing=8,
        )
        lay.addWidget(dots)
        lay.addStretch(1)
        title = ("STANAG 5066 Subnet Console — Annex F Client Manager  ·  %s"
                 % model.node["station"])
        lay.addWidget(C.lbl(title, size=12, weight=600, color=T.TITLE_FG, letter_spacing=0.2))
        lay.addStretch(1)
        lay.addWidget(C.lbl("v%s" % __version__, size=11, mono=True, color="#6c7076"))

    def _toggle_max(self):
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    # ---- frameless drag ----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.MouseButton.LeftButton:
            if self._win.isMaximized():
                self._win.showNormal()
            self._win.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, e):
        self._toggle_max()
