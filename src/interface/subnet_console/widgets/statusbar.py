"""Bottom status bar: SIS/clients/traffic/link readouts, live UTC clock, grip.

The readouts come from ``model.statusbar_view()`` and are rebuilt whenever the
model reports a status/traffic change; the clock (QTimer) and the resize grip
persist across rebuilds.
"""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QSizeGrip, QWidget

from .. import theme as T
from ..model import ConsoleModel
from . import common as C


def _txt(s, color=T.STATUS_FG, weight=400):
    return C.lbl(s, size=11, mono=True, color=color, weight=weight)


class StatusBar(QFrame):
    def __init__(self, model: ConsoleModel):
        super().__init__()
        self.model = model
        self.setFixedHeight(26)
        self.setStyleSheet(
            "QFrame#status{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 %s,stop:1 %s);"
            "border:none;border-top:1px solid %s;}" % (T.STATUS_TOP, T.STATUS_BOTTOM, T.STATUS_BORDER)
        )
        self.setObjectName("status")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 4, 0)
        lay.setSpacing(0)

        # Dynamic readouts live in their own holder so they can be rebuilt
        # without disturbing the clock/grip.
        self._content = QWidget()
        self._cl = QHBoxLayout(self._content)
        self._cl.setContentsMargins(0, 0, 0, 0)
        self._cl.setSpacing(0)
        lay.addWidget(self._content, 1)

        self._clock = _txt("--:--:-- UTC", color="#25282c", weight=600)
        lay.addWidget(self._clock)
        lay.addSpacing(6)
        grip = QSizeGrip(self)
        grip.setStyleSheet("background:transparent;")
        lay.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        model.changed.connect(self._on_changed)
        model.accent_changed.connect(self._rebuild_content)
        self._rebuild_content()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

    def _on_changed(self, topic: str) -> None:
        if topic in ("modem", "statusbar", "dashboard"):
            self._rebuild_content()

    def _rebuild_content(self) -> None:
        C.clear_layout(self._cl)
        v = self.model.statusbar_view()
        self._cl.addWidget(C.dot(v["sis_dot"], 7))
        self._cl.addSpacing(6)
        self._cl.addWidget(_txt(v["sis_label"]))
        self._cl.addWidget(self._sep())
        self._cl.addWidget(_txt(v["clients"]))
        self._cl.addWidget(self._sep())
        self._cl.addWidget(_txt(v["traffic"]))
        self._cl.addStretch(1)
        self._cl.addWidget(_txt(v["right"]))
        self._cl.addWidget(self._sep())
        self._cl.addWidget(_txt(v["node"]))
        self._cl.addWidget(self._sep())

    def _sep(self) -> QFrame:
        f = C.vline(T.STATUS_BORDER, 14)
        f.setContentsMargins(12, 0, 12, 0)
        wrap = QFrame()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(12, 0, 12, 0)
        wl.addWidget(f)
        return wrap

    def _tick(self):
        self._clock.setText(time.strftime("%H:%M:%S", time.gmtime()) + " UTC")
