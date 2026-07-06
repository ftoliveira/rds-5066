"""HFCHAT Orderwire (SAP 5) — operators, message thread, S-primitive feed."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from ... import theme as T
from .. import common as C
from .base import Screen

_FEED_QSS = ("QScrollArea{background:%s;border:none;}"
             "QScrollBar:vertical{background:transparent;width:9px;margin:0;}"
             "QScrollBar::handle:vertical{background:#c9ccd1;border-radius:4px;min-height:24px;}"
             "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")


def _avatar(init: str, bg: str, fg: str, size: int = 30, radius: int = 6, fsize: int = 11) -> QLabel:
    w = QLabel(init)
    w.setFixedSize(size, size)
    w.setAlignment(Qt.AlignmentFlag.AlignCenter)
    f = w.font()
    f.setFamilies(T.MONO_STACK)
    f.setPixelSize(fsize)
    f.setBold(True)
    w.setFont(f)
    w.setStyleSheet(f"QLabel{{background:{bg};color:{fg};border-radius:{radius}px;}}")
    return w


class ChatScreen(Screen):
    topics = {"chat"}
    scroll = False
    full_height = True

    def build(self, lay: QVBoxLayout) -> None:
        a = self.accent
        # ---- header ----
        badge = C.lbl("SAP 5", size=11, mono=True, weight=600, color="#fff", bg=a, radius=4, pad=(2, 8))
        head = C.page_header(
            "HFCHAT Orderwire",
            "Operator orderwire · ITA5 / ASCII · short coordination messaging",
            badges=[badge], right=C.status_text("S_BIND_ACCEPT · Rank 15"))
        head.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(head)

        # ---- body: operators | thread | feed ----
        body = C.scoped(QFrame(), "background:transparent;border:none;border-top:1px solid %s;" % T.SIDEBAR_DIV)
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addWidget(self._operators())
        bl.addWidget(self._thread(), 1)
        bl.addWidget(self._feed())
        lay.addWidget(body, 1)

    # ---- operators column ----
    def _operators(self) -> QWidget:
        col = C.scoped(QFrame(), "background:#fafafb;border:none;border-right:1px solid %s;" % T.SIDEBAR_DIV)
        col.setFixedWidth(232)
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        cap = C.caption("SUBNET OPERATORS", size=10, color=T.FG_FAINT)
        cap.setContentsMargins(14, 11, 14, 7)
        v.addWidget(cap)
        for o in self.model.operators():
            v.addWidget(self._operator_row(o))
        mc = C.caption("MULTICAST GROUPS", size=10, color=T.FG_FAINT)
        mc.setContentsMargins(14, 11, 14, 7)
        top = C.scoped(QFrame(), "background:transparent;border:none;border-top:1px solid %s;" % T.HAIRLINE)
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 6, 0, 0)
        tv.addWidget(mc)
        v.addWidget(top)
        grp = QWidget()
        gl = QHBoxLayout(grp)
        gl.setContentsMargins(14, 0, 14, 9)
        gl.setSpacing(10)
        gl.addWidget(_avatar("⁂", "#e2e9f1", self.accent, fsize=13))
        meta = C.col(C.lbl("NET-ALL", size=12.5, weight=600, color="#25282c"),
                     C.lbl("239.000.000.001", size=10.5, mono=True, color=T.FG_FAINT), spacing=0)
        gl.addWidget(meta, 1)
        v.addWidget(grp)
        v.addStretch(1)
        return col

    def _operator_row(self, o) -> C.ClickableFrame:
        base = "QFrame{background:%s;border:none;border-left:3px solid %s;}" % (o["row_bg"], o["bar"])
        row = C.ClickableFrame(hover_bg="#eef0f2", base_css=base)
        l = QHBoxLayout(row)
        l.setContentsMargins(14, 9, 14, 9)
        l.setSpacing(10)
        l.addWidget(_avatar(o["init"], o["av_bg"], o["av_fg"]))
        meta = C.col(C.lbl(o["call"], size=12.5, weight=600, color="#25282c"),
                     C.lbl(o["addr"], size=10.5, mono=True, color=T.FG_FAINT), spacing=0)
        l.addWidget(meta, 1)
        l.addWidget(C.dot(o["dot"], 8))
        return row

    # ---- thread column ----
    def _thread(self) -> QWidget:
        a = self.accent
        col = QWidget()
        col.setStyleSheet(f"background:{T.CONTENT_BG};")
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # thread header
        hdr = C.scoped(QFrame(), "background:#ffffff;border:none;border-bottom:1px solid %s;" % T.SIDEBAR_DIV)
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(16, 9, 16, 9)
        hl.setSpacing(10)
        hl.addWidget(_avatar("CR", a, "#fff", size=32, radius=7, fsize=12))
        meta = C.col(C.lbl("CORVUS-06", size=13.5, weight=600, color="#25282c"),
                     C.lbl("3.066.000.006 · Point-to-point · ARQ / NODE DELIVERY", size=10.5, mono=True, color=T.FG_FAINT),
                     spacing=0)
        hl.addWidget(meta, 1)
        hl.addWidget(C.lbl("IN-ORDER", size=10, mono=True, color=T.GREEN_DARK, bg=T.GREEN_BG, radius=3, pad=(3, 8)))
        v.addWidget(hdr)

        # messages
        msgs = QWidget()
        ml = QVBoxLayout(msgs)
        ml.setContentsMargins(18, 16, 18, 16)
        ml.setSpacing(12)
        for m in self.model.chat_messages():
            ml.addWidget(self._bubble(m))
        ml.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setStyleSheet(_FEED_QSS % T.CONTENT_BG)
        area.setWidget(msgs)
        v.addWidget(area, 1)
        self._msg_area = area
        QTimer.singleShot(0, lambda: area.verticalScrollBar().setValue(area.verticalScrollBar().maximum()))

        v.addWidget(self._composer())
        return col

    def _bubble(self, m) -> QWidget:
        right = m["align"] == "r"
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(3)
        bubble = C.scoped(QFrame(), "background:%s;border:1px solid %s;border-radius:9px;"
                          % (m["bubble_bg"], m["bubble_border"]))
        bubble.setMaximumWidth(560)
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(3)
        head = C.row(C.lbl(m["from"], size=10.5, mono=True, weight=600, color=m["name_color"]),
                     C.lbl(m["addr"], size=10, mono=True, color=T.FG_GHOST2), None, spacing=8)
        bl.addWidget(head)
        text = C.lbl(m["text"], size=13, color="#25282c")
        text.setWordWrap(True)
        bl.addWidget(text)
        meta = C.row(C.lbl(m["time"], size=10, mono=True, color=T.FG_GHOST2),
                     C.lbl(m["conf"], size=10, mono=True, color=m["conf_color"]), None, spacing=6)
        meta.setContentsMargins(3, 0, 3, 0)
        # Align by stretch rows (not an alignment flag) so the wrapped label keeps
        # its height-for-width and the bubble grows to fit multi-line text.
        for widget in (bubble, meta):
            r = QHBoxLayout()
            r.setContentsMargins(0, 0, 0, 0)
            if right:
                r.addStretch(1)
                r.addWidget(widget)
            else:
                r.addWidget(widget)
                r.addStretch(1)
            wl.addLayout(r)
        return wrap

    def _composer(self) -> QWidget:
        a = self.accent
        box = C.scoped(QFrame(), "background:#ffffff;border:none;border-top:1px solid %s;" % T.SIDEBAR_DIV)
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 11, 14, 11)
        v.setSpacing(8)
        addr = QHBoxLayout()
        addr.setSpacing(8)
        addr.addWidget(C.lbl("ADDRESSING", size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.3))
        addr.addWidget(C.lbl("Point-to-point", size=11, weight=600, color="#fff", bg=a, radius=11, pad=(3, 9)))
        addr.addWidget(C.lbl("Multicast", size=11, color=T.FG_MUTED, border=T.SIDEBAR_DIV, radius=11, pad=(3, 9)))
        addr.addStretch(1)
        self._counter = C.lbl("%d / 1023 octets" % len(self.model.draft), size=10.5, mono=True, color=T.FG_GHOST2)
        addr.addWidget(self._counter)
        v.addLayout(addr)

        inp = QHBoxLayout()
        inp.setSpacing(9)
        edit = C.line_edit(self.model.draft, placeholder="Type orderwire message (CR/LF appended automatically)…",
                           mono=False, accent=a, on_change=self._on_draft, on_return=self.model.send_msg)
        inp.addWidget(edit, 1)
        inp.addWidget(C.button("Send", kind="primary", accent=a, on_click=self.model.send_msg))
        v.addLayout(inp)
        return box

    def _on_draft(self, text: str):
        self.model.set_draft(text)
        if hasattr(self, "_counter") and self._counter is not None:
            self._counter.setText("%d / 1023 octets" % len(text))

    # ---- primitive feed column ----
    def _feed(self) -> QWidget:
        col = C.scoped(QFrame(), "background:#fafafb;border:none;border-left:1px solid %s;" % T.SIDEBAR_DIV)
        col.setFixedWidth(270)
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        cap = C.caption("SIS PRIMITIVE FEED", size=10, color=T.FG_FAINT)
        cap.setContentsMargins(14, 11, 14, 7)
        v.addWidget(cap)
        for p in self.model.chat_prims():
            row = C.scoped(QFrame(), "background:transparent;border:none;border-bottom:1px solid %s;" % T.ROW_DIV)
            rl = QVBoxLayout(row)
            rl.setContentsMargins(14, 8, 14, 8)
            rl.setSpacing(2)
            top = C.row(C.lbl(p["name"], size=11, mono=True, weight=600, color=p["color"]),
                        C.lbl(p["time"], size=10, mono=True, color=T.FG_GHOST2), spacing=8)
            top.layout().setStretch(0, 1)
            rl.addWidget(top)
            rl.addWidget(C.lbl(p["detail"], size=10.5, mono=True, color=T.FG_DIM))
            v.addWidget(row)
        v.addStretch(1)
        return col
