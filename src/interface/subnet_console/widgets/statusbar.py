"""Bottom status bar with a live UTC clock and a resize grip."""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QSizeGrip

from .. import theme as T
from ..model import ConsoleModel
from . import common as C


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

        def txt(s, color=T.STATUS_FG, weight=400):
            return C.lbl(s, size=11, mono=True, color=color, weight=weight)

        lay.addWidget(C.dot(T.GREEN, 7))
        lay.addSpacing(6)
        lay.addWidget(txt("SIS 127.0.0.1:5066 LISTENING"))
        lay.addWidget(self._sep())
        lay.addWidget(txt("5 CLIENTS BOUND"))
        lay.addWidget(self._sep())
        lay.addWidget(txt("TX 14 · RX 2"))
        lay.addStretch(1)
        n = model.node
        lay.addWidget(txt(f"{n['waveform']} · {n['dataRate']} · SNR {n['snr']}"))
        lay.addWidget(self._sep())
        lay.addWidget(txt(f"NODE {n['address']}"))
        lay.addWidget(self._sep())
        self._clock = txt("--:--:-- UTC", color="#25282c", weight=600)
        lay.addWidget(self._clock)
        lay.addSpacing(6)
        grip = QSizeGrip(self)
        grip.setStyleSheet("background:transparent;")
        lay.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

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
