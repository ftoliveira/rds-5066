"""Toolbar: modem-link status pill, quick actions and RF readouts."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from .. import theme as T
from ..model import ConsoleModel
from . import common as C


def _readout(label: str, value: str, value_color: str = "#25282c") -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    v.addWidget(C.lbl(label, size=9.5, mono=True, color="#898d93", letter_spacing=0.5, upper=True))
    v.addWidget(C.lbl(value, size=11.5, mono=True, weight=600, color=value_color))
    return w


class Toolbar(QFrame):
    def __init__(self, model: ConsoleModel):
        super().__init__()
        self.model = model
        self.setFixedHeight(46)
        self.setStyleSheet(
            "QFrame#tb{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 %s,stop:1 %s);"
            "border:none;border-bottom:1px solid %s;}" % (T.TOOLBAR_TOP, T.TOOLBAR_BOTTOM, T.MENU_BORDER)
        )
        self.setObjectName("tb")
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(12, 0, 12, 0)
        self._lay.setSpacing(8)
        model.changed.connect(self._on_changed)
        model.accent_changed.connect(self._rebuild)
        self._rebuild()

    def _on_changed(self, topic: str):
        if topic in ("modem", "radio", "radio_tele", "toolbar"):
            self._rebuild()

    def _rebuild(self):
        C.clear_layout(self._lay)
        mv = self.model.modem_view()
        stat = mv["stat"]

        pill = C.ClickableFrame(
            hover_bg=None,
            base_css="QFrame{background:%s;border:1px solid %s;border-radius:4px;}" % (stat["bg"], stat["border"]),
        )
        pill.setToolTip("MIL-STD-188-110C modem link")
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(12, 5, 12, 5)
        pl.setSpacing(7)
        pl.addWidget(C.dot(stat["dot"], 9, halo=stat["halo"]))
        pl.addWidget(C.lbl(mv["top_label"], size=12, weight=600, color=stat["fg"]))
        pill.clicked.connect(lambda: self.model.set_screen("modem"))
        self._lay.addWidget(pill)

        self._lay.addWidget(C.vline(T.MENU_BORDER, 26))

        # BIND ALL button carries an accent dot + label
        bind = self._chip_button([C.dot(self.accent, 7), C.lbl("BIND ALL", size=12, weight=500, color="#2a2d31")])
        self._lay.addWidget(bind)
        self._lay.addWidget(self._chip_button([C.lbl("Hard Link", size=12, weight=500, color="#2a2d31")]))
        self._lay.addWidget(self._chip_button([C.lbl("Broadcast", size=12, weight=500, color="#2a2d31")]))

        self._lay.addStretch(1)

        rf = self.model.rf_readouts()
        readouts = QHBoxLayout()
        readouts.setSpacing(16)
        readouts.addWidget(_readout("FREQ", rf["freq"]))
        readouts.addWidget(_readout("MODE", rf["mode"]))
        readouts.addWidget(_readout("RATE", rf["rate"]))
        readouts.addWidget(_readout("SNR", rf["snr"], value_color=rf["snr_color"]))
        holder = QWidget()
        holder.setLayout(readouts)
        self._lay.addWidget(holder)

    @property
    def accent(self) -> str:
        return self.model.theme.accent

    def _chip_button(self, widgets) -> C.ClickableFrame:
        f = C.ClickableFrame(
            hover_bg=None,
            base_css="QFrame{background:%s;border:1px solid #b9bcc1;border-radius:4px;}" % T.INPUT_BG_ALT,
        )
        lay = QHBoxLayout(f)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(6)
        for w in widgets:
            lay.addWidget(w)
        return f
