"""Left navigation rail: local node, section links and SIS-server status."""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .. import theme as T
from ..model import ConsoleModel
from . import common as C


def _icon(text: str, size: int, bg: str, fg: str) -> QLabel:
    w = QLabel(text)
    w.setFixedSize(20, 20)
    w.setAlignment(Qt.AlignmentFlag.AlignCenter)
    f = w.font()
    f.setFamilies(T.MONO_STACK)
    f.setPixelSize(size)
    f.setBold(True)
    w.setFont(f)
    w.setStyleSheet(f"QLabel{{background:{bg};color:{fg};border-radius:4px;}}")
    return w


class Sidebar(QFrame):
    def __init__(self, model: ConsoleModel):
        super().__init__()
        self.model = model
        self.setFixedWidth(244)
        self.setStyleSheet("QFrame#sb{background:%s;border:none;border-right:1px solid %s;}"
                           % (T.SIDEBAR_BG, T.MENU_BORDER))
        self.setObjectName("sb")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        model.screen_changed.connect(lambda *_: self._rebuild())
        model.changed.connect(self._on_changed)
        model.accent_changed.connect(self._rebuild)
        self._rebuild()

    def _on_changed(self, topic: str):
        if topic in ("mail", "filexfer", "modem", "radio"):
            self._rebuild()

    def _rebuild(self):
        C.clear_layout(self._lay)
        m = self.model
        a = m.theme.accent

        # ---- local node ----
        node = QFrame()
        C.scoped(node, "background:transparent;border:none;border-bottom:1px solid %s;" % T.SIDEBAR_DIV)
        nl = QVBoxLayout(node)
        nl.setContentsMargins(16, 14, 16, 10)
        nl.setSpacing(2)
        nl.addWidget(C.caption("LOCAL NODE", size=10, color="#898d93"))
        idrow = QHBoxLayout()
        idrow.setSpacing(8)
        idrow.setContentsMargins(0, 5, 0, 0)
        cs = C.lbl(m.node["callsign"], size=17, weight=700, color="#1c1e22")
        cs.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        addr = C.lbl(m.node["address"], size=11.5, weight=600, mono=True, color=a)
        addr.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        idrow.addWidget(cs)
        idrow.addWidget(addr)
        idrow.addStretch(1)
        nl.addLayout(idrow)
        nl.addWidget(C.lbl(m.node["station"], size=11, color=T.FG_DIM))
        self._lay.addWidget(node)

        mailv = m.mail_view()
        ft_active = str(m._ft_active_count())
        ft_badge_bg = "#a0a4aa" if ft_active == "0" else T.GREEN
        mv = m.modem_view()
        rs = m.radio_status()

        # ---- sections ----
        self._section("SECTIONS", [
            self._nav("DB", 9, "Subnet Dashboard", "dashboard"),
            self._nav("MO", 9, "Traffic Monitor", "monitor"),
        ])
        self._section("SIS CLIENTS", [
            self._nav("CH", 9, "HFCHAT Orderwire", "chat", green_badge="2", trailing="5"),
            self._nav("@", 11, "HF Mail", "mail", green_badge=str(mailv["unread"]) if mailv["unread"] else None, trailing="3·4"),
            self._nav("IP", 9, "IP Client", "ipclient", trailing="9"),
            self._nav("FX", 9, "File Transfer", "filexfer", badge=(ft_active, ft_badge_bg), trailing="6·7"),
            self._nav("SK", 9, "Raw SIS Socket", "sissocket", trailing="all", trailing_size=9),
        ])
        self._section("RADIO", [
            self._nav("RF", 9, "Radio Control", "radio", status_dot=rs["dot"]),
        ])
        self._section("SETUP", [
            self._nav("ML", 9, "Modem Link", "modem", status_dot=mv["stat"]["dot"]),
            self._nav("CF", 9, "Configuration", "config"),
        ])

        self._lay.addStretch(1)

        # ---- SIS socket server card ----
        card = QFrame()
        C.scoped(card, "background:%s;border:1px solid %s;border-radius:6px;" % (T.SIDEBAR_CARD, T.SIDEBAR_CARD_BORDER))
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 11, 12, 11)
        cl.setSpacing(4)
        cl.addWidget(C.caption("SIS SOCKET SERVER", size=9.5, color="#898d93"))
        srv = QHBoxLayout()
        srv.setSpacing(7)
        srv.setContentsMargins(0, 2, 0, 0)
        srv.addWidget(C.dot(T.GREEN, 8))
        srv.addWidget(C.lbl("127.0.0.1:5066", size=11.5, weight=600, mono=True, color="#2a2d31"))
        srv.addStretch(1)
        cl.addLayout(srv)
        cl.addWidget(C.lbl("Listening · 5 of 16 clients bound", size=10.5, color=T.FG_DIM))
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(10, 10, 10, 10)
        wl.addWidget(card)
        self._lay.addWidget(wrap)

    def _section(self, title: str, items):
        holder = QWidget()
        v = QVBoxLayout(holder)
        v.setContentsMargins(10, 12 if title == "SECTIONS" else 10, 10, 4)
        v.setSpacing(2)
        cap = C.caption(title, size=9.5, color="#9a9ea4")
        cap.setContentsMargins(8, 0, 0, 6)
        v.addWidget(cap)
        for it in items:
            v.addWidget(it)
        self._lay.addWidget(holder)

    def _nav(self, icon: str, icon_size: int, label: str, screen: str, *,
             green_badge: Optional[str] = None, badge: Optional[tuple] = None,
             trailing: Optional[str] = None, trailing_size: int = 10,
             status_dot: Optional[str] = None) -> QWidget:
        active = self.model.screen == screen
        a = self.model.theme.accent
        bar = a if active else "transparent"
        bg = self.model.theme.accent_soft if active else "transparent"
        base = ("QFrame{background:%s;border:none;border-left:3px solid %s;border-radius:5px;}"
                % (bg, bar))
        row = C.ClickableFrame(hover_bg=T.SIDEBAR_HOVER, base_css=base)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(9, 8, 9, 8)
        lay.setSpacing(10)
        lay.addWidget(_icon(icon, icon_size, a if active else "#c9ccd1", "#ffffff" if active else "#5a5e64"))
        lay.addWidget(C.lbl(label, size=13, weight=700 if active else 500,
                            color="#1c1e22" if active else "#34373c"), 1)
        if green_badge is not None:
            lay.addWidget(C.lbl(green_badge, size=9.5, mono=True, weight=600, color="#fff",
                                bg=T.GREEN, radius=8, pad=(1, 6), align="c"))
        if badge is not None:
            lay.addWidget(C.lbl(badge[0], size=9.5, mono=True, weight=600, color="#fff",
                                bg=badge[1], radius=8, pad=(1, 6), align="c"))
        if trailing is not None:
            lay.addWidget(C.lbl(trailing, size=trailing_size, mono=True, color="#9a9ea4"))
        if status_dot is not None:
            lay.addWidget(C.dot(status_dot, 7))
        row.clicked.connect(lambda: self.model.set_screen(screen))
        return row
