"""Subnet Dashboard — links, channel quality, SAP binds, queues, primitives."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


def _sap_badge(sap: str, bg: str) -> QWidget:
    w = C.lbl(sap, size=13, weight=700, mono=True, color="#fff", bg=bg, radius=4, align="c")
    w.setFixedSize(26, 24)
    return w


class DashboardScreen(Screen):
    topics = {"dashboard"}  # live: rebuilds on status/event; demo: only accent

    def build(self, lay: QVBoxLayout) -> None:
        m = self.model
        refresh = C.lbl("Auto-refresh · 2s", size=11, mono=True, color=T.FG_DIM,
                        bg="#ffffff", radius=4, pad=(6, 10), border=T.SIDEBAR_DIV)
        lay.addWidget(C.page_header(
            "Subnet Dashboard",
            "Real-time state of the HF subnetwork interface sublayer and active links",
            right=refresh))
        lay.addWidget(C.kpi_strip(m.dashboard_kpis(), 4))

        # ---- links + channel quality ----
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        r1.addWidget(self._links_card(), 1)
        r1.addWidget(self._quality_card())
        lay.addLayout(r1)

        # ---- bound SAPs + (queues / primitives) ----
        r2 = QHBoxLayout()
        r2.setSpacing(12)
        r2.addWidget(self._saps_card(), 1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._queues_card())
        right.addWidget(self._prims_card(), 1)
        rw = QWidget()
        rw.setLayout(right)
        r2.addWidget(rw, 1)
        lay.addLayout(r2)

    def _links_card(self) -> C.Card:
        links = self.model.links()
        card = C.Card("Active Links & Peers",
                      right=C.lbl(f"{len(links)} node{'s' if len(links) != 1 else ''}",
                                  size=11, mono=True, color=T.FG_DIM))
        tbl = C.Table([1.4, 1, 0.9, 0.9, 0.9, 1])
        tbl.header(["Peer", "Address", "Link", "SNR", "Rate", "Uptime"])
        for l in links:
            peer = C.row(C.dot(l["dot"], 7),
                         C.lbl(l["peer"], size=12.5, weight=600, color="#25282c"), spacing=8)
            tbl.add([
                peer,
                C.lbl(l["address"], mono=True, size=11.5, color=T.FG_MUTED),
                C.pill(l["type"], l["type_fg"], l["type_bg"]),
                C.lbl(l["snr"], mono=True, color=l["snr_color"]),
                C.lbl(l["rate"], mono=True, color=T.FG_MUTED),
                C.lbl(l["uptime"], mono=True, size=11.5, color=T.FG_MUTED),
            ])
        card.add(tbl)
        return card

    def _quality_card(self) -> C.Card:
        meta = self.model.quality_meta()
        card = C.Card(meta["title"])
        card.setFixedWidth(320)
        body = QVBoxLayout()
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(13)
        for q in self.model.quality():
            body.addWidget(C.progress_row(q["label"], q["value"], q["pct"], q["color"]))
        body.addWidget(C.hline())
        grid = QGridLayout()
        grid.setContentsMargins(0, 3, 0, 0)
        grid.setHorizontalSpacing(10)
        for i, (k, v) in enumerate(meta["cells"]):
            cell = QVBoxLayout()
            cell.setSpacing(3)
            cell.addWidget(C.lbl(k, size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.3))
            cell.addWidget(C.lbl(v, size=13, mono=True, color="#25282c"))
            cw = QWidget()
            cw.setLayout(cell)
            grid.addWidget(cw, 0, i)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        body.addLayout(grid)
        card.add_layout(body)
        return card

    def _saps_card(self) -> C.Card:
        card = C.Card("Bound Service Access Points")
        for s in self.model.bound_saps():
            row = QHBoxLayout()
            row.setContentsMargins(14, 10, 14, 10)
            row.setSpacing(12)
            row.addWidget(_sap_badge(s["sap"], s["bg"]))
            meta = QVBoxLayout()
            meta.setSpacing(0)
            meta.addWidget(C.lbl(s["name"], size=12.5, weight=600, color="#25282c"))
            meta.addWidget(C.lbl("Rank %s · Pri %s · %s" % (s["rank"], s["pri"], s["mode"]),
                                 size=10.5, color=T.FG_FAINT))
            mw = QWidget()
            mw.setLayout(meta)
            row.addWidget(mw, 1)
            row.addWidget(C.lbl("BOUND", size=10, mono=True, weight=600, color=T.GREEN_DARK,
                                bg=T.GREEN_BG, radius=3, pad=(3, 7), align="c"))
            line = self._divider_row(row)
            card.add(line)
        return card

    def _queues_card(self) -> C.Card:
        card = C.Card("Transmit / Receive Queues")
        grid = QGridLayout()
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(20)
        for i, q in enumerate(self.model.queues()):
            cap, num, unit, sub = q["cap"], q["num"], q["unit"], q["sub"]
            cell = QVBoxLayout()
            cell.setSpacing(2)
            cell.addWidget(C.lbl(cap, size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.4))
            numrow = C.row(C.lbl(num, size=22, weight=600, mono=True, color=T.FG),
                           C.lbl(unit, size=11, color=T.FG_FAINT), None, spacing=5)
            cell.addWidget(numrow)
            cell.addWidget(C.lbl(sub, size=11, color=T.FG_DIM))
            cw = QWidget()
            cw.setLayout(cell)
            grid.addWidget(cw, 0, i)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        card.add_layout(grid)
        return card

    def _prims_card(self) -> C.Card:
        card = C.Card("Recent S-Primitives")
        for p in self.model.dash_prims():
            row = C.row(
                C.lbl(p["time"], size=11, mono=True, color=T.FG_GHOST),
                C.lbl(p["name"], size=11, mono=True, weight=600, color=p["color"]),
                C.lbl("SAP %s" % p["sap"], size=11, mono=True, color=T.FG_FAINT),
                None, spacing=9, margins=(14, 7, 14, 7),
            )
            card.add(self._wrap_divider(row))
        return card

    # small helpers to add a bottom hairline to a custom row layout
    def _divider_row(self, row_layout) -> QWidget:
        from PyQt6.QtWidgets import QFrame
        f = QFrame()
        C.scoped(f, "background:transparent;border:none;border-bottom:1px solid %s;" % T.ROW_DIV)
        f.setLayout(row_layout)
        return f

    def _wrap_divider(self, w: QWidget) -> QWidget:
        from PyQt6.QtWidgets import QFrame, QVBoxLayout as V
        f = QFrame()
        C.scoped(f, "background:transparent;border:none;border-bottom:1px solid %s;" % T.ROW_DIV)
        v = V(f)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(w)
        return f
