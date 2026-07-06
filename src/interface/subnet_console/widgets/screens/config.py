"""Configuration — SIS socket server + per-client bind service requirements."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


class ConfigScreen(Screen):
    topics = {"config"}

    def build(self, lay: QVBoxLayout) -> None:
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        col.addWidget(C.page_header(
            "Configuration",
            "SIS socket server, client binding defaults and subnetwork service requirements"))
        col.addWidget(self._sis_card())
        col.addWidget(self._binding_card())
        lay.addWidget(C.max_width(inner, 980))

    def _sis_card(self) -> C.Card:
        mand = C.lbl("MANDATORY", size=10, mono=True, weight=600, color="#fff", bg="#5a5e64",
                     radius=3, pad=(1, 6))
        card = C.Card("Raw SIS Socket Server", right=mand)
        grid = QGridLayout()
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setHorizontalSpacing(16)
        for i, (k, v) in enumerate([("Bind Address", "127.0.0.1"), ("TCP Port", "5066"),
                                    ("Max Concurrent Clients", "16")]):
            grid.addWidget(C.read_field(k, v), 0, i)
            grid.setColumnStretch(i, 1)
        card.add_layout(grid)
        return card

    def _binding_card(self) -> C.Card:
        cv = self.model.config_view()
        a = self.accent
        card = C.Card()  # no header; tab strip acts as header

        # ---- tab strip ----
        tabs = C.ClickableFrame(
            base_css="QFrame{background:%s;border:none;border-bottom:1px solid %s;"
                     "border-top-left-radius:6px;border-top-right-radius:6px;}" % (T.CARD_HEADER_BG, T.CARD_BORDER),
            cursor=False)
        tl = QHBoxLayout(tabs)
        tl.setContentsMargins(6, 0, 6, 0)
        tl.setSpacing(0)
        tl.addWidget(self._tab("HFCHAT · SAP 5", cv["tab"] == "chat", "chat"))
        tl.addWidget(self._tab("IP Client · SAP 9", cv["tab"] == "ip", "ip"))
        tl.addStretch(1)
        card.add(tabs)

        # ---- form ----
        form = QWidget()
        fl = QVBoxLayout(form)
        fl.setContentsMargins(18, 18, 18, 18)
        fl.setSpacing(0)
        fl.addWidget(C.lbl(cv["title"], size=13, weight=600, color="#25282c"))
        sub = C.lbl(cv["subtitle"], size=11.5, color=T.FG_DIM)
        sub.setContentsMargins(0, 4, 0, 16)
        fl.addWidget(sub)

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(16)
        grid.addWidget(self._labeled("SAP ID", self._readbox(cv["sap"])), 0, 0)
        grid.addWidget(self._rank_field(cv), 0, 1)
        grid.addWidget(self._labeled("Transmission Mode", self._segment(cv["arq"])), 1, 0)
        grid.addWidget(self._labeled("Delivery Confirmation", self._deliv(cv["deliv"])), 1, 1)
        grid.addWidget(self._labeled("Deliver In Order", self._toggle_row("IN-ORDER DELIVERY")), 2, 0)
        grid.addWidget(self._labeled("Traffic Priority", self._prios(cv["prios"])), 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        fl.addLayout(grid)

        note = self._note(cv["note"])
        fl.addWidget(note)
        actions = C.row(None, C.button("Revert"), C.button("Apply && Rebind", kind="primary", accent=a), spacing=9)
        actions.setContentsMargins(0, 18, 0, 0)
        fl.addWidget(actions)
        card.add(form)
        return card

    # ---- parts ----
    def _tab(self, text, active, key) -> C.ClickableFrame:
        a = self.accent
        bar = a if active else "transparent"
        t = C.ClickableFrame(
            base_css="QFrame{background:transparent;border:none;border-bottom:2px solid %s;}" % bar)
        tl = QHBoxLayout(t)
        tl.setContentsMargins(16, 11, 16, 11)
        tl.addWidget(C.lbl(text, size=12.5, weight=700 if active else 500,
                           color="#1c1e22" if active else T.FG_DIM))
        t.clicked.connect(lambda: self.model.set_cfg_tab(key))
        return t

    def _labeled(self, label, widget) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(C.lbl(label, size=11, weight=600, color=T.FG_DIM))
        v.addWidget(widget)
        return w

    def _readbox(self, value, bg="#eef1f4") -> QWidget:
        f = C.ClickableFrame(base_css="QFrame{background:%s;border:1px solid %s;border-radius:5px;}"
                             % (bg, T.INPUT_BORDER), cursor=False)
        l = QHBoxLayout(f)
        l.setContentsMargins(11, 8, 11, 8)
        l.addWidget(C.lbl(value, size=13, mono=True, color="#25282c"))
        l.addStretch(1)
        return f

    def _rank_field(self, cv) -> QWidget:
        a = self.accent
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        head = C.row(C.lbl("Client Rank", size=11, weight=600, color=T.FG_DIM), None,
                     C.lbl(str(cv["rank"]), size=11, mono=True, weight=600, color=a))
        v.addWidget(head)
        v.addWidget(self._knob_slider(float(cv["rank_pct"].rstrip("%"))))
        return w

    def _knob_slider(self, pct: float) -> QWidget:
        a = self.accent
        w = QWidget()
        w.setFixedHeight(16)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        fillbar = C.scoped(QFrame(), f"background:{a};border-radius:3px;")
        fillbar.setFixedHeight(6)
        trackbar = C.scoped(QFrame(), "background:#e7e8eb;border-radius:3px;")
        trackbar.setFixedHeight(6)
        knob = C.scoped(QFrame(), f"background:#ffffff;border:2px solid {a};border-radius:7px;")
        knob.setFixedSize(14, 14)
        lay.addWidget(fillbar, max(1, round(pct * 10)), Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(knob, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(trackbar, max(1, round((100 - pct) * 10)), Qt.AlignmentFlag.AlignVCenter)
        return w

    def _segment(self, arq: bool) -> QWidget:
        a = self.accent
        f = C.ClickableFrame(base_css="QFrame{background:transparent;border:1px solid %s;border-radius:5px;}"
                             % T.INPUT_BORDER, cursor=False)
        l = QHBoxLayout(f)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)
        left = C.lbl("ARQ", size=12, weight=600, color="#fff" if arq else T.FG_DIM,
                     bg=a if arq else "#fff", align="c")
        left.setContentsMargins(0, 8, 0, 8)
        right = C.lbl("Non-ARQ", size=12, weight=500 if arq else 600, color=T.FG_DIM if arq else "#fff",
                      bg="#fff" if arq else a, align="c")
        right.setContentsMargins(0, 8, 0, 8)
        l.addWidget(left, 1)
        l.addWidget(C.vline(T.INPUT_BORDER, 34))
        l.addWidget(right, 1)
        return f

    def _deliv(self, value) -> QWidget:
        f = C.ClickableFrame(base_css="QFrame{background:%s;border:1px solid %s;border-radius:5px;}"
                             % (T.INPUT_BG, T.INPUT_BORDER), cursor=False)
        l = QHBoxLayout(f)
        l.setContentsMargins(11, 8, 11, 8)
        l.addWidget(C.lbl(value, size=12.5, color="#25282c"))
        l.addStretch(1)
        l.addWidget(C.lbl("▼", size=10, color=T.FG_GHOST2))
        return f

    def _toggle_row(self, label) -> QWidget:
        return C.row(self._toggle(True), C.lbl(label, size=12.5, color="#25282c"), None, spacing=9)

    def _toggle(self, on: bool) -> QWidget:
        a = self.accent
        track = C.scoped(QFrame(), f"background:{a if on else '#c4c6cb'};border-radius:10px;")
        track.setFixedSize(38, 21)
        l = QHBoxLayout(track)
        l.setContentsMargins(2, 2, 2, 2)
        knob = C.scoped(QFrame(), "background:#ffffff;border-radius:8px;")
        knob.setFixedSize(17, 17)
        if on:
            l.addStretch(1)
        l.addWidget(knob)
        if not on:
            l.addStretch(1)
        return track

    def _prios(self, prios) -> QWidget:
        a = self.accent
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(5)
        for p in prios:
            on = p["on"]
            chip = C.lbl(str(p["n"]), size=11, mono=True, weight=600,
                         color="#fff" if on else T.FG_DIM, bg=a if on else "#fff",
                         border=a if on else T.SIDEBAR_DIV, radius=4, pad=(6, 0), align="c")
            l.addWidget(chip, 1)
        return w

    def _note(self, text) -> QWidget:
        a = self.accent
        note = C.ClickableFrame(
            base_css="QFrame{background:%s;border:1px solid %s;border-radius:6px;}"
                     % (self.model.theme.accent_note_bg, self.model.theme.accent_note_border), cursor=False)
        nl = QHBoxLayout(note)
        nl.setContentsMargins(14, 12, 14, 12)
        nl.setSpacing(10)
        icon = C.lbl("i", size=11, mono=True, weight=700, color="#fff", bg=a, radius=4, align="c")
        icon.setFixedSize(18, 18)
        nl.addWidget(icon, 0)
        txt = C.lbl(text, size=11.5, color="#4a4d52")
        txt.setWordWrap(True)
        nl.addWidget(txt, 1)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 18, 0, 0)
        wl.addWidget(note)
        return wrap
