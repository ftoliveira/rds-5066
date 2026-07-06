"""Traffic Monitor — counters, SAP allocation (Table F-1), S-primitive log."""
from __future__ import annotations

from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


class MonitorScreen(Screen):
    topics = {"monitor"}   # live: rebuilds on status/event; demo: only accent

    def build(self, lay: QVBoxLayout) -> None:
        capturing = C.lbl("● CAPTURING", size=11, mono=True, color=T.GREEN_DARK,
                          bg=T.GREEN_BG, radius=4, pad=(6, 10), border=T.GREEN)
        actions = C.row(capturing, C.button("Export PCAP", accent=self.accent), spacing=8)
        lay.addWidget(C.page_header(
            "Traffic Monitor",
            "Service Access Point allocation and live S-Primitive event log",
            right=actions))

        lay.addWidget(self._counters())
        lay.addWidget(self._sap_table())
        lay.addWidget(self._event_log())

    def _counters(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        for i, c in enumerate(self.model.counters()):
            card = QFrame()
            C.scoped(card, "background:%s;border:1px solid %s;border-radius:6px;" % (T.CARD_BG, T.CARD_BORDER))
            v = QVBoxLayout(card)
            v.setContentsMargins(13, 11, 13, 11)
            v.setSpacing(6)
            v.addWidget(C.caption(c["label"], size=10))
            v.addWidget(C.lbl(c["value"], size=21, weight=600, mono=True, color=c["color"]))
            grid.addWidget(card, 0, i)
            grid.setColumnStretch(i, 1)
        return w

    def _sap_table(self) -> C.Card:
        card = C.Card("SAP ID Allocation — Annex F Table F-1")
        tbl = C.Table([0, 2, 1, 0.7, 0.7, 1, 0.9, 0.9, 1], fixed={0: 46})
        tbl.header(["SAP", "Client / Application", "State", "Rank", "Pri", "Mode", "TX", "RX", "Last Activity"])
        for s in self.model.sap_table():
            name = C.row(
                C.lbl(s["name"], size=12.5, weight=s["name_weight"], color=s["name_color"]),
                C.lbl(s["tag"], size=9, mono=True, weight=600, color=s["tag_fg"], bg=s["tag_bg"],
                      radius=3, pad=(1, 5), align="c"),
                None, spacing=8)
            tbl.add([
                C.lbl(s["sap"], mono=True, weight=700, color=s["sap_color"]),
                name,
                C.lbl(s["state"], size=10, mono=True, weight=600, color=s["state_fg"]),
                C.lbl(str(s["rank"]), mono=True, color=T.FG_MUTED),
                C.lbl(str(s["pri"]), mono=True, color=T.FG_MUTED),
                C.lbl(s["mode"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(s["tx"], mono=True, color=T.FG_MUTED),
                C.lbl(s["rx"], mono=True, color=T.FG_MUTED),
                C.lbl(s["last"], size=11, mono=True, color=T.FG_FAINT),
            ], bg=s["row_bg"])
        card.add(tbl)
        return card

    def _event_log(self) -> C.Card:
        filters = C.row(
            C.lbl("ALL SAPs", size=10, mono=True, color=T.FG_MUTED, bg="#fff", radius=3,
                  pad=(2, 7), border=T.SIDEBAR_DIV),
            C.lbl("REQUEST + INDICATION", size=10, mono=True, color=T.FG_MUTED, bg="#fff", radius=3,
                  pad=(2, 7), border=T.SIDEBAR_DIV),
            spacing=6)
        card = C.Card("S-Primitive Event Log", right=filters)
        tbl = C.Table([1.1, 2, 0.6, 1.3, 1.3, 0.8, 1.1], scroll_height=224)
        tbl.header(["Time (UTC)", "Primitive", "SAP", "Source", "Destination", "Size", "Result"])
        for e in self.model.event_log():
            tbl.add([
                C.lbl(e["time"], size=11, mono=True, color=T.FG_GHOST),
                C.lbl(e["prim"], size=11, mono=True, weight=600, color=e["color"]),
                C.lbl(e["sap"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["src"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["dst"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["size"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["result"], size=11, mono=True, weight=600, color=e["res_color"]),
            ], divider=T.ROW_DIV_FAINT)
        tbl.finish()
        card.add(tbl)
        return card
