"""Raw SIS Socket Server (Annex F.16) — params, clients, wire log."""
from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout

from ... import theme as T
from .. import common as C
from .base import Screen


class SisSocketScreen(Screen):
    topics = set()

    def build(self, lay: QVBoxLayout) -> None:
        m = self.model
        badges = [C.lbl("ALL SAPs", size=10, mono=True, weight=600, color="#fff", bg="#5a5e64", radius=4, pad=(2, 7)),
                  C.lbl("MANDATORY", size=10, mono=True, weight=600, color="#fff", bg="#5a5e64", radius=4, pad=(2, 7))]
        lay.addWidget(C.page_header(
            "Raw SIS Socket Server",
            "TCP/IP socket-server providing the physical Subnetwork Interface Sublayer channel · Annex F.16",
            badges=badges, right=C.status_text("LISTENING · 127.0.0.1:5066")))
        lay.addWidget(C.kpi_strip(m.sk_kpis(), 4))

        r1 = QHBoxLayout()
        r1.setSpacing(12)
        r1.addWidget(C.kv_card("Server Parameters", m.sk_server(), width=320))
        r1.addWidget(self._clients_card(), 1)
        lay.addLayout(r1)

        lay.addWidget(self._wire_card())

    def _clients_card(self) -> C.Card:
        card = C.Card("Connected Clients",
                      right=C.lbl("5 sockets open", size=11, mono=True, color=T.FG_DIM))
        tbl = C.Table([0.5, 1.3, 1.6, 0.6, 0.6, 1, 1.2])
        tbl.header(["ID", "Remote Socket", "Client", "SAP", "Rank", "State", "Connected"])
        for c in self.model.sk_clients():
            sap = C.lbl(c["sap"], size=11, mono=True, weight=700, color="#fff", bg=c["sap_bg"],
                        radius=3, pad=(1, 7), align="c")
            tbl.add([
                C.lbl(c["id"], mono=True, color=T.FG_FAINT),
                C.lbl(c["remote"], size=11.5, mono=True, color=T.FG_MUTED),
                C.lbl(c["client"], size=12.5, weight=600, color=c["client_fg"]),
                sap,
                C.lbl(c["rank"], mono=True, color=T.FG_MUTED),
                C.lbl(c["st"], size=10, mono=True, weight=600, color=c["st_fg"], bg=c["st_bg"],
                      radius=3, pad=(2, 6), align="c"),
                C.lbl(c["since"], size=11, mono=True, color=T.FG_FAINT),
            ])
        card.add(tbl)
        return card

    def _wire_card(self) -> C.Card:
        live = C.lbl("● LIVE", size=10, mono=True, color=T.GREEN_DARK, bg=T.GREEN_BG, radius=3, pad=(2, 8))
        card = C.Card("SIS Wire Log", right=live)
        tbl = C.Table([1.2, 0.9, 2.4, 0.7, 0.8], scroll_height=248)
        tbl.header(["Time (UTC)", "Direction", "S-Primitive", "SAP", "Length"])
        for w in self.model.sk_wire():
            tbl.add([
                C.lbl(w["time"], size=11, mono=True, color=T.FG_GHOST),
                C.lbl(w["dir"], size=11, mono=True, weight=600, color=w["dir_fg"]),
                C.lbl(w["name"], size=11, mono=True, weight=600, color=w["color"]),
                C.lbl(w["sap"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(w["size"], size=11, mono=True, color=T.FG_MUTED),
            ], divider=T.ROW_DIV_FAINT)
        tbl.finish()
        card.add(tbl)
        return card
