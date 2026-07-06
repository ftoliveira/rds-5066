"""IP Client (SAP 9) — binding, QoS mapping, address routes, datagram log."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


def _mini_toggle(on: bool, accent: str) -> QWidget:
    track = C.scoped(QFrame(), f"background:{accent if on else '#c4c6cb'};border-radius:8px;")
    track.setFixedSize(30, 17)
    l = QHBoxLayout(track)
    l.setContentsMargins(2, 2, 2, 2)
    knob = C.scoped(QFrame(), "background:#ffffff;border-radius:6px;")
    knob.setFixedSize(13, 13)
    if on:
        l.addStretch(1)
    l.addWidget(knob)
    if not on:
        l.addStretch(1)
    return track


class IpClientScreen(Screen):
    topics = set()

    def build(self, lay: QVBoxLayout) -> None:
        m = self.model
        a = self.accent
        badges = [C.lbl("SAP 9", size=11, mono=True, weight=600, color="#fff", bg=a, radius=4, pad=(2, 8)),
                  C.lbl("MANDATORY", size=10, mono=True, weight=600, color="#fff", bg="#5a5e64", radius=4, pad=(2, 7))]
        lay.addWidget(C.page_header(
            "IP Client", "IPv4 datagram transport over the HF subnetwork · Annex F.12",
            badges=badges, right=C.status_text("BOUND · S_BIND_ACCEPT")))
        lay.addWidget(C.kpi_strip(m.ip_kpis(), 4))

        r1 = QHBoxLayout()
        r1.setSpacing(12)
        r1.addWidget(C.kv_card("Interface & Binding", m.ip_bind(), width=360))
        r1.addWidget(self._qos_card(), 1)
        lay.addLayout(r1)

        lay.addWidget(self._routes_card())
        lay.addWidget(self._log_card())

    def _qos_card(self) -> C.Card:
        a = self.accent
        right = C.row(C.lbl("QoS MANAGEMENT", size=11, color=T.GREEN_DARK), _mini_toggle(True, a), spacing=7)
        card = C.Card("Quality-of-Service Mapping", right=right)
        tbl = C.Table([1.6, 0.8, 0.7, 1, 0.7])
        tbl.header(["IP QoS Label", "DSCP", "Priority", "Mode", "State"])
        for q in self.model.ip_qos():
            dim = q["row_op"] < 1.0
            label_fg = T.FG_GHOST2 if dim else "#25282c"
            tbl.add([
                C.lbl(q["label"], size=12.5, weight=600, color=label_fg),
                C.lbl(q["dscp"], mono=True, color=T.FG_MUTED),
                C.lbl(q["prio"], mono=True, weight=600, color=a if not dim else T.FG_GHOST2),
                C.lbl(q["mode"], size=11, mono=True, color=T.FG_MUTED),
                _mini_toggle(q["on"], a),
            ])
        card.add(tbl)
        return card

    def _routes_card(self) -> C.Card:
        note = C.lbl("· unicast → ARQ · multicast → non-ARQ", size=11, color=T.FG_FAINT)
        card = C.Card("IP → STANAG 5066 Address Mapping", right=note)
        tbl = C.Table([1.2, 1.2, 1, 1.2, 0.8])
        tbl.header(["IP Prefix", "Node Address", "Tx Mode", "Peer / Group", "Link"])
        for r in self.model.ip_routes():
            link = C.row(C.dot(r["dot"], 7),
                         C.lbl(r["st"], size=10, mono=True, weight=600, color=r["st_fg"]), spacing=7)
            tbl.add([
                C.lbl(r["cidr"], mono=True, weight=600, color="#25282c"),
                C.lbl(r["node"], mono=True, color=T.FG_MUTED),
                C.pill(r["mode"], r["mode_fg"], r["mode_bg"]),
                C.lbl(r["links"], size=12.5, color=T.FG_MUTED),
                link,
            ])
        card.add(tbl)
        return card

    def _log_card(self) -> C.Card:
        live = C.lbl("● LIVE", size=10, mono=True, color=T.GREEN_DARK, bg=T.GREEN_BG, radius=3, pad=(2, 8))
        card = C.Card("IP Datagram Log", right=live)
        tbl = C.Table([1.1, 1.2, 1.2, 0.7, 0.8, 0.9, 1], scroll_height=220)
        tbl.header(["Time (UTC)", "Source", "Destination", "Proto", "Length", "Mode", "Result"])
        for e in self.model.ip_log():
            tbl.add([
                C.lbl(e["time"], size=11, mono=True, color=T.FG_GHOST),
                C.lbl(e["src"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["dst"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["proto"], size=11, mono=True, weight=600, color=e["proto_color"]),
                C.lbl(e["len"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["mode"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["result"], size=11, mono=True, weight=600, color=e["res_color"]),
            ], divider=T.ROW_DIV_FAINT)
        tbl.finish()
        card.add(tbl)
        return card
