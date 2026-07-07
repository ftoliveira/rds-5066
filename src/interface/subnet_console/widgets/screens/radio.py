"""Radio Control — ALE 2G station remote control (docs/PROTOCOLO-CONTROLE-REMOTO.md).

Displays the radio's live telemetry (frequency, TX power, VSWR, SINAD/BER/RSSI,
FSM/link, scanning) and the CHANNELS / SCAN / LQA / SOUND / AMD / LOG tables, and
sends the full operator command set (CALL / GROUP / NET / TERM / SOUND / CONFIG /
FORCE_LINK / CHEDIT / AMD).

Repaint discipline (see ``ConsoleModel``): the screen fully rebuilds on the
``"radio"`` topic (config/mode/link/channel-table changes — infrequent) and
refreshes only its live-display holders on ``"radio_tele"`` (STATE ~5 Hz, scan,
LQA, log, AMD). Text inputs and the click-to-set control chips live in the
structural part, so streaming telemetry never yanks focus or closes a control.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


class RadioScreen(Screen):
    topics = {"radio"}

    def __init__(self, model):
        self._tele_fillers = []   # (layout, filler(layout, rv)) — refreshed on radio_tele
        super().__init__(model)

    def _on_changed(self, topic: str) -> None:
        if topic == "radio":
            self.rebuild()
        elif topic == "radio_tele":
            self._refresh_tele()

    def _refresh_tele(self) -> None:
        rv = self.model.radio_view()
        for lay, filler in self._tele_fillers:
            C.clear_layout(lay)
            filler(lay, rv)

    def _tele(self, rv, filler, *, spacing: int = 12) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(spacing)
        self._tele_fillers.append((lay, filler))
        filler(lay, rv)
        return holder

    # ------------------------------------------------------------------ build
    def build(self, lay: QVBoxLayout) -> None:
        self._tele_fillers = []
        rv = self.model.radio_view()
        a = self.accent
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        badge = C.lbl("ALE 2G · MIL-STD-188-141B", size=11, mono=True, weight=600,
                      color="#fff", bg=a, radius=4, pad=(2, 8))
        col.addWidget(C.page_header(
            "Radio Control",
            "Remote control & telemetry · UDP :54001 · frequency / power / scanning / links",
            badges=[badge], right=self._status_pill(rv)))

        # live telemetry (refreshes on radio_tele, no rebuild)
        col.addWidget(self._tele(rv, self._fill_kpis))
        col.addWidget(self._tele(rv, self._fill_readouts))

        # structural controls
        col.addWidget(self._controls_card(rv))
        col.addWidget(self._link_card(rv))
        col.addWidget(self._amd_card(rv))
        col.addWidget(self._channels_card(rv))

        # live tables (refresh on radio_tele)
        col.addWidget(self._tele(rv, self._fill_lqa))
        col.addWidget(self._tele(rv, self._fill_sound))
        col.addWidget(self._tele(rv, self._fill_log))

        lay.addWidget(C.max_width(inner, 1040))

    # ------------------------------------------------------------------ header
    def _status_pill(self, rv) -> QWidget:
        s = rv["status"]
        pill = C.ClickableFrame(
            base_css="QFrame{background:%s;border:1px solid %s;border-radius:20px;}" % (s["bg"], s["border"]),
            cursor=False)
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(13, 6, 13, 6)
        pl.setSpacing(7)
        pl.addWidget(C.dot(s["dot"], 8))
        pl.addWidget(C.lbl(s["label"], size=11, mono=True, weight=600, color=s["fg"]))
        return pill

    # ------------------------------------------------------------------ tele fills
    def _fill_kpis(self, lay: QVBoxLayout, rv) -> None:
        lay.addWidget(C.kpi_strip(rv["kpis"], cols=6, gap=10))

    def _fill_readouts(self, lay: QVBoxLayout, rv) -> None:
        card = C.Card().pad(14, 12, 14, 12)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        items = rv["readouts"]
        cols = 4
        for i, (k, v) in enumerate(items):
            grid.addWidget(self._readout(k, v), i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        card.add_layout(grid)
        lay.addWidget(card)

    def _fill_channels(self, lay: QVBoxLayout, rv) -> None:
        # header row
        head = self._chan_row(["#", "FREQUENCY", "NAME", "BAND", "OCC", "LQA"], header=True)
        lay.addWidget(head)
        for c in rv["channels"]:
            lay.addWidget(self._chan_row(c))

    def _fill_lqa(self, lay: QVBoxLayout, rv) -> None:
        card = C.Card("LQA Matrix · peer × channel").pad(0, 0, 0, 0)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 12, 14, 12)
        bl.setSpacing(0)
        labels = rv["lqa_labels"]
        peers = rv["lqa_peers"]
        if not peers:
            bl.addWidget(C.lbl("No LQA data yet.", size=12, color=T.FG_DIM))
        else:
            # header
            hdr = QHBoxLayout()
            hdr.setSpacing(6)
            hdr.addWidget(self._cell("PEER", 64, T.FG_FAINT, weight=600))
            for f in labels:
                hdr.addWidget(self._cell(f, 52, T.FG_FAINT, weight=600, align="c"))
            hdr.addStretch(1)
            bl.addLayout(hdr)
            bl.addWidget(C.hline())
            for p in peers:
                r = QHBoxLayout()
                r.setSpacing(6)
                r.setContentsMargins(0, 5, 0, 5)
                dotcol = T.GREEN if p["online"] else "#9aa0a6"
                addr = C.row(C.dot(dotcol, 7), C.lbl(p["addr"], size=12, mono=True, weight=600,
                                                     color=T.FG_BODY), spacing=6)
                addr.setFixedWidth(64)
                r.addWidget(addr)
                for cell in p["cells"]:
                    r.addWidget(self._cell(cell["txt"], 52, cell["color"], mono=True, align="c"))
                r.addStretch(1)
                bl.addLayout(r)
        card.add(body)
        lay.addWidget(card)

    def _fill_sound(self, lay: QVBoxLayout, rv) -> None:
        card = C.Card("Sounding History").pad(0, 0, 0, 0)
        tbl = C.Table([1, 1, 1, 2], aligns=["l", "c", "c", "r"], pad_h=14)
        tbl.header(["TIME", "CH", "QUALITY", "ACK BY"])
        rows = rv["sound_hist"]
        if not rows:
            card.add(tbl)
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(14, 10, 14, 12)
            wl.addWidget(C.lbl("No soundings recorded.", size=12, color=T.FG_DIM))
            card.add(wrap)
        else:
            for r in rows:
                tbl.add([
                    C.lbl(r["t"], size=12, mono=True, color=T.FG_MUTED),
                    C.lbl(str(r["ch"]) if r["ch"] >= 0 else "—", size=12, mono=True, color=T.FG_BODY, align="c"),
                    C.lbl(str(r["q"]), size=12, mono=True, weight=600, color=r["q_color"], align="c"),
                    C.lbl(r["ack"] or "—", size=12, mono=True, color=T.FG_BODY, align="r"),
                ])
            card.add(tbl)
        lay.addWidget(card)

    def _fill_log(self, lay: QVBoxLayout, rv) -> None:
        card = C.Card("Event Log").pad(0, 0, 0, 0)
        tbl = C.Table([1, 1, 6], fixed={0: 74, 1: 52}, aligns=["l", "l", "l"],
                      scroll_height=170, pad_h=14)
        tbl.header(["TIME", "KIND", "MESSAGE"])
        for e in rv["log"][:120]:
            tbl.add([
                C.lbl(e["t"], size=11.5, mono=True, color=T.FG_DIM),
                C.lbl(e["kind"], size=10.5, mono=True, weight=600, color=e["color"]),
                C.lbl(e["text"], size=11.5, mono=True, color=T.FG_BODY),
            ])
        tbl.finish()
        card.add(tbl)
        lay.addWidget(card)

    def _fill_amd_inbox(self, lay: QVBoxLayout, rv) -> None:
        msgs = rv["amd"]
        if not msgs:
            lay.addWidget(C.lbl("No AMD messages received.", size=12, color=T.FG_DIM))
            return
        for m in msgs[:12]:
            row = C.ClickableFrame(
                base_css="QFrame{background:%s;border:1px solid %s;border-radius:6px;}"
                         % (T.INPUT_BG, T.HAIRLINE), cursor=False)
            rl = QVBoxLayout(row)
            rl.setContentsMargins(11, 8, 11, 9)
            rl.setSpacing(3)
            top = C.row(
                C.lbl(m["from"], size=12, mono=True, weight=700, color=self.accent),
                C.lbl(m["t"], size=10.5, mono=True, color=T.FG_FAINT),
                None,
                C.lbl("UNREAD" if not m["read"] else "READ", size=9.5, mono=True, weight=600,
                      color="#fff" if not m["read"] else T.FG_DIM,
                      bg=T.AMBER if not m["read"] else "#e2e4e7", radius=3, pad=(1, 6)),
                spacing=8)
            rl.addWidget(top)
            body = C.lbl(m["text"], size=12, color=T.FG_BODY)
            body.setWordWrap(True)
            rl.addWidget(body)
            lay.addWidget(row)

    # ------------------------------------------------------------------ controls
    def _controls_card(self, rv) -> C.Card:
        a = self.accent
        card = C.Card("Radio Configuration").pad(18, 16, 18, 18)

        card.add(self._label("TX Power — Potência (RF_POWER)"))
        card.add(self._chip_row([(f"{s['n']} dBm", s["active"],
                                  (lambda n=s["n"]: self.model.ale_set_tx_power(n)))
                                 for s in rv["power_steps"]]))

        grid = QGridLayout()
        grid.setContentsMargins(0, 12, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)
        grid.addWidget(self._label("Sideband"), 0, 0)
        grid.addWidget(self._label("Scan Rate — Taxa de varrimento"), 0, 1)
        grid.addWidget(self._segment([(sb["name"], sb["active"],
                                       (lambda v=sb["v"]: self.model.ale_set_sideband(v)))
                                      for sb in rv["sidebands"]]), 1, 0)
        grid.addWidget(self._chip_row([(f"{r['n']}/s", r["active"],
                                        (lambda n=r["n"]: self.model.ale_set_scan_rate(n)))
                                       for r in rv["scan_rates"]]), 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        card.add_layout(grid)

        card.add(C.hline())
        # occupancy + Twa
        occ_row = C.row(
            self._toggle(rv["occupancy"], lambda: self.model.ale_set_occupancy(not rv["occupancy"])),
            C.lbl("Occupancy detect (LBT)", size=12.5, color="#25282c"),
            None,
            self._twa_field(rv),
            spacing=10)
        card.add(occ_row)

        card.add(C.hline())
        # forced / normal + sounding trigger
        force = C.row(
            C.lbl("Manual mode", size=11, weight=600, color=T.FG_DIM),
            self._segment([(s["v"].upper(), s["active"],
                            (lambda v=s["v"]: self.model.set_ale_force_service(v)))
                           for s in rv["force_services"]], compact=True),
            C.button("Park Channel", on_click=self.model.ale_park),
            C.button("Normal / Scan", on_click=self.model.ale_normal),
            None,
            spacing=9)
        card.add(force)

        sound = C.row(
            C.lbl("Sounding", size=11, weight=600, color=T.FG_DIM),
            self._segment([(m["name"], m["active"],
                            (lambda v=m["v"]: self.model.set_ale_sound_mode(v)))
                           for m in rv["sound_modes"]], compact=True),
            C.button("Sound Now", kind="primary", accent=a, on_click=self.model.ale_sound),
            None,
            spacing=9)
        sound.layout().setContentsMargins(0, 4, 0, 0)
        card.add(sound)
        return card

    def _link_card(self, rv) -> C.Card:
        a = self.accent
        card = C.Card("Link & Calling", right=self._link_badge(rv)).pad(18, 16, 18, 18)

        # readouts
        info = C.row(
            self._readout("SELF ADDRESS", rv["self_addr"]),
            self._readout("LINK PEER", rv["link_peer"] or "—"),
            self._readout("STATE", rv["fsm_name"] or "—"),
            self._readout("SERVICE", (rv["active_service"] or "—").upper()),
            spacing=24)
        card.add(info)
        card.add(C.hline())

        # individual call
        callrow = QGridLayout()
        callrow.setHorizontalSpacing(12)
        callrow.setVerticalSpacing(6)
        addr = C.line_edit(rv["call_addr"], placeholder="dest (e.g. BR2)", accent=a,
                           on_change=self.model.set_ale_call_addr,
                           on_return=self.model.ale_call)
        chan = C.line_edit(rv["call_channel"], placeholder="ch (auto)", accent=a,
                           on_change=self.model.set_ale_call_channel,
                           on_return=self.model.ale_call)
        callrow.addWidget(self._field("Individual Call — CALL", addr), 0, 0)
        callrow.addWidget(self._field("Channel", chan), 0, 1)
        btns = C.row(C.button("Call", kind="primary", accent=a, on_click=self.model.ale_call),
                     C.button("Terminate", kind="danger", on_click=self.model.ale_terminate),
                     spacing=8)
        callrow.addWidget(self._field(" ", btns), 0, 2)
        callrow.setColumnStretch(0, 3)
        callrow.setColumnStretch(1, 1)
        callrow.setColumnStretch(2, 2)
        card.add_layout(callrow)

        # group / net
        gn = QGridLayout()
        gn.setHorizontalSpacing(12)
        gn.setVerticalSpacing(6)
        grp = C.line_edit(rv["group_members"], placeholder="members: BR2 BR3 BR4", accent=a,
                          on_change=self.model.set_ale_group, on_return=self.model.ale_group_call)
        net = C.line_edit(rv["net_id"], placeholder="net id", accent=a,
                          on_change=self.model.set_ale_net, on_return=self.model.ale_net_call)
        gn.addWidget(self._field("Group Call — GROUP", grp), 0, 0)
        gn.addWidget(self._field(" ", C.button("Group Call", on_click=self.model.ale_group_call)), 0, 1)
        gn.addWidget(self._field("Net Call — NET", net), 0, 2)
        gn.addWidget(self._field(" ", C.button("Net Call", on_click=self.model.ale_net_call)), 0, 3)
        gn.setColumnStretch(0, 3)
        gn.setColumnStretch(1, 1)
        gn.setColumnStretch(2, 3)
        gn.setColumnStretch(3, 1)
        card.add_layout(gn)
        return card

    def _amd_card(self, rv) -> C.Card:
        a = self.accent
        card = C.Card("AMD — Automatic Message Display").pad(18, 16, 18, 18)
        compose = QGridLayout()
        compose.setHorizontalSpacing(12)
        compose.setVerticalSpacing(6)
        dest = C.line_edit(rv["amd_dest"], placeholder="dest (blank = current link)", accent=a,
                           on_change=self.model.set_ale_amd_dest)
        text = C.line_edit(rv["amd_text"], placeholder="short text (≤ 91 chars)", mono=False, accent=a,
                           on_change=self.model.set_ale_amd_text, on_return=self.model.ale_send_amd)
        compose.addWidget(self._field("Destination", dest), 0, 0)
        compose.addWidget(self._field("Message", text), 0, 1)
        compose.addWidget(self._field(" ", C.button("Send AMD", kind="primary", accent=a,
                                                    on_click=self.model.ale_send_amd)), 0, 2)
        compose.setColumnStretch(0, 2)
        compose.setColumnStretch(1, 4)
        compose.setColumnStretch(2, 1)
        card.add_layout(compose)
        card.add(C.hline())
        card.add(self._label("Received"))
        card.add(self._tele(rv, self._fill_amd_inbox, spacing=8))
        return card

    def _channels_card(self, rv) -> C.Card:
        a = self.accent
        card = C.Card("Channels · click a row to load into the forms").pad(14, 12, 14, 14)
        # live channel table (occ/LQA refresh on radio_tele)
        card.add(self._tele(rv, self._fill_channels, spacing=0))
        card.add(C.hline())
        # channel edit form (structural — keeps focus)
        edit = QGridLayout()
        edit.setHorizontalSpacing(12)
        edit.setVerticalSpacing(6)
        idx = C.line_edit(rv["chedit_idx"], placeholder="idx", accent=a, on_change=self.model.set_ale_chedit_idx)
        freq = C.line_edit(rv["chedit_freq"], placeholder="MHz (e.g. 14.109)", accent=a,
                           on_change=self.model.set_ale_chedit_freq)
        name = C.line_edit(rv["chedit_name"], placeholder="name", accent=a, on_change=self.model.set_ale_chedit_name)
        edit.addWidget(self._field("Edit Channel — CHEDIT", idx), 0, 0)
        edit.addWidget(self._field("Frequency", freq), 0, 1)
        edit.addWidget(self._field("Name", name), 0, 2)
        edit.addWidget(self._field(" ", C.button("Apply", kind="primary", accent=a,
                                                 on_click=self.model.ale_chedit_apply)), 0, 3)
        edit.setColumnStretch(0, 1)
        edit.setColumnStretch(1, 2)
        edit.setColumnStretch(2, 2)
        edit.setColumnStretch(3, 1)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 4, 0, 0)
        wl.addLayout(edit)
        card.add(wrap)
        return card

    # ------------------------------------------------------------------ small parts
    def _label(self, text) -> QWidget:
        return C.lbl(text, size=11, weight=600, color=T.FG_DIM)

    def _field(self, label, widget) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(C.lbl(label, size=11, weight=600, color=T.FG_DIM))
        v.addWidget(widget)
        return w

    def _readout(self, k, v) -> QWidget:
        w = QWidget()
        vv = QVBoxLayout(w)
        vv.setContentsMargins(0, 0, 0, 0)
        vv.setSpacing(4)
        vv.addWidget(C.lbl(k, size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.3))
        vv.addWidget(C.lbl(v, size=13, mono=True, weight=600, color="#25282c"))
        return w

    def _cell(self, text, width, color, *, mono=True, weight=400, align="l") -> QWidget:
        c = C.lbl(str(text), size=11.5, mono=mono, weight=weight, color=color, align=align)
        c.setFixedWidth(width)
        return c

    def _chip_row(self, chips) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        for text, active, cb in chips:
            lay.addWidget(self._chip(text, active, cb), 1)
        return w

    def _chip(self, text, active, cb) -> C.ClickableFrame:
        a = self.accent
        fg = "#fff" if active else T.FG_MUTED
        bg = a if active else "#ffffff"
        border = a if active else T.INPUT_BORDER
        chip = C.ClickableFrame(hover_bg=None,
                                base_css="QFrame{background:%s;border:1px solid %s;border-radius:5px;}" % (bg, border))
        cl = QHBoxLayout(chip)
        cl.setContentsMargins(0, 8, 0, 8)
        cl.addWidget(C.lbl(str(text), size=12, mono=True, weight=600, color=fg, align="c"), 1)
        chip.clicked.connect(lambda _=None: cb())
        return chip

    def _segment(self, options, *, compact: bool = False) -> QWidget:
        inner = C.scoped(QFrame(), "background:transparent;border:1px solid %s;border-radius:5px;" % T.INPUT_BORDER)
        il = QHBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(0)
        for i, (label, active, cb) in enumerate(options):
            if i:
                il.addWidget(C.vline(T.INPUT_BORDER, 30 if compact else 34))
            il.addWidget(self._seg_half(label, active, cb, compact), 1)
        if compact:
            return inner
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 6, 0, 0)
        wl.addWidget(inner)
        return wrap

    def _seg_half(self, text, active, cb, compact) -> QWidget:
        a = self.accent
        half = C.ClickableFrame(base_css="QFrame{background:%s;border:none;}" % (a if active else "#fff"))
        l = QHBoxLayout(half)
        l.setContentsMargins(10 if compact else 0, 6 if compact else 8, 10 if compact else 0, 6 if compact else 8)
        l.addWidget(C.lbl(text, size=12, mono=True, weight=600 if active else 500,
                          color="#fff" if active else T.FG_DIM, align="c"), 1)
        half.clicked.connect(lambda _=None: cb())
        return half

    def _toggle(self, on: bool, cb) -> QWidget:
        a = self.accent
        track = C.ClickableFrame(base_css="QFrame{background:%s;border-radius:10px;}" % (a if on else "#c4c6cb"))
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
        track.clicked.connect(lambda _=None: cb())
        return track

    def _twa_field(self, rv) -> QWidget:
        a = self.accent
        cur = f"{rv['twa_remain']}/{rv['twa_max']} s"
        edit = C.line_edit(rv["twa_draft"], placeholder=cur, accent=a,
                           on_change=self.model.set_ale_twa, on_return=self.model.ale_apply_twa)
        edit.setFixedWidth(96)
        return C.row(C.lbl("Twa", size=11, weight=600, color=T.FG_DIM), edit,
                     C.button("Set", on_click=self.model.ale_apply_twa), spacing=8)

    def _link_badge(self, rv) -> QWidget:
        s = rv["status"]
        return C.pill(s["label"], s["fg"], s["bg"], size=10)

    def _chan_row(self, cells, *, header: bool = False):
        if header:
            line = C.scoped(QWidget(), "background:transparent;border:none;border-bottom:1px solid %s;" % T.HAIRLINE)
            lay = QHBoxLayout(line)
            lay.setContentsMargins(6, 6, 6, 7)
            lay.setSpacing(10)
            widths = [34, 110, 120, 56, 60, 48]
            for i, txt in enumerate(cells):
                c = C.lbl(txt, size=10, weight=600, color=T.FG_FAINT, upper=True, letter_spacing=0.3)
                c.setFixedWidth(widths[i])
                lay.addWidget(c)
            lay.addStretch(1)
            return line
        c = cells
        active = c["current"]
        bg = self.model.theme.accent_soft if active else "transparent"
        row = C.ClickableFrame(
            hover_bg=T.ROW_DIV,
            base_css="QFrame{background:%s;border:none;border-bottom:1px solid %s;}" % (bg, T.ROW_DIV))
        lay = QHBoxLayout(row)
        lay.setContentsMargins(6, 7, 6, 7)
        lay.setSpacing(10)
        idx = C.lbl(str(c["idx"]), size=12, mono=True, weight=700 if active else 500,
                    color=self.accent if active else T.FG_MUTED)
        idx.setFixedWidth(34)
        freq = C.lbl(c["freq"], size=12.5, mono=True, weight=600, color=T.FG_BODY)
        freq.setFixedWidth(110)
        name = C.lbl(c["name"], size=12.5, color=T.FG_MUTED)
        name.setFixedWidth(120)
        band = C.lbl(c["band"], size=11, mono=True, color=T.FG_DIM)
        band.setFixedWidth(56)
        occ = C.lbl(c["occ_name"], size=11, mono=True, weight=600, color=c["occ_color"])
        occ.setFixedWidth(60)
        lq = C.lbl(c["lqa_txt"], size=12, mono=True, weight=600, color=c["lqa_color"])
        lq.setFixedWidth(48)
        for wdg in (idx, freq, name, band, occ, lq):
            lay.addWidget(wdg)
        lay.addStretch(1)
        row.clicked.connect(lambda _=None, i=c["idx"]: self.model.ale_prefill_channel(i))
        return row
