"""Modem Link (MIL-STD-188-110C) — connection + waveform parameters."""
from __future__ import annotations

from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


class ModemScreen(Screen):
    topics = {"modem"}

    def build(self, lay: QVBoxLayout) -> None:
        mv = self.model.modem_view()
        a = self.accent
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        # header with status pill on the right
        stat = mv["stat"]
        status_pill = C.ClickableFrame(
            base_css="QFrame{background:%s;border:1px solid %s;border-radius:20px;}" % (stat["bg"], stat["border"]),
            cursor=False)
        sp = QHBoxLayout(status_pill)
        sp.setContentsMargins(13, 6, 13, 6)
        sp.setSpacing(7)
        sp.addWidget(C.dot(stat["dot"], 8))
        sp.addWidget(C.lbl(stat["label"], size=11, mono=True, weight=600, color=stat["fg"]))
        badge = C.lbl("MIL-STD-188-110C", size=11, mono=True, weight=600, color="#fff", bg=a, radius=4, pad=(2, 8))
        col.addWidget(C.page_header(
            "Modem Link",
            "Serial (single-tone) waveform · fixed frequency · fixed data rates up to 4800 bps",
            badges=[badge], right=status_pill))

        col.addWidget(self._connection_card(mv))
        col.addWidget(self._waveform_card(mv))
        lay.addWidget(C.max_width(inner, 900))

    def _connection_card(self, mv) -> C.Card:
        a = self.accent
        card = C.Card("Connection").pad(18, 18, 18, 18)
        grid = QGridLayout()
        grid.setHorizontalSpacing(26)
        grid.setVerticalSpacing(16)
        ip = C.line_edit(mv["ip"], accent=a, on_change=self.model.set_modem_ip)
        port = C.line_edit(mv["port"], accent=a, on_change=self.model.set_modem_port)
        grid.addWidget(self._field("Modem IP Address", ip), 0, 0)
        grid.addWidget(self._field("TCP Port", port), 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        card.add_layout(grid)
        card.add(C.hline())
        specs = QGridLayout()
        specs.setContentsMargins(0, 4, 0, 0)
        specs.setHorizontalSpacing(16)
        for i, (k, v) in enumerate([("WAVEFORM", "STD-188-110C App C"),
                                    ("CHANNEL", "Single Tone · Fixed Freq"),
                                    ("MODULATION", "Serial PSK · 1800 Hz")]):
            specs.addWidget(self._readout(k, v), 0, i)
            specs.setColumnStretch(i, 1)
        card.add_layout(specs)
        return card

    def _waveform_card(self, mv) -> C.Card:
        a = self.accent
        card = C.Card("Waveform Parameters").pad(18, 18, 18, 18)
        # data rate header
        hdr = C.row(
            C.lbl("Data Rate — Taxa (bps)", size=11, weight=600, color=T.FG_DIM),
            None,
            C.lbl(mv["rate_label"], size=11, mono=True, weight=600, color=a),
        )
        card.add(hdr)
        rates = QGridLayout()
        rates.setContentsMargins(0, 8, 0, 0)
        rates.setHorizontalSpacing(6)
        for i, r in enumerate(mv["rates"]):
            rates.addWidget(self._rate_chip(r), 0, i)
            rates.setColumnStretch(i, 1)
        card.add_layout(rates)

        card.add(C.lbl("Interleaver", size=11, weight=600, color=T.FG_DIM))
        ils = QGridLayout()
        ils.setContentsMargins(0, 0, 0, 0)
        ils.setHorizontalSpacing(8)
        for i, il in enumerate(mv["ils"]):
            ils.addWidget(self._il_card(il), 0, i)
            ils.setColumnStretch(i, 1)
        card.add_layout(ils)

        # info note
        note = C.ClickableFrame(
            base_css="QFrame{background:%s;border:1px solid %s;border-radius:6px;}"
                     % (self.model.theme.accent_note_bg, self.model.theme.accent_note_border),
            cursor=False)
        nl = QHBoxLayout(note)
        nl.setContentsMargins(14, 12, 14, 12)
        nl.setSpacing(10)
        icon = C.lbl("i", size=11, mono=True, weight=700, color="#fff", bg=a, radius=4, align="c")
        icon.setFixedSize(18, 18)
        nl.addWidget(icon, 0)
        txt = C.lbl("Fixed-frequency single-tone operation carries a fixed rate from 75 to 4800 bps. "
                    "Higher rates need a cleaner channel; a longer interleaver improves robustness under "
                    "fading and multipath at the cost of end-to-end latency.",
                    size=11.5, color="#4a4d52")
        txt.setWordWrap(True)
        nl.addWidget(txt, 1)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 4, 0, 0)
        wl.addWidget(note)
        card.add(wrap)

        actions = C.row(None,
                        C.button("Reset", on_click=self.model.reset_modem),
                        C.button(mv["btn_label"], kind="primary", accent=mv["btn_bg"],
                                 on_click=self.model.toggle_modem),
                        spacing=9)
        card.add(actions)
        return card

    # ---- small parts ----
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
        vv.addWidget(C.lbl(v, size=12.5, mono=True, color="#25282c"))
        return w

    def _rate_chip(self, r) -> C.ClickableFrame:
        active = r["active"]
        a = self.accent
        fg = "#fff" if active else T.FG_MUTED
        bg = a if active else "#ffffff"
        border = a if active else T.INPUT_BORDER
        chip = C.ClickableFrame(hover_bg=None,
                                base_css="QFrame{background:%s;border:1px solid %s;border-radius:5px;}" % (bg, border))
        cl = QHBoxLayout(chip)
        cl.setContentsMargins(0, 9, 0, 9)
        cl.addWidget(C.lbl(str(r["n"]), size=12, mono=True, weight=600, color=fg, align="c"), 1)
        chip.clicked.connect(lambda _=None, n=r["n"]: self.model.set_modem_rate(n))
        return chip

    def _il_card(self, il) -> C.ClickableFrame:
        active = il["active"]
        a = self.accent
        fg = a if active else "#25282c"
        bg = self.model.theme.tint(0.9) if active else "#ffffff"
        border = a if active else T.HAIRLINE
        desc_fg = "#4a4d52" if active else T.FG_GHOST
        card = C.ClickableFrame(base_css="QFrame{background:%s;border:1px solid %s;border-radius:6px;}" % (bg, border))
        cl = QVBoxLayout(card)
        cl.setContentsMargins(13, 11, 13, 11)
        cl.setSpacing(3)
        cl.addWidget(C.lbl(il["v"], size=12.5, mono=True, weight=700, color=fg))
        d = C.lbl(il["desc"], size=10.5, color=desc_fg)
        d.setWordWrap(True)
        cl.addWidget(d)
        card.clicked.connect(lambda _=None, v=il["v"]: self.model.set_modem_interleaver(v))
        return card
