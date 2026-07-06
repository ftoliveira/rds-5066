"""File Transfer (RCOP SAP 6 / UDOP SAP 7) — composer, queue, primitive log."""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


class FileTransferScreen(Screen):
    topics = {"filexfer"}

    def build(self, lay: QVBoxLayout) -> None:
        m = self.model
        a = self.accent
        badges = [C.lbl("RCOP · SAP 6", size=11, mono=True, weight=600, color="#fff", bg=a, radius=4, pad=(2, 8)),
                  C.lbl("UDOP · SAP 7", size=11, mono=True, weight=600, color="#fff", bg=a, radius=4, pad=(2, 8))]
        lay.addWidget(C.page_header(
            "File Transfer",
            "Block-oriented file delivery over the SIS — reliable connection-oriented (RCOP) "
            "& unreliable datagram (UDOP)",
            badges=badges, right=C.status_text("BOUND · S_BIND_ACCEPT")))
        lay.addWidget(C.kpi_strip(m.ft_kpis(), 4))

        r1 = QHBoxLayout()
        r1.setSpacing(12)
        r1.addWidget(self._composer(), 1)
        r1.addWidget(self._proto_detail())
        lay.addLayout(r1)

        lay.addWidget(self._queue_card())
        lay.addWidget(self._log_card())

    # ---- composer ----
    def _composer(self) -> C.Card:
        m = self.model
        a = self.accent
        card = C.Card("Send File").pad(14, 14, 14, 14)

        # protocol toggle
        proto = QWidget()
        pl = QHBoxLayout(proto)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(10)
        pl.addWidget(C.lbl("PROTOCOL", size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.4))
        seg = C.scoped(QFrame(), "background:%s;border:1px solid %s;border-radius:5px;" % (T.CARD_HEADER_BG, T.CARD_BORDER))
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(2, 2, 2, 2)
        sl.setSpacing(2)
        sl.addWidget(self._proto_btn("RCOP", "reliable · ARQ", m.ft_is_rcop))
        sl.addWidget(self._proto_btn("UDOP", "datagram · non-ARQ", not m.ft_is_rcop))
        pl.addWidget(seg)
        pl.addStretch(1)
        card.add(proto)

        # destination
        dst = QWidget()
        dl = QHBoxLayout(dst)
        dl.setContentsMargins(0, 12, 0, 0)
        dl.setSpacing(10)
        dl.addWidget(self._field_label("DESTINATION"))
        combo = C.combo([(d["addr"], d["label"]) for d in m.ft_dests()], m.ft_dest,
                        accent=a, on_change=m.set_ft_dest)
        dl.addWidget(combo, 1)
        card.add(dst)

        # delivery + priority
        dp = QWidget()
        dpl = QHBoxLayout(dp)
        dpl.setContentsMargins(0, 12, 0, 2)
        dpl.setSpacing(10)
        dpl.addWidget(self._field_label("DELIVERY"))
        mode = m.ft_delivery_mode()
        dpl.addWidget(C.lbl(mode["label"], size=11, mono=True, weight=600, color=mode["fg"],
                            bg=mode["bg"], radius=4, pad=(4, 9)))
        dpl.addStretch(1)
        dpl.addWidget(C.lbl("PRIORITY", size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.4))
        for p in m.ft_prios():
            dpl.addWidget(self._prio_btn(p))
        card.add(dp)

        # drop zone
        drop = C.ClickableFrame(
            hover_bg=None,
            base_css="QFrame{background:#f7f8fa;border:1px dashed %s;border-radius:6px;}" % T.INPUT_BORDER)
        drl = QVBoxLayout(drop)
        drl.setContentsMargins(22, 22, 22, 22)
        drl.setSpacing(6)
        drl.addWidget(C.lbl("+ SELECT FILES", size=11, mono=True, weight=600, color=a, align="c"))
        drl.addWidget(C.lbl("or drop here · segmented into U-PDUs at the current data rate",
                            size=11, color=T.FG_FAINT, align="c"))
        drop.clicked.connect(self._pick_files)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 14, 0, 0)
        wl.addWidget(drop)
        card.add(wrap)

        # staged files
        staged = m.ft_staged_view()
        if staged:
            box = C.scoped(QFrame(), "background:transparent;border:1px solid %s;border-radius:6px;" % T.HAIRLINE)
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(0)
            for f in staged:
                bl.addWidget(self._staged_row(f))
            sw = QWidget()
            swl = QVBoxLayout(sw)
            swl.setContentsMargins(0, 12, 0, 0)
            swl.addWidget(box)
            card.add(sw)

        # actions
        ok = bool(staged)
        act = QWidget()
        al = QHBoxLayout(act)
        al.setContentsMargins(0, 14, 0, 0)
        al.setSpacing(10)
        btn = C.button("QUEUE TRANSFER", kind="primary" if ok else "default", accent=a,
                       on_click=m.send_ft if ok else None)
        if not ok:
            btn.setEnabled(False)
        al.addWidget(btn)
        al.addWidget(C.lbl(m.ft_staged_summary(), size=11.5, color=T.FG_DIM))
        al.addStretch(1)
        card.add(act)
        return card

    def _proto_detail(self) -> C.Card:
        title, desc = self.model.ft_proto_desc()
        card = C.Card(title)
        card.setFixedWidth(320)
        d = C.lbl(desc, size=12, color="#4a4d52")
        d.setWordWrap(True)
        pad = QWidget()
        pl = QVBoxLayout(pad)
        pl.setContentsMargins(14, 12, 14, 12)
        pl.addWidget(d)
        card.add(pad)
        tbl = C.Table([1, 1], aligns=["l", "r"])
        for b in self.model.ft_proto_params():
            tbl.add([C.lbl(b["k"], size=10, weight=600, color=T.FG_FAINT, upper=True, letter_spacing=0.4),
                     C.lbl(b["v"], size=12, mono=True, color="#25282c", align="r")],
                    divider=T.ROW_DIV)
        card.add(tbl)
        return card

    def _queue_card(self) -> C.Card:
        card = C.Card("Transfer Queue",
                      right=C.lbl("%d jobs" % len(self.model.ft_queue), size=11, mono=True, color=T.FG_DIM))
        tbl = C.Table([1.6, 0.6, 1.2, 0.7, 0.7, 1.4, 0.9])
        tbl.header(["File", "Proto", "Destination", "Size", "Pri", "Progress", "State"])
        for j in self.model.ft_queue_view():
            fname = C.row(
                C.lbl(j["ext"], size=9.5, mono=True, weight=700, color=T.FG_MUTED, bg="#eceef1", radius=3, pad=(2, 5)),
                C.lbl(j["name"], size=12.5, weight=600, color="#25282c"), None, spacing=8)
            tbl.add([
                fname,
                C.lbl(j["proto"], size=10, mono=True, weight=700, color=j["proto_fg"], bg=j["proto_bg"], radius=3, pad=(2, 6), align="c"),
                C.lbl(j["dest"], size=11.5, mono=True, color=T.FG_MUTED),
                C.lbl(j["size"], mono=True, color=T.FG_MUTED),
                C.lbl(str(j["pri"]), mono=True, weight=600, color=self.accent),
                self._progress_cell(j["pct"], j["bar_color"]),
                C.lbl(j["st"], size=10, mono=True, weight=600, color=j["st_fg"], bg=j["st_bg"], radius=3, pad=(2, 6), align="c"),
            ])
        card.add(tbl)
        return card

    def _log_card(self) -> C.Card:
        live = C.lbl("● LIVE", size=10, mono=True, color=T.GREEN_DARK, bg=T.GREEN_BG, radius=3, pad=(2, 8))
        card = C.Card("RCOP / UDOP Primitive Log", right=live)
        tbl = C.Table([1.1, 0.8, 2.2, 0.7, 0.9], scroll_height=200)
        tbl.header(["Time (UTC)", "Dir", "Primitive", "Proto", "Detail"])
        for e in self.model.ft_log():
            tbl.add([
                C.lbl(e["time"], size=11, mono=True, color=T.FG_GHOST),
                C.lbl(e["dir"], size=11, mono=True, weight=600, color=e["dir_fg"]),
                C.lbl(e["name"], size=11, mono=True, weight=600, color=e["color"]),
                C.lbl(e["proto"], size=11, mono=True, color=T.FG_MUTED),
                C.lbl(e["detail"], size=11, mono=True, color=T.FG_MUTED),
            ], divider=T.ROW_DIV_FAINT)
        tbl.finish()
        card.add(tbl)
        return card

    # ---- parts ----
    def _field_label(self, text) -> QWidget:
        w = C.lbl(text, size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.4)
        w.setFixedWidth(92)
        return w

    def _proto_btn(self, name, sub, active) -> C.ClickableFrame:
        a = self.accent
        bg = "#ffffff" if active else "transparent"
        fg = "#1c1e22" if active else T.FG_DIM
        subfg = a if active else T.FG_GHOST2
        btn = C.ClickableFrame(base_css="QFrame{background:%s;border:none;border-radius:4px;}" % bg)
        bl = QVBoxLayout(btn)
        bl.setContentsMargins(14, 5, 14, 5)
        bl.setSpacing(1)
        bl.addWidget(C.lbl(name, size=12, mono=True, weight=700, color=fg))
        bl.addWidget(C.lbl(sub, size=9.5, color=subfg))
        btn.clicked.connect(lambda _=None, n=name: self.model.set_ft_proto(n))
        return btn

    def _prio_btn(self, p) -> C.ClickableFrame:
        on = self.model.ft_pri == p["n"]
        btn = C.ClickableFrame(base_css="QFrame{background:%s;border:1px solid %s;border-radius:4px;}"
                               % (p["bg"], p["border"]))
        btn.setFixedSize(26, 26)
        bl = QHBoxLayout(btn)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(C.lbl(str(p["n"]), size=12, mono=True, weight=600, color=p["fg"], align="c"), 1)
        btn.clicked.connect(lambda _=None, n=p["n"]: self.model.set_ft_pri(n))
        return btn

    def _staged_row(self, f) -> QWidget:
        line = C.scoped(QFrame(), "background:transparent;border:none;border-bottom:1px solid %s;" % T.ROW_DIV_FAINT)
        l = QHBoxLayout(line)
        l.setContentsMargins(12, 8, 12, 8)
        l.setSpacing(10)
        l.addWidget(C.lbl(f["ext"], size=10, mono=True, weight=700, color=T.FG_MUTED, bg="#eceef1", radius=3, pad=(3, 6)))
        l.addWidget(C.lbl(f["name"], size=12.5, color="#25282c"), 1)
        l.addWidget(C.lbl(f["size"], size=11, mono=True, color=T.FG_FAINT))
        x = C.lbl("×", size=14, mono=True, color=T.FG_GHOST2)
        rm = C.ClickableFrame(base_css="QFrame{background:transparent;border:none;}")
        rl = QHBoxLayout(rm)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(x)
        rm.clicked.connect(lambda _=None, sid=f["id"]: self.model.remove_staged(sid))
        l.addWidget(rm)
        return line

    def _progress_cell(self, pct, color) -> QWidget:
        from PyQt6.QtWidgets import QSizePolicy
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(8)
        l.addWidget(C.bar(pct, color, height=6, track="#e7e8eb", radius=3), 1)
        v = C.lbl(pct, size=10.5, mono=True, color=T.FG_FAINT, align="r")
        v.setFixedWidth(34)
        l.addWidget(v)
        return w

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select files to transfer")
        files = []
        for p in paths:
            try:
                files.append((os.path.basename(p), os.path.getsize(p)))
            except OSError:
                files.append((os.path.basename(p), 0))
        if files:
            self.model.stage_files(files)
