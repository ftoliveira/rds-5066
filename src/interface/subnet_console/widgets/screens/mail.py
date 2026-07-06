"""HF Mail (HMTP SAP 3 + HFPOP SAP 4) — mailbox client, compose, pipelining."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from ... import theme as T
from .. import common as C
from .base import Screen


def _avatar(init, bg, fg, size=30, radius=6, fsize=11) -> QLabel:
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


class MailScreen(Screen):
    topics = {"mail"}

    def build(self, lay: QVBoxLayout) -> None:
        m = self.model
        a = self.accent
        v = m.mail_view()

        badges = [C.lbl("HMTP · SAP 3", size=11, mono=True, weight=600, color="#fff", bg=a, radius=4, pad=(2, 8)),
                  C.lbl("HFPOP · SAP 4", size=11, mono=True, weight=600, color="#fff", bg="#2f8f5b", radius=4, pad=(2, 8))]
        compose_btn = C.button("✎  Compose", kind="primary", accent=a, on_click=m.compose_new)
        actions = C.row(C.status_text("BOUND · 3 & 4"),
                        C.button("Poll HFPOP", on_click=m.poll_hfpop),
                        compose_btn, spacing=8)
        lay.addWidget(C.page_header(
            "HF Mail",
            "Informal interpersonal e-mail over HF — SMTP submit + POP3 retrieve, "
            "encapsulated directly in S_UNIDATA · Annex F.5–F.6",
            badges=badges, right=actions))
        lay.addWidget(C.kpi_strip(v["kpis"], 4))
        lay.addWidget(self._client(v))
        lay.addLayout(self._bottom(v))

    # ---- mail client (rail | list | pane) ----
    def _client(self, v) -> QFrame:
        card = C.scoped(QFrame(), "background:#fff;border:1px solid %s;border-radius:6px;" % T.CARD_BORDER)
        card.setFixedHeight(432)
        l = QHBoxLayout(card)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)
        l.addWidget(self._rail(v))
        l.addWidget(self._list(v))
        l.addWidget(self._pane(v), 1)
        return card

    def _rail(self, v) -> QFrame:
        col = C.scoped(QFrame(), "background:#fafafb;border:none;border-right:1px solid %s;" % T.HAIRLINE)
        col.setFixedWidth(188)
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cap = C.caption("MAILBOXES", size=10, color=T.FG_FAINT)
        cap.setContentsMargins(12, 11, 12, 8)
        cv.addWidget(cap)
        for f in v["folders"]:
            cv.addWidget(self._folder(f))
        cv.addStretch(1)
        sess = C.scoped(QFrame(), "background:#eef1f4;border:1px solid #dfe3e8;border-radius:6px;")
        sl = QVBoxLayout(sess)
        sl.setContentsMargins(11, 10, 11, 10)
        sl.setSpacing(4)
        sl.addWidget(C.caption("HFPOP SESSION", size=9.5, color=T.FG_FAINT))
        sl.addWidget(C.row(C.dot(T.GREEN, 7), C.lbl("UIDL · pipelined", size=11, mono=True, color="#2a2d31"),
                           None, spacing=7))
        sl.addWidget(C.lbl("Last poll 14:22Z · 10-min cycle", size=10, color=T.FG_DIM))
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(10, 0, 10, 10)
        wl.addWidget(sess)
        cv.addWidget(wrap)
        return col

    def _folder(self, f) -> C.ClickableFrame:
        row = C.ClickableFrame(hover_bg="#eef0f2",
                               base_css="QFrame{background:%s;border:none;border-left:3px solid %s;}"
                                        % (f["row_bg"], f["bar"]))
        l = QHBoxLayout(row)
        l.setContentsMargins(12, 9, 12, 9)
        l.setSpacing(10)
        meta = C.col(C.lbl(f["name"], size=13, weight=f["weight"], color=f["fg"]),
                     C.lbl(f["sub"], size=10, mono=True, color=f["sub_fg"]), spacing=1)
        l.addWidget(meta, 1)
        if f["badge_show"]:
            l.addWidget(C.lbl(str(f["badge"]), size=10, mono=True, weight=600, color="#fff",
                              bg=f["badge_bg"], radius=9, pad=(1, 7), align="c"))
        row.clicked.connect(lambda _=None, k=f["key"]: self.model.set_mail_folder(k))
        return row

    def _list(self, v) -> QFrame:
        col = C.scoped(QFrame(), "background:#fff;border:none;border-right:1px solid %s;" % T.HAIRLINE)
        col.setFixedWidth(322)
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        hdr = C.scoped(QFrame(), "background:%s;border:none;border-bottom:1px solid %s;" % (T.INPUT_BG, T.HAIRLINE))
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(13, 9, 13, 9)
        hl.addWidget(C.lbl(v["list_title"], size=11, weight=700, color=T.FG_MUTED, letter_spacing=0.4))
        hl.addStretch(1)
        hl.addWidget(C.lbl("%d new" % v["unread"], size=10, mono=True, color=T.FG_GHOST))
        cv.addWidget(hdr)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(0)
        for r in v["rows"]:
            il.addWidget(self._mail_row(r))
        il.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setStyleSheet("QScrollArea{background:#fff;border:none;}")
        area.setWidget(inner)
        cv.addWidget(area, 1)
        return col

    def _mail_row(self, r) -> C.ClickableFrame:
        row = C.ClickableFrame(hover_bg="#f4f5f7",
                               base_css="QFrame{background:%s;border:none;border-left:3px solid %s;"
                                        "border-bottom:1px solid %s;}" % (r["row_bg"], r["bar"], T.ROW_DIV_FAINT))
        l = QHBoxLayout(row)
        l.setContentsMargins(13, 10, 13, 10)
        l.setSpacing(10)
        l.addWidget(_avatar(r["init"], r["av_bg"], r["av_fg"]), 0, Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(1)
        top = C.row(C.dot(r["unread_dot"], 6),
                    C.lbl(r["who"], size=12.5, weight=r["name_w"], color="#25282c"),
                    C.lbl(r["time"], size=10, mono=True, color=T.FG_GHOST2), spacing=6)
        top.layout().setStretch(1, 1)
        body.addWidget(top)
        body.addWidget(C.lbl(r["subj"], size=12, color="#3a3d42"))
        body.addWidget(C.lbl(r["preview"], size=11, color=T.FG_GHOST))
        if r["is_out"]:
            st = C.row(C.lbl(r["status"], size=9, mono=True, weight=600, color=r["status_fg"],
                             bg=r["status_bg"], radius=3, pad=(1, 6)),
                       C.lbl(r["size"], size=10, mono=True, color=T.FG_GHOST2), spacing=7)
            if r["show_bar"]:
                st.layout().addWidget(C.bar(r["pct"], "#b9821a", height=5, track="#eceef1", radius=3), 1)
            else:
                st.layout().addStretch(1)
            st.setContentsMargins(0, 5, 0, 0)
            body.addWidget(st)
        l.addLayout(body, 1)
        row.clicked.connect(lambda _=None, i=r["idx"]: self.model.set_mail_sel(i))
        return row

    def _pane(self, v) -> QFrame:
        col = C.scoped(QFrame(), f"background:{T.CONTENT_BG};border:none;")
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        if v["is_compose"]:
            cv.addWidget(self._compose(v), 1)
        elif v["show_read"]:
            cv.addWidget(self._read(v["cur"]), 1)
        return col

    def _compose(self, v) -> QWidget:
        m = self.model
        a = self.accent
        c = v["compose"]
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        hdr = C.scoped(QFrame(), "background:#fff;border:none;border-bottom:1px solid %s;" % T.SIDEBAR_DIV)
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(16, 11, 16, 11)
        hl.setSpacing(9)
        hl.addWidget(C.lbl("New Mail-Object", size=13.5, weight=700, color="#25282c"))
        hl.addWidget(C.lbl("HMTP SUBMIT", size=10, mono=True, weight=600, color="#fff", bg=a, radius=3, pad=(2, 7)))
        hl.addStretch(1)
        hl.addWidget(C.lbl("ARQ · NODE DELIVERY", size=10, mono=True, color=T.FG_FAINT))
        cv.addWidget(hdr)

        form = QWidget()
        fl = QVBoxLayout(form)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.setSpacing(11)
        fl.addLayout(self._crow("From", C.lbl("watch@falcon-01.s5066", size=12, mono=True, color=T.FG_MUTED)))
        to = C.line_edit(c["to"], accent=a, on_change=m.set_compose_to)
        fl.addLayout(self._crow("To", to))
        subj = C.line_edit(c["subj"], placeholder="Subject line", mono=False, accent=a, on_change=m.set_compose_subj)
        fl.addLayout(self._crow("Subject", subj))
        body = C.text_edit(c["body"], placeholder="Compose informal e-mail. 7-bit ITA5/ASCII, MSB set to zero per Annex F.",
                           accent=a, on_change=m.set_compose_body)
        fl.addWidget(body, 1)
        cv.addWidget(form, 1)

        footer = C.scoped(QFrame(), "background:#fff;border:none;border-top:1px solid %s;" % T.SIDEBAR_DIV)
        ftl = QHBoxLayout(footer)
        ftl.setContentsMargins(16, 11, 16, 11)
        ftl.setSpacing(9)
        ftl.addWidget(C.lbl("Single S_UNIDATA_REQUEST · pipelined EHLO→QUIT", size=10.5, mono=True, color=T.FG_FAINT))
        ftl.addStretch(1)
        ftl.addWidget(C.button("Discard", on_click=m.cancel_compose))
        ftl.addWidget(C.button("Submit to HF", kind="primary", accent=a, on_click=m.send_mail))
        cv.addWidget(footer)
        return col

    def _crow(self, label, widget) -> QHBoxLayout:
        l = QHBoxLayout()
        l.setSpacing(10)
        lab = C.lbl(label, size=11, weight=600, color=T.FG_DIM)
        lab.setFixedWidth(56)
        l.addWidget(lab)
        l.addWidget(widget, 1)
        return l

    def _read(self, cur) -> QWidget:
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        a = self.accent
        hdr = C.scoped(QFrame(), "background:#fff;border:none;border-bottom:1px solid %s;" % T.SIDEBAR_DIV)
        hl = QVBoxLayout(hdr)
        hl.setContentsMargins(18, 13, 18, 13)
        hl.setSpacing(0)
        hl.addWidget(C.lbl(cur["subj"], size=15, weight=700, color=T.FG))
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 7, 0, 0)
        meta.setSpacing(10)
        meta.addWidget(_avatar(cur["init"], a, "#fff", size=34, radius=7, fsize=12))
        who = C.col(C.lbl(cur["who"], size=13, weight=600, color="#25282c"),
                    C.lbl("%s · node %s" % (cur["addr"], cur["node"]), size=11, mono=True, color=T.FG_FAINT),
                    spacing=0)
        meta.addWidget(who, 1)
        right = C.col(C.lbl("%sZ · %s" % (cur["time"], cur["size"]), size=11, mono=True, color=T.FG_DIM),
                      C.lbl(cur["mime"], size=10, mono=True, color=T.FG_GHOST2), spacing=2)
        meta.addWidget(right)
        hl.addLayout(meta)
        tags = QHBoxLayout()
        tags.setContentsMargins(0, 10, 0, 0)
        tags.setSpacing(8)
        tags.addWidget(C.lbl(cur["dir_label"], size=9.5, mono=True, weight=600, color=cur["status_fg"],
                             bg=cur["status_bg"], radius=3, pad=(2, 8)))
        if cur["has_progress"]:
            tags.addWidget(C.lbl("%s · %s" % (cur["conf"], cur["pct"]), size=9.5, mono=True, color=T.FG_DIM))
        tags.addStretch(1)
        hl.addLayout(tags)
        cv.addWidget(hdr)

        bodylabel = C.lbl(cur["body"], size=13, color="#25282c")
        bodylabel.setWordWrap(True)
        bodylabel.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(18, 16, 18, 16)
        il.addWidget(bodylabel)
        il.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setStyleSheet(f"QScrollArea{{background:{T.CONTENT_BG};border:none;}}")
        area.setWidget(inner)
        cv.addWidget(area, 1)

        footer = C.scoped(QFrame(), "background:#fff;border:none;border-top:1px solid %s;" % T.SIDEBAR_DIV)
        ftl = QHBoxLayout(footer)
        ftl.setContentsMargins(16, 11, 16, 11)
        ftl.setSpacing(9)
        ftl.addWidget(C.button("Reply", kind="primary", accent=a, on_click=self.model.compose_new))
        ftl.addWidget(C.button("Forward"))
        ftl.addStretch(1)
        ftl.addWidget(C.lbl("U_PDU reassembled · in-order", size=10, mono=True, color=T.FG_GHOST2))
        cv.addWidget(footer)
        return col

    # ---- bottom row: pipelining + binding ----
    def _bottom(self, v) -> QHBoxLayout:
        r = QHBoxLayout()
        r.setSpacing(12)
        r.addWidget(self._pipelining(v), 1)
        r.addWidget(C.kv_card("Subnetwork Service Requirements", v["bind_rows"], width=360,
                              upper=False, k_size=10.5))
        return r

    def _pipelining(self, v) -> C.Card:
        a = self.accent
        tag = C.lbl("1 × S_UNIDATA_REQUEST", size=10, mono=True, weight=600, color="#fff", bg=a, radius=3, pad=(2, 8))
        card = C.Card("HMTP Enforced Command Pipelining", right=tag)
        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(0)
        # classic SMTP
        classic = C.scoped(QFrame(), "background:transparent;border:none;border-right:1px solid %s;" % T.ROW_DIV)
        clv = QVBoxLayout(classic)
        clv.setContentsMargins(16, 13, 16, 13)
        clv.setSpacing(0)
        clv.addWidget(C.lbl("CLASSIC SMTP — 7 ROUND-TRIPS", size=10, weight=600, color=T.FG_FAINT, letter_spacing=0.4))
        clv.addSpacing(8)
        for line in ["C: EHLO …", "S: 250 …", "C: MAIL FROM …", "S: 250 OK", "C: RCPT TO …", "S: 250 OK",
                     "C: DATA", "S: 354 …", "C: «message» .", "S: 250 sent", "C: QUIT", "S: 221 bye"]:
            clv.addWidget(C.lbl(line, size=10.5, mono=True, color="#a8acb2"))
        cols.addWidget(classic, 1)
        # HMTP
        hmtp = C.scoped(QFrame(), f"background:{v['pipe_bg']};border:none;")
        hv = QVBoxLayout(hmtp)
        hv.setContentsMargins(16, 13, 16, 13)
        hv.setSpacing(0)
        hv.addWidget(C.lbl("HMTP — 1 TRANSACTION (this node)", size=10, weight=700, color=a, letter_spacing=0.4))
        hv.addSpacing(8)
        for p in v["pipe_lines"]:
            hv.addWidget(C.lbl(p["t"], size=11, mono=True, color=p["color"]))
        cols.addWidget(hmtp, 1)
        holder = QWidget()
        holder.setLayout(cols)
        card.add(holder)
        foot = C.scoped(QFrame(), "background:transparent;border:none;border-top:1px solid %s;" % T.ROW_DIV)
        fll = QHBoxLayout(foot)
        fll.setContentsMargins(16, 9, 16, 9)
        fll.addWidget(C.lbl("14 SMTP steps → 1 U-PDU · saves ~6 channel turnarounds on a high-latency HF link",
                            size=10, mono=True, color=T.FG_DIM))
        card.add(foot)
        return card
