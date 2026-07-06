"""ConsoleModel — the single source of truth behind every screen.

Phase 1 seeds it with the exact demo data from the ``S5066 Subnet Console``
mockup and exposes *view accessors* (``links()``, ``sap_table()``, …) that return
plain dicts with accent-resolved colours. Screens read only through these
accessors and never touch raw state, so Phase 2 can swap the seed data for a
live :class:`~src.stanag_node.StanagNode` feed without touching the widgets.

Mutation goes through small command methods (``set_screen``, ``toggle_modem``,
``send_mail`` …). Structural changes emit :attr:`changed` with a topic string so
the affected screen rebuilds; pure text edits use the silent ``set_*`` setters so
they never yank focus out of a field mid-type.
"""
from __future__ import annotations

import time
from typing import List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .theme import DEFAULT_ACCENT, Theme
from . import theme as T

SCREENS = [
    "dashboard", "monitor", "chat", "mail",
    "ipclient", "filexfer", "sissocket", "modem", "config",
]

CHAT_SAP = 5   # HFCHAT Orderwire (Annex F.7)

# Annex F Table F-1 client names, keyed by SAP id (subset the console binds/shows).
SAP_NAMES = {1: "COSS", 2: "T-MMHS (S4406E)", 3: "HMTP", 4: "HFPOP",
             5: "HFCHAT Orderwire", 6: "RCOP", 7: "UDOP", 9: "IP Client"}

# HFCHAT (SAP 5) subnetwork service requirements — Annex F.7.3 point-to-point
# default: ARQ + NODE DELIVERY + IN-ORDER, priority 4, rank 15.
CHAT_CFG_DEFAULT = {"arq": True, "confirm": "node", "in_order": True, "priority": 4, "rank": 15}

# File transfer SAPs (Annex F) and the chunk-protocol prefixes (mirrors
# chat_app_110d._send_file): "<TAG>:<filename>\x00<data>", all tags 5 bytes.
RCOP_SAP = 6   # Reliable Connection-Oriented Protocol — ARQ
UDOP_SAP = 7   # Unreliable Datagram-Oriented Protocol — non-ARQ
FT_PREFIXES = (b"FILE:", b"FALL:", b"FCON:", b"FEND:")


def _now() -> str:
    return time.strftime("%H:%M:%S")

NODE_PROFILES = {
    "A": {"callsign": "FALCON-01", "address": "3.066.000.001", "station": "HQ Node — Lisbon",
          "modem_port": "4532"},
    "B": {"callsign": "CORVUS-06", "address": "3.066.000.006", "station": "Relay Node — Porto",
          "modem_port": "4533"},
}


def _prim_color(name: str, t: Theme) -> str:
    if "REJECT" in name or "OFF" in name:
        return T.RED
    if "CONFIRM" in name or "ACCEPT" in name or "AVAIL" in name:
        return T.GREEN_DARK
    return t.accent


def _fmt_size(b: int) -> str:
    if b >= 1048576:
        return f"{b / 1048576:.1f} MB"
    if b >= 1024:
        return f"{round(b / 1024)} KB"
    return f"{b} B"


class ConsoleModel(QObject):
    screen_changed = pyqtSignal(str)   # navigation → window switches the stack
    changed = pyqtSignal(str)          # a data topic changed → screen rebuilds
    accent_changed = pyqtSignal()      # accent changed → full restyle

    def __init__(self, node: str = "A", accent: str = DEFAULT_ACCENT,
                 modem_host: Optional[str] = None, modem_port: Optional[str] = None,
                 controller=None):
        super().__init__()
        self.theme = Theme(accent)
        prof = NODE_PROFILES.get(node.upper(), NODE_PROFILES["A"])
        self.screen = "dashboard"

        # Fase 2: quando um NodeController está ligado, o modelo opera em modo
        # "live" e os accessors ligados devolvem estado real em vez de demo.
        self.controller = controller
        self.live = controller is not None
        self._live_status: dict = {"running": False, "connected": False}

        # Fatia 2/3 — estado ao vivo. A thread do HFCHAT (`live_messages`) e o
        # log de eventos S-primitive (`live_events`, fonte única do feed do chat,
        # do event log do monitor e das "recent primitives" do dashboard) são
        # alimentados pelos sinais do NodeController. As contagens agregam o
        # snapshot de `status()` nos KPIs/contadores/barra de estado.
        self.chat_link_up = False
        self.live_messages: List[dict] = []
        self.live_events: List[dict] = []   # rich SIS events (newest first)
        self.live_tx = 0                    # U-PDUs enviados (S_UNIDATA_REQUEST)
        self.live_rx = 0                    # U-PDUs recebidos (S_UNIDATA_INDICATION)
        self.live_rejected = 0             # envios rejeitados pelo SIS

        # HFCHAT service requirements: `chat_cfg` é o aplicado (usado no envio),
        # `chat_cfg_draft` é o editável no ecrã Configuration até "Apply".
        self.chat_cfg = dict(CHAT_CFG_DEFAULT)
        self.chat_cfg_draft = dict(CHAT_CFG_DEFAULT)

        self.node = {
            "callsign": prof["callsign"], "address": prof["address"], "station": prof["station"],
            "freq": "8.142 MHz", "waveform": "S4539 / 39-tone", "dataRate": "2400 bps",
            "snr": "+18 dB", "activePeer": "CORVUS-06",
        }

        self.modem = {
            "ip": modem_host or "192.168.10.20",
            "port": modem_port or prof["modem_port"],
            "rate": 2400, "interleaver": "LONG", "linked": True,
        }

        # ---- mail state ----
        self.mail_folder = "inbox"
        self.mail_sel = 0
        self.poll_n = 0
        self.compose = {"to": "ops@corvus-06.s5066", "subj": "", "body": ""}
        self.outbox: List[dict] = [
            {"id": "o0", "to": "duty@merlin-04.s5066", "subj": "Relay schedule request",
             "time": "14:21", "size": "3.6 KB", "status": "TRANSFERRING", "conf": "NODE DELIVERY",
             "pct": 46, "body": "Requesting relay window for the 15:00Z bulletin push. Confirm you "
             "can hold ARQ for ~6 min at 2400 bps."},
        ]

        # ---- file transfer state ----
        self.ft_proto = "RCOP"
        self.ft_dest = "3.066.000.006"
        self.ft_pri = 6
        self.ft_seq = 0
        self.ft_staged: List[dict] = []
        # Live: a fila arranca vazia e é preenchida por envios/recepções reais.
        self.ft_received: List[dict] = []   # ficheiros reassemblados (RX)
        self.ft_rx_buffers: dict = {}       # {filename: bytearray} em curso
        self.ft_events: List[tuple] = []    # (time, dir, prim, proto, detail) — log RCOP/UDOP
        self.ft_active: Optional[dict] = None   # transferência em curso (progresso)
        self.ft_queue: List[dict] = [] if self.live else [
            {"id": "q0", "name": "imagery_tile_0842.jp2", "proto": "RCOP", "dest": "3.066.000.006",
             "size": "184 KB", "pri": 6, "pct": 38, "st": "SENDING"},
            {"id": "q1", "name": "sitrep_1500z.pdf", "proto": "RCOP", "dest": "3.066.000.004",
             "size": "42 KB", "pri": 8, "pct": 100, "st": "DELIVERED"},
            {"id": "q2", "name": "wx_grib_north.bin", "proto": "UDOP", "dest": "3.066.000.255",
             "size": "11 KB", "pri": 4, "pct": 100, "st": "SENT"},
            {"id": "q3", "name": "route_overlay.kmz", "proto": "RCOP", "dest": "3.066.000.009",
             "size": "9 KB", "pri": 6, "pct": 0, "st": "QUEUED"},
        ]

        self.cfg_tab = "chat"

        # ---- chat state ----
        self.draft = ""
        self.messages: List[dict] = [
            {"dir": "in", "from": "CORVUS-06", "addr": "·006", "time": "14:18:02",
             "text": "FALCON, request channel quality check on 8142.", "conf": "RECEIVED"},
            {"dir": "out", "from": "FALCON-01", "addr": "·001", "time": "14:18:44",
             "text": "Copy. SNR +18, BER nominal. Holding hard link.", "conf": "✓ DELIVERED"},
            {"dir": "in", "from": "CORVUS-06", "addr": "·006", "time": "14:19:20",
             "text": "Good copy. Switching to 2400 bps for the file transfer.", "conf": "RECEIVED"},
            {"dir": "out", "from": "FALCON-01", "addr": "·001", "time": "14:20:05",
             "text": "Acknowledged, ARQ in-order delivery confirmed. Send when ready.",
             "conf": "✓ DELIVERED"},
            {"dir": "in", "from": "CORVUS-06", "addr": "·006", "time": "14:21:40",
             "text": "Transfer queued on SAP 9. ETA 6 min at current rate.", "conf": "RECEIVED"},
            {"dir": "out", "from": "FALCON-01", "addr": "·001", "time": "14:22:02",
             "text": "Standing by. Monitoring queues.", "conf": "PENDING"},
        ]

    # ------------------------------------------------------------------ nav
    def set_screen(self, name: str) -> None:
        if name != self.screen:
            self.screen = name
            self.screen_changed.emit(name)

    def set_accent(self, hex_color: str) -> None:
        self.theme = Theme(hex_color)
        self.accent_changed.emit()

    # --------------------------------------------------------------- live (F2)
    def apply_live_status(self, snap: dict) -> None:
        """Receive a status snapshot from the NodeController (GUI thread).

        Only repaint when a field the UI shows actually changed, so the 500 ms
        poll doesn't rebuild the Modem pane under the user's cursor.
        """
        prev = self._live_status
        self._live_status = snap
        keys = ("running", "connected", "rate", "blocking", "cas", "sis_state",
                "sis_type", "dts", "arq_state", "arq_window", "arq_queue",
                "reset_pending", "tx_queue")
        if any(prev.get(k) != snap.get(k) for k in keys):
            # These four surfaces all read the status snapshot.
            for topic in ("modem", "dashboard", "monitor", "statusbar"):
                self.changed.emit(topic)
        # File-transfer progress tracks the queue draining, so poll it every tick.
        self._update_ft_progress(snap)

    def _bound_sap_count(self) -> int:
        if self.live and self.controller is not None:
            return len(self.controller.bound_saps)
        return 5   # demo

    def _sap_traffic(self) -> tuple:
        """Per-SAP TX/RX counts and newest activity time from ``live_events``."""
        tx: dict = {}
        rx: dict = {}
        last: dict = {}
        for e in self.live_events:   # newest first
            sap = e["sap"]
            if e["prim"] == "S_UNIDATA_INDICATION":
                rx[sap] = rx.get(sap, 0) + 1
            elif e["prim"] == "S_UNIDATA_REQUEST":
                tx[sap] = tx.get(sap, 0) + 1
            last.setdefault(sap, e["time"])
        return tx, rx, last

    # ------------------------------------------------------ live SIS (F2/F3)
    # These slots run on the GUI thread (queued connections from the tick
    # thread). Each records an event in the single ``live_events`` log and
    # repaints the affected screens (chat / monitor / dashboard).
    def _log_event(self, prim: str, *, sap="—", src="—", dst="—", size="—",
                   result: str = "OK", detail: str = "", chat: bool = False) -> None:
        """Push a rich SIS event to the front of the live log (cap 200)."""
        self.live_events.insert(0, {
            "time": _now(), "prim": prim, "sap": str(sap), "src": src, "dst": dst,
            "size": size, "result": result, "detail": detail or prim, "chat": chat})
        del self.live_events[200:]
        self.changed.emit("monitor")
        self.changed.emit("dashboard")

    def on_rx(self, ind: dict) -> None:
        """S_UNIDATA_INDICATION — log every SAP; append SAP 5 to the HFCHAT thread.

        Wired to ``NodeController.unidata_received``. Non-chat SAPs (IP, files)
        still land in the monitor's event log, but only SAP 5 feeds the thread.
        """
        sap = int(ind.get("sap", 0))
        src = int(ind.get("src_addr", 0))
        updu = ind.get("updu", b"") or b""
        nbytes = len(updu)
        self.live_rx += 1
        is_file = updu[:5] in FT_PREFIXES
        self._log_event("S_UNIDATA_INDICATION", sap=sap, src=f"·{src:03d}", dst="local",
                        size=f"{nbytes} B", result="OK",
                        detail=f"from node {src} · {nbytes} oct",
                        chat=(sap == CHAT_SAP and not is_file))
        if is_file:
            self._handle_ft_rx(updu, src, sap)
            return
        if sap != CHAT_SAP:
            return
        text = (ind.get("text") or "").rstrip("\r\n")
        self.live_messages.append({
            "dir": "in", "from": f"NODE {src}", "addr": f"·{src:03d}",
            "time": _now(), "text": text, "conf": "RECEIVED"})
        self.changed.emit("chat")

    def on_link_up(self, remote_addr: int, remote_sap: int) -> None:
        self.chat_link_up = True
        self._log_event("S_HARD_LINK_ESTABLISH_CONFIRM", sap=remote_sap, src="local",
                        dst=f"·{remote_addr:03d}", result="CONFIRMED",
                        detail=f"node {remote_addr} · SAP {remote_sap}", chat=True)
        self.changed.emit("chat")

    def on_link_down(self, remote_addr: int, confirm: bool) -> None:
        self.chat_link_up = False
        name = "S_HARD_LINK_TERMINATE_CONFIRM" if confirm else "S_HARD_LINK_TERMINATED"
        self._log_event(name, sap=CHAT_SAP, src="local", dst=f"·{remote_addr:03d}",
                        result="CONFIRMED" if confirm else "OK",
                        detail=f"node {remote_addr}", chat=True)
        self.changed.emit("chat")

    def on_rejected(self, sap_id: int, reason: str) -> None:
        self.live_rejected += 1
        self._log_event("S_UNIDATA_REQUEST_REJECTED", sap=sap_id, src="local",
                        result=reason, detail=f"SAP {sap_id} · {reason}",
                        chat=(int(sap_id) == CHAT_SAP))
        if int(sap_id) == CHAT_SAP:
            self.changed.emit("chat")

    def toggle_chat_link(self) -> None:
        """Establish / terminate the SAP 5 hard link to the configured peer."""
        if not (self.live and self.controller is not None):
            self.chat_link_up = not self.chat_link_up   # demo: visual only
            self.changed.emit("chat")
            return
        if self.chat_link_up:
            self.controller.hard_link_terminate(CHAT_SAP)
            self._log_event("S_HARD_LINK_TERMINATE_REQUEST", sap=CHAT_SAP, src="local",
                            dst=f"node {self.controller.remote_id}",
                            result="PENDING", detail=f"SAP {CHAT_SAP}", chat=True)
        else:
            self.controller.hard_link_establish(CHAT_SAP, CHAT_SAP)
            self._log_event("S_HARD_LINK_ESTABLISH_REQUEST", sap=CHAT_SAP, src="local",
                            dst=f"node {self.controller.remote_id}", result="PENDING",
                            detail=f"SAP {CHAT_SAP} → node {self.controller.remote_id}", chat=True)
        self.changed.emit("chat")

    def _chat_delivery_mode(self):
        """Build the S_UNIDATA DeliveryMode from the applied HFCHAT config."""
        from src.stypes import DeliveryMode  # lazy: keeps demo mode backend-free
        c = self.chat_cfg
        return DeliveryMode(arq_mode=bool(c["arq"]), in_order=bool(c["in_order"]),
                            node_delivery_confirm=(c["confirm"] == "node"),
                            client_delivery_confirm=(c["confirm"] == "client"))

    def _send_chat_live(self, text: str) -> None:
        payload = text.encode("ascii", "replace") + b"\r\n"
        try:
            self.controller.send_unidata(CHAT_SAP, CHAT_SAP, payload,
                                         priority=int(self.chat_cfg["priority"]),
                                         mode=self._chat_delivery_mode())
        except Exception as exc:
            self.live_rejected += 1
            self._log_event("S_UNIDATA_REQUEST_REJECTED", sap=CHAT_SAP, src="local",
                            result="ERROR", detail=f"send failed: {exc}", chat=True)
            return
        self.live_tx += 1
        remote = self.controller.remote_id
        self.live_messages.append({
            "dir": "out", "from": self.node["callsign"],
            "addr": "·" + self.node["address"].split(".")[-1],
            "time": _now(), "text": text, "conf": "SENT · ARQ"})
        self._log_event("S_UNIDATA_REQUEST", sap=CHAT_SAP, src="local", dst=f"·{remote:03d}",
                        size=f"{len(payload)} B", result="OK",
                        detail=f"SAP {CHAT_SAP} → node {remote} · {len(payload)} oct", chat=True)

    # ------------------------------------------------------------- modem cmd
    def set_modem_ip(self, v: str) -> None:
        self.modem["ip"] = v

    def set_modem_port(self, v: str) -> None:
        self.modem["port"] = "".join(c for c in v if c.isdigit())[:5]

    def set_modem_rate(self, n: int) -> None:
        self.modem["rate"] = n
        if self.live and self.controller is not None:
            self.controller.set_rate(n)
        self.changed.emit("modem")

    def set_modem_interleaver(self, v: str) -> None:
        self.modem["interleaver"] = v
        if self.live and self.controller is not None:
            self.controller.set_interleaver(v)
        self.changed.emit("modem")

    def toggle_modem(self) -> None:
        if self.live and self.controller is not None:
            # Connect = build+start the live node; Disconnect = tear it down.
            if self.controller.running:
                self.controller.stop()
            else:
                self.controller.start(host=self.modem["ip"], port=self.modem["port"],
                                      bitrate=self.modem["rate"],
                                      interleaver=self.modem["interleaver"])
            self.changed.emit("modem")
            return
        self.modem["linked"] = not self.modem["linked"]
        self.changed.emit("modem")

    def reset_modem(self) -> None:
        self.modem.update(ip="192.168.10.20", port="4532", rate=2400, interleaver="LONG")
        self.changed.emit("modem")

    # -------------------------------------------------------------- mail cmd
    def set_mail_folder(self, f: str) -> None:
        self.mail_folder = f
        self.mail_sel = 0
        self.changed.emit("mail")

    def set_mail_sel(self, i: int) -> None:
        self.mail_sel = i
        if self.mail_folder == "compose":
            self.mail_folder = "inbox"
        self.changed.emit("mail")

    def compose_new(self) -> None:
        self.mail_folder = "compose"
        self.mail_sel = 0
        self.changed.emit("mail")

    def cancel_compose(self) -> None:
        self.mail_folder = "inbox"
        self.mail_sel = 0
        self.changed.emit("mail")

    def poll_hfpop(self) -> None:
        self.poll_n = min(self.poll_n + 1, 2)
        self.mail_folder = "inbox"
        self.mail_sel = 0
        self.changed.emit("mail")

    def set_compose_to(self, v: str) -> None:
        self.compose["to"] = v

    def set_compose_subj(self, v: str) -> None:
        self.compose["subj"] = v

    def set_compose_body(self, v: str) -> None:
        self.compose["body"] = v

    def send_mail(self) -> None:
        c = self.compose
        size = round((len(c["body"]) / 1024 + 0.3) * 10) / 10
        self.outbox.insert(0, {
            "id": f"o{self.ft_seq}m", "to": c["to"] or "(no recipient)",
            "subj": c["subj"] or "(no subject)", "time": "14:22", "size": f"{size} KB",
            "status": "TRANSFERRING", "conf": "NODE DELIVERY", "pct": 8, "body": c["body"] or "",
        })
        self.compose["subj"] = ""
        self.compose["body"] = ""
        self.mail_folder = "outbox"
        self.mail_sel = 0
        self.changed.emit("mail")

    # ---------------------------------------------------------- file-xfer cmd
    def set_ft_proto(self, p: str) -> None:
        self.ft_proto = p
        self.ft_pri = 6 if p == "RCOP" else 4
        self.changed.emit("filexfer")

    def set_ft_dest(self, addr: str) -> None:
        self.ft_dest = addr

    def set_ft_pri(self, n: int) -> None:
        self.ft_pri = n
        self.changed.emit("filexfer")

    def stage_files(self, files: List[tuple]) -> None:
        """``files``: list of ``(name, data_bytes)`` — o conteúdo é preciso p/ envio live."""
        for i, (name, data) in enumerate(files):
            data = data or b""
            self.ft_staged.append({"id": f"f{self.ft_seq}_{i}", "name": name, "bytes": len(data),
                                   "data": data, "size": _fmt_size(len(data))})
        self.ft_seq += 1
        self.changed.emit("filexfer")

    def remove_staged(self, sid: str) -> None:
        self.ft_staged = [f for f in self.ft_staged if f["id"] != sid]
        self.changed.emit("filexfer")

    def send_ft(self) -> None:
        if not self.ft_staged:
            return
        if self.live and self.controller is not None:
            self._send_ft_live()
            return
        jobs = []
        for i, f in enumerate(self.ft_staged):
            jobs.append({"id": f"q{self.ft_seq}_{i}", "name": f["name"], "proto": self.ft_proto,
                         "dest": self.ft_dest, "size": f["size"], "pri": self.ft_pri,
                         "pct": 4 if i == 0 else 0, "st": "SENDING" if i == 0 else "QUEUED"})
        self.ft_queue = jobs + self.ft_queue
        self.ft_staged = []
        self.ft_seq += 1
        self.changed.emit("filexfer")

    # ---- file transfer live (Fatia 4): chunk/send, RX reassembly, progresso ----
    def _log_ft(self, direction: str, prim: str, proto: str, detail: str) -> None:
        self.ft_events.insert(0, (_now(), direction, prim, proto, detail))
        del self.ft_events[120:]

    def _chunk_file(self, filename: str, data: bytes) -> list:
        """Fatiar ``data`` em blocos MTU com prefixos FILE/FCON/FEND/FALL (Annex F)."""
        mtu = getattr(self.controller, "max_user_data_bytes", 128) if self.controller else 128
        pfx_file = f"FILE:{filename}\x00".encode("utf-8")
        pfx_cont = f"FCON:{filename}\x00".encode("utf-8")
        pfx_end = f"FEND:{filename}\x00".encode("utf-8")
        pfx_all = f"FALL:{filename}\x00".encode("utf-8")
        size = len(data)
        if size == 0:
            return [pfx_all]
        chunks: list = []
        offset = 0
        while offset < size:
            pfx = pfx_file if offset == 0 else pfx_cont
            space = max(1, mtu - len(pfx))
            chunk = data[offset:offset + space]
            if offset == 0 and len(chunk) >= size:
                pfx = pfx_all           # cabe num único bloco
            elif offset + len(chunk) >= size:
                pfx = pfx_end           # último bloco de vários
            chunks.append(pfx + chunk)
            offset += len(chunk)
        return chunks

    def _send_ft_live(self) -> None:
        from src.stypes import DeliveryMode  # lazy: mantém o modo demo sem backend
        arq = self.ft_is_rcop
        sap = RCOP_SAP if arq else UDOP_SAP
        proto = self.ft_proto
        mode = DeliveryMode(arq_mode=arq, in_order=arq, node_delivery_confirm=arq)
        remote = self.controller.remote_id
        s = self._live_status
        baseline = (int(s.get("tx_queue") or 0) + int(s.get("arq_queue") or 0)
                    + int(s.get("arq_unacked") or 0))
        order, new_jobs = [], []
        for i, f in enumerate(self.ft_staged):
            chunks = self._chunk_file(f["name"], f.get("data", b""))
            jid = f"tx{self.ft_seq}_{i}"
            job = {"id": jid, "name": f["name"], "proto": proto, "dest": f"node {remote}",
                   "size": f["size"], "pri": self.ft_pri, "pct": 0, "st": "QUEUED"}
            new_jobs.append(job)
            failed = False
            for ch in chunks:
                try:
                    self.controller.send_unidata(sap, sap, ch, priority=int(self.ft_pri), mode=mode)
                    self.live_tx += 1
                except Exception as exc:
                    self.live_rejected += 1
                    job["st"] = "FAILED"
                    self._log_ft("TX", "S_UNIDATA_REQUEST_REJECTED", proto, f"{f['name']}: {exc}")
                    failed = True
                    break
            if not failed:
                order.append((jid, len(chunks)))
                self._log_ft("TX", "S_UNIDATA_REQUEST", proto,
                             f"{f['name']} · SAP {sap} · {len(chunks)} blk · {'ARQ' if arq else 'non-ARQ'}")
                self._log_event("S_UNIDATA_REQUEST", sap=sap, src="local", dst=f"·{remote:03d}",
                                size=f"{len(chunks)} blk", result="OK",
                                detail=f"{f['name']} · {proto} · {len(chunks)} chunks")
        self.ft_queue = new_jobs + self.ft_queue
        self.ft_staged = []
        self.ft_seq += 1
        if order:
            self.ft_active = {"order": order, "total": sum(n for _, n in order),
                              "baseline": baseline, "arq": arq}
        self.changed.emit("filexfer")

    def _update_ft_progress(self, snap: dict) -> None:
        """Repartir o esvaziamento da fila (FIFO) pelos jobs da transferência ativa."""
        act = self.ft_active
        if not act:
            return
        pending = (int(snap.get("tx_queue") or 0) + int(snap.get("arq_queue") or 0)
                   + int(snap.get("arq_unacked") or 0))
        my_pending = max(0, pending - act["baseline"])
        resolved = max(0, act["total"] - my_pending)
        jobs = {j["id"]: j for j in self.ft_queue}
        done_state = "DELIVERED" if act["arq"] else "SENT"
        remaining = resolved
        changed = False
        for jid, n in act["order"]:
            j = jobs.get(jid)
            here = min(remaining, n)
            remaining -= here
            if j is None:
                continue
            pct = 100 if n == 0 else round(here / n * 100)
            st = done_state if pct >= 100 else ("SENDING" if here > 0 else "QUEUED")
            if j.get("pct") != pct or j.get("st") != st:
                j["pct"], j["st"] = pct, st
                changed = True
        if my_pending == 0:
            for jid, n in act["order"]:
                j = jobs.get(jid)
                if j is not None:
                    j["pct"], j["st"] = 100, done_state
            self._log_ft("RX" if act["arq"] else "TX",
                         "S_UNIDATA_REQUEST_CONFIRM" if act["arq"] else "UDOP_DATAGRAM",
                         self.ft_proto, "transfer complete" if act["arq"] else "datagrams sent")
            self.ft_active = None
            changed = True
        if changed:
            self.changed.emit("filexfer")

    def _handle_ft_rx(self, raw: bytes, src: int, sap: int) -> None:
        null = raw.find(b"\x00")
        if null < 0:
            return
        try:
            header = raw[:null].decode("utf-8")
        except UnicodeDecodeError:
            return
        tag, _, filename = header.partition(":")
        if not filename:
            return
        data = raw[null + 1:]
        proto = "RCOP" if sap == RCOP_SAP else ("UDOP" if sap == UDOP_SAP else "FILE")
        if tag == "FALL":
            self._log_ft("RX", "FALL", proto, f"{filename} · {len(data)} B · 1 chunk")
            self._ft_rx_complete(filename, bytes(data), src, proto)
        elif tag == "FILE":
            self.ft_rx_buffers[filename] = bytearray(data)
            self._log_ft("RX", "FILE", proto, f"{filename} · chunk 1 · {len(data)} B")
        elif tag == "FCON":
            buf = self.ft_rx_buffers.setdefault(filename, bytearray())
            buf.extend(data)
            self._log_ft("RX", "FCON", proto, f"{filename} · +{len(data)} B ({len(buf)} B)")
        elif tag == "FEND":
            buf = self.ft_rx_buffers.pop(filename, bytearray())
            buf.extend(data)
            self._log_ft("RX", "FEND", proto, f"{filename} · {len(buf)} B complete")
            self._ft_rx_complete(filename, bytes(buf), src, proto)
        self.changed.emit("filexfer")

    def _ft_rx_complete(self, filename: str, data: bytes, src: int, proto: str) -> None:
        self.ft_received.append({"name": filename, "data": data, "size": _fmt_size(len(data)),
                                 "from": src, "time": _now(), "proto": proto})
        self.ft_queue.insert(0, {
            "id": f"rx{len(self.ft_received)}", "name": filename, "proto": proto,
            "dest": f"node {src}", "size": _fmt_size(len(data)), "pri": "—",
            "pct": 100, "st": "RECEIVED"})

    # -------------------------------------------------------------- misc cmd
    def set_cfg_tab(self, tab: str) -> None:
        self.cfg_tab = tab
        self.changed.emit("config")

    # ---- HFCHAT service requirements (config screen, chat tab) ----
    def set_chat_arq(self, arq: bool) -> None:
        self.chat_cfg_draft["arq"] = bool(arq)
        self.changed.emit("config")

    def set_chat_priority(self, n: int) -> None:
        self.chat_cfg_draft["priority"] = int(n)
        self.changed.emit("config")

    def toggle_chat_in_order(self) -> None:
        self.chat_cfg_draft["in_order"] = not self.chat_cfg_draft["in_order"]
        self.changed.emit("config")

    def cycle_chat_confirm(self) -> None:
        order = ["none", "node", "client"]
        i = order.index(self.chat_cfg_draft["confirm"])
        self.chat_cfg_draft["confirm"] = order[(i + 1) % len(order)]
        self.changed.emit("config")

    def apply_chat_cfg(self) -> None:
        """Commit the HFCHAT draft; subsequent sends use these requirements."""
        self.chat_cfg = dict(self.chat_cfg_draft)
        if self.live:
            c = self.chat_cfg
            deliv = {"none": "no-confirm", "node": "NODE", "client": "CLIENT"}[c["confirm"]]
            self._log_event("S_BIND_REQUEST", sap=CHAT_SAP, src="local", result="OK",
                            detail=f"HFCHAT: {'ARQ' if c['arq'] else 'non-ARQ'} · {deliv} · "
                                   f"pri {c['priority']}{' · in-order' if c['in_order'] else ''}",
                            chat=True)
        self.changed.emit("config")
        self.changed.emit("chat")   # header reflects the applied mode

    def revert_chat_cfg(self) -> None:
        self.chat_cfg_draft = dict(self.chat_cfg)
        self.changed.emit("config")

    def set_draft(self, v: str) -> None:
        self.draft = v

    def send_msg(self) -> None:
        text = self.draft.strip()
        if text:
            if self.live and self.controller is not None:
                self._send_chat_live(text)
            else:
                self.messages.append({"dir": "out", "from": self.node["callsign"],
                                      "addr": "·" + self.node["address"].split(".")[-1],
                                      "time": "14:22", "text": text, "conf": "PENDING"})
        self.draft = ""
        self.changed.emit("chat")

    # ============================================================ view data
    # Each accessor returns plain dicts/lists with colours already resolved
    # against the current theme, so screens are pure layout.

    def dashboard_kpis(self) -> list:
        gd = T.GREEN_DARK
        if self.live:
            s = self._live_status
            connected = bool(s.get("connected"))
            rate = int(s.get("rate") or 0)
            return [
                {"label": "Modem Link", "value": "UP" if connected else "DOWN",
                 "unit": "bps" if connected else "", "delta": f"CAS {s.get('cas', 'IDLE')}",
                 "delta_color": gd if connected else T.RED},
                {"label": "Hard Link SAP 5", "value": "1" if self.chat_link_up else "0",
                 "unit": "link", "delta": s.get("sis_state", "IDLE")},
                {"label": "TX Queue", "value": str(int(s.get("tx_queue") or 0)), "unit": "U-PDUs",
                 "delta": f"ARQ {s.get('arq_state', '-')}", "delta_color": gd},
                {"label": "U-PDUs", "value": str(self.live_rx), "unit": "rx",
                 "delta": f"{self.live_tx} tx · {self.live_rejected} rej",
                 "delta_color": T.RED if self.live_rejected else gd},
            ]
        return [
            {"label": "Active Links", "value": "3", "unit": "peers", "delta": "2 hard · 1 soft"},
            {"label": "Throughput", "value": "1.92", "unit": "kb/s", "delta": "↑ 14% last 60s", "delta_color": gd},
            {"label": "Bound SAPs", "value": "5", "unit": "of 16", "delta": "Mail · chat · IP up"},
            {"label": "Frame Errors", "value": "0.4", "unit": "%", "delta": "↓ nominal", "delta_color": gd},
        ]

    def links(self) -> list:
        g, gd, gb = T.GREEN, T.GREEN_DARK, T.GREEN_BG
        if self.live:
            s = self._live_status
            remote = self.controller.remote_id if self.controller is not None else 0
            connected = bool(s.get("connected"))
            rate = str(int(s.get("rate") or 0))
            if self.chat_link_up:
                typ, tfg, tbg, dotc = "HARD", "#1f6e43", gb, g
            elif connected:
                typ, tfg, tbg, dotc = "SOFT", T.FG_MUTED, "#eceef1", T.AMBER
            else:
                typ, tfg, tbg, dotc = "IDLE", T.RED_DARK, T.RED_BG, T.RED
            return [{"peer": f"NODE {remote}", "address": f"3.066.000.{remote:03d}", "type": typ,
                     "snr": "—", "rate": rate if connected else "—", "uptime": "—",
                     "dot": dotc, "snr_color": T.FG_GHOST2, "type_fg": tfg, "type_bg": tbg}]
        return [
            {"peer": "CORVUS-06", "address": "3.066.000.006", "type": "HARD", "snr": "+18", "rate": "2400",
             "uptime": "01:24:18", "dot": g, "snr_color": gd, "type_fg": "#1f6e43", "type_bg": gb},
            {"peer": "MERLIN-04", "address": "3.066.000.004", "type": "SOFT", "snr": "+11", "rate": "1200",
             "uptime": "00:42:55", "dot": g, "snr_color": gd, "type_fg": T.FG_MUTED, "type_bg": "#eceef1"},
            {"peer": "OSPREY-09", "address": "3.066.000.009", "type": "HARD", "snr": "+6", "rate": "600",
             "uptime": "00:08:31", "dot": T.AMBER, "snr_color": T.AMBER, "type_fg": "#1f6e43", "type_bg": gb},
            {"peer": "HARRIER-02", "address": "3.066.000.002", "type": "IDLE", "snr": "—", "rate": "—",
             "uptime": "—", "dot": T.RED, "snr_color": T.FG_GHOST2, "type_fg": T.RED_DARK, "type_bg": T.RED_BG},
        ]

    def quality(self) -> list:
        if self.live:
            s = self._live_status
            a = self.theme.accent
            rate = int(s.get("rate") or 0)
            blocking = int(s.get("blocking") or 0)
            txq = int(s.get("tx_queue") or 0)
            win = int(s.get("arq_window") or 0)

            def pct(x, mx):
                return f"{max(0, min(100, round(x / mx * 100)))}%"

            # Real link-layer metrics (we don't measure SNR/BER off the modem).
            return [
                {"label": "Modem Data Rate", "value": f"{rate} bps", "pct": pct(rate, 4800), "color": T.GREEN},
                {"label": "Blocking Factor", "value": str(blocking), "pct": pct(blocking, 1200), "color": a},
                {"label": "TX Queue Depth", "value": f"{txq} U-PDU", "pct": pct(txq, 32),
                 "color": T.AMBER if txq else T.GREEN},
                {"label": "ARQ TX Window", "value": f"{win} frames", "pct": pct(win, 16), "color": a},
            ]
        return [
            {"label": "Signal-to-Noise (SNR)", "value": "+18 dB", "pct": "82%", "color": T.GREEN},
            {"label": "Bit Error Rate", "value": "1.2e-4", "pct": "20%", "color": T.GREEN},
            {"label": "Channel Utilisation", "value": "64 %", "pct": "64%", "color": self.theme.accent},
            {"label": "Doppler / Multipath", "value": "Low", "pct": "24%", "color": T.GREEN},
        ]

    def quality_meta(self) -> dict:
        """Title + the two footer cells of the dashboard quality card."""
        if self.live:
            remote = self.controller.remote_id if self.controller is not None else 0
            connected = bool(self._live_status.get("connected"))
            return {"title": f"Link Metrics — NODE {remote}",
                    "cells": [("INTERLEAVER", self.modem["interleaver"]),
                              ("LINK STATE", "CONNECTED" if connected else "OFFLINE")]}
        return {"title": "Channel Quality — %s" % self.node["activePeer"],
                "cells": [("INTERLEAVER", "LONG (4.8s)"), ("ALE STATE", "LINKED")]}

    def queues(self) -> list:
        """Two cells for the dashboard TX/RX queue card."""
        if self.live:
            s = self._live_status
            txq = int(s.get("tx_queue") or 0)
            arqq = int(s.get("arq_queue") or 0)
            return [
                {"cap": "TX QUEUE", "num": str(txq), "unit": "U-PDUs",
                 "sub": f"ARQ {s.get('arq_state', '-')}"},
                {"cap": "ARQ WINDOW", "num": str(int(s.get("arq_window") or 0)), "unit": "frames",
                 "sub": f"queue {arqq} · LWE {s.get('arq_lwe', 0)}"},
            ]
        return [
            {"cap": "TX QUEUE", "num": "14", "unit": "U-PDUs", "sub": "3.2 KB pending · ARQ"},
            {"cap": "RX QUEUE", "num": "2", "unit": "U-PDUs", "sub": "reassembling 1"},
        ]

    def bound_saps(self) -> list:
        a = self.theme.accent
        return [
            {"sap": "3", "name": "HMTP — Mail Submit", "rank": 8, "pri": 6, "mode": "ARQ", "bg": a},
            {"sap": "4", "name": "HFPOP — Mail Retrieve", "rank": 8, "pri": 6, "mode": "ARQ", "bg": T.GREEN},
            {"sap": "5", "name": "HFCHAT Orderwire", "rank": 15, "pri": 4, "mode": "ARQ", "bg": a},
            {"sap": "9", "name": "IP Client", "rank": 8, "pri": 6, "mode": "ARQ/non-ARQ", "bg": T.GREEN},
            {"sap": "—", "name": "Raw SIS Socket Server", "rank": "—", "pri": "—", "mode": "pass-through", "bg": "#5a5e64"},
        ]

    def dash_prims(self) -> list:
        t = self.theme
        if self.live:
            return [{"time": e["time"], "name": e["prim"], "sap": e["sap"],
                     "color": _prim_color(e["prim"], t)} for e in self.live_events[:6]]
        return [
            {"time": "14:22:08", "name": "S_UNIDATA_INDICATION", "sap": "5", "color": _prim_color("S_UNIDATA_INDICATION", t)},
            {"time": "14:22:03", "name": "S_UNIDATA_REQUEST_CONFIRM", "sap": "9", "color": _prim_color("S_UNIDATA_REQUEST_CONFIRM", t)},
            {"time": "14:21:40", "name": "S_UNIDATA_REQUEST", "sap": "5", "color": _prim_color("S_UNIDATA_REQUEST", t)},
            {"time": "14:21:22", "name": "S_DATA_FLOW_ON", "sap": "9", "color": _prim_color("S_DATA_FLOW_ON", t)},
        ]

    # ---------------------------------------------------------------- monitor
    def counters(self) -> list:
        a = self.theme.accent
        if self.live:
            s = self._live_status
            return [
                {"label": "U-PDUs RX", "value": str(self.live_rx), "color": T.FG},
                {"label": "U-PDUs TX", "value": str(self.live_tx), "color": a},
                {"label": "TX Queue", "value": str(int(s.get("tx_queue") or 0)), "color": a},
                {"label": "Rejected", "value": str(self.live_rejected),
                 "color": T.RED if self.live_rejected else T.FG},
                {"label": "ARQ Window", "value": str(int(s.get("arq_window") or 0)), "color": T.FG},
            ]
        return [
            {"label": "Total U-PDUs", "value": "8 412", "color": T.FG},
            {"label": "TX / s", "value": "6.1", "color": a},
            {"label": "RX / s", "value": "4.8", "color": a},
            {"label": "Rejected", "value": "12", "color": T.RED},
            {"label": "Avg Latency", "value": "2.4s", "color": T.FG},
        ]

    def _sap_row(self, sap, name, state, rank, pri, mode, tx, rx, last, mand) -> dict:
        t = self.theme
        bound = state == "BOUND"
        idle = state in ("UNBOUND", "RESERVED")
        return {
            "sap": sap, "name": name, "state": state, "rank": rank, "pri": pri, "mode": mode,
            "tx": tx, "rx": rx, "last": last,
            "sap_color": t.sap_color(sap) if bound else T.FG_GHOST2,
            "name_color": T.FG_GHOST2 if idle else T.FG_BODY,
            "name_weight": 600 if bound else 400,
            "row_bg": T.CARD_BG if bound else "#fafafb",
            "state_fg": T.GREEN_DARK if bound else T.FG_GHOST2,
            "tag": "MAND" if mand else "OPT",
            "tag_fg": "#fff" if mand else T.FG_DIM,
            "tag_bg": "#5a5e64" if mand else "#eceef1",
        }

    def sap_table(self) -> list:
        if self.live:
            bound = set(self.controller.bound_saps) if self.controller is not None else set()
            txm, rxm, lastm = self._sap_traffic()
            out = []
            for sap in (1, 2, 3, 4, 5, 6, 7, 9):
                sid = str(sap)
                is_bound = sap in bound
                if is_bound:
                    out.append(self._sap_row(
                        sid, SAP_NAMES.get(sap, "—"), "BOUND", 0,
                        4 if sap == CHAT_SAP else 6, "ARQ",
                        str(txm.get(sid, 0)), str(rxm.get(sid, 0)), lastm.get(sid, "—"),
                        sap == 9))
                else:
                    out.append(self._sap_row(sid, SAP_NAMES.get(sap, "—"), "UNBOUND",
                                             "—", "—", "—", "0", "0", "—", sap == 9))
            return out
        rows = [
            ("1", "COSS", "UNBOUND", "—", "—", "—", "0", "0", "—", False),
            ("2", "T-MMHS (S4406E)", "UNBOUND", "—", "—", "—", "0", "0", "—", False),
            ("3", "HMTP", "BOUND", 8, 6, "ARQ", "512", "88", "13:58:40", False),
            ("4", "HFPOP", "BOUND", 8, 6, "ARQ", "94", "1 204", "14:22:00", False),
            ("5", "HFCHAT Orderwire", "BOUND", 15, 4, "ARQ", "146", "203", "14:22:08", False),
            ("6", "RCOP", "UNBOUND", "—", "—", "—", "0", "0", "—", False),
            ("7", "UDOP", "UNBOUND", "—", "—", "—", "0", "0", "—", False),
            ("9", "IP Client", "BOUND", 8, 6, "ARQ/nARQ", "5 902", "1 339", "14:22:03", True),
        ]
        return [self._sap_row(*r) for r in rows]

    def event_log(self) -> list:
        t = self.theme
        if self.live:
            out = []
            for e in self.live_events:
                result = e["result"]
                ok = result in ("OK", "CONFIRMED", "DELIVERED", "RECV", "SENT")
                out.append({"time": e["time"], "prim": e["prim"], "sap": e["sap"],
                            "src": e["src"], "dst": e["dst"], "size": e["size"],
                            "result": result, "color": _prim_color(e["prim"], t),
                            "res_color": T.GREEN_DARK if ok else (T.AMBER if result == "PENDING" else T.RED)})
            return out
        raw = [
            ("14:22:08.412", "S_UNIDATA_INDICATION", "5", "·006", "·001", "46 B", "OK"),
            ("14:22:03.901", "S_UNIDATA_REQUEST_CONFIRM", "9", "local", "·006", "1280 B", "CONFIRMED"),
            ("14:22:02.118", "S_UNIDATA_REQUEST", "5", "local", "·006", "28 B", "PENDING"),
            ("14:21:55.770", "S_UNIDATA_REQUEST", "9", "local", "·004", "1280 B", "OK"),
            ("14:21:40.330", "S_UNIDATA_INDICATION", "5", "·006", "·001", "52 B", "OK"),
            ("14:21:22.090", "S_DATA_FLOW_ON", "9", "subnet", "local", "4 B", "OK"),
            ("14:20:58.610", "S_UNIDATA_REQUEST_REJECTED", "9", "local", "·002", "1280 B", "NO LINK"),
            ("14:20:05.221", "S_UNIDATA_REQUEST_CONFIRM", "5", "local", "·006", "44 B", "DELIVERED"),
            ("14:18:44.870", "S_UNIDATA_REQUEST", "5", "local", "·006", "44 B", "OK"),
        ]
        out = []
        for time, prim, sap, src, dst, size, result in raw:
            ok = result in ("OK", "CONFIRMED", "DELIVERED")
            out.append({"time": time, "prim": prim, "sap": sap, "src": src, "dst": dst,
                        "size": size, "result": result, "color": _prim_color(prim, t),
                        "res_color": T.GREEN_DARK if ok else (T.AMBER if result == "PENDING" else T.RED)})
        return out

    # ------------------------------------------------------------------ chat
    def operators(self) -> list:
        a = self.theme.accent
        raw = [
            ("CORVUS-06", "3.066.000.006", "CR", T.GREEN, True),
            ("MERLIN-04", "3.066.000.004", "ME", T.GREEN, False),
            ("OSPREY-09", "3.066.000.009", "OS", T.AMBER, False),
            ("HARRIER-02", "3.066.000.002", "HA", T.RED, False),
            ("KESTREL-07", "3.066.000.007", "KE", T.RED, False),
        ]
        out = []
        for call, addr, init, d, active in raw:
            out.append({"call": call, "addr": addr, "init": init, "dot": d, "active": active,
                        "row_bg": self.theme.tint(0.90) if active else "transparent",
                        "bar": a if active else "transparent",
                        "av_bg": a if active else "#d3d6db",
                        "av_fg": "#fff" if active else T.FG_MUTED})
        return out

    def chat_header(self) -> dict:
        """Peer identity + hard-link affordance for the thread column header."""
        if self.live:
            remote = self.controller.remote_id if self.controller is not None else 0
            up = self.chat_link_up
            c = self.chat_cfg
            deliv = {"none": "NO CONFIRM", "node": "NODE DELIVERY",
                     "client": "CLIENT DELIVERY"}[c["confirm"]]
            sub = (f"3.066.000.{remote:03d} · SAP 5 · {'ARQ' if c['arq'] else 'non-ARQ'} / {deliv}"
                   + (" · IN-ORDER" if c["in_order"] else ""))
            return {
                "live": True, "init": f"N{remote}", "name": f"NODE {remote}",
                "sub": sub,
                "link_up": up,
                "status_label": "HARD LINK UP" if up else "NO HARD LINK",
                "status_fg": T.GREEN_DARK if up else T.FG_GHOST2,
                "status_bg": T.GREEN_BG if up else "#eceef1",
                "btn_label": "Terminate Link" if up else "Establish Link",
                "btn_kind": "danger" if up else "primary",
            }
        return {
            "live": False, "init": "CR", "name": "CORVUS-06",
            "sub": "3.066.000.006 · Point-to-point · ARQ / NODE DELIVERY",
            "link_up": True, "status_label": "IN-ORDER",
            "status_fg": T.GREEN_DARK, "status_bg": T.GREEN_BG,
            "btn_label": "", "btn_kind": "",
        }

    def chat_messages(self) -> list:
        a = self.theme.accent
        src = self.live_messages if self.live else self.messages
        out = []
        for m in src:
            if m["dir"] == "in":
                style = {"align": "l", "bubble_bg": "#ffffff", "bubble_border": "#dfe1e4",
                         "name_color": T.FG_MUTED, "conf_color": T.FG_GHOST2}
            else:
                style = {"align": "r", "bubble_bg": self.theme.tint(0.88),
                         "bubble_border": self.theme.tint(0.74), "name_color": a,
                         "conf_color": T.GREEN_DARK}
            out.append({**m, **style})
        return out

    def chat_prims(self) -> list:
        t = self.theme
        if self.live:
            return [{"time": e["time"], "name": e["prim"], "detail": e["detail"],
                     "color": _prim_color(e["prim"], t)}
                    for e in self.live_events if e["chat"]][:40]
        raw = [
            ("14:22:02", "S_UNIDATA_REQUEST", "SAP 5 → 3.066.000.006 · 28 oct"),
            ("14:22:00", "S_UNIDATA_REQUEST_CONFIRM", "msg 0x4A2 · NODE DELIVERY"),
            ("14:21:40", "S_UNIDATA_INDICATION", "from 3.066.000.006 · 46 oct"),
            ("14:20:05", "S_UNIDATA_REQUEST_CONFIRM", "msg 0x4A1 · delivered"),
            ("14:18:44", "S_UNIDATA_REQUEST", "SAP 5 → 3.066.000.006 · 44 oct"),
            ("14:15:10", "S_BIND_ACCEPT", "SAP 5 · Rank 15 · accepted"),
            ("14:15:09", "S_BIND_REQUEST", "SAP 5 · ARQ / NODE DELIVERY"),
        ]
        return [{"time": tm, "name": nm, "detail": dt, "color": _prim_color(nm, t)} for tm, nm, dt in raw]

    # -------------------------------------------------------------- ip client
    def ip_kpis(self) -> list:
        return [
            {"label": "Datagrams TX", "value": "5 902", "unit": "pkt", "delta": "1.21 MB"},
            {"label": "Datagrams RX", "value": "1 339", "unit": "pkt", "delta": "402 KB"},
            {"label": "Dropped", "value": "12", "unit": "pkt", "delta": "no link / TTL", "delta_color": T.RED},
            {"label": "Path MTU", "value": "1280", "unit": "bytes", "delta": "PMTU RFC1191", "delta_color": T.GREEN_DARK},
        ]

    def ip_bind(self) -> list:
        return [
            {"k": "LAN INTERFACE", "v": "tun0 (point-to-point)"},
            {"k": "LOCAL IP", "v": "10.66.0.1 / 24"},
            {"k": "HF NODE ADDRESS", "v": self.node["address"]},
            {"k": "SAP ID", "v": "9 · Rank 8 · Pri 6"},
            {"k": "INTERFACE MTU", "v": "1500 bytes"},
            {"k": "ICMP", "v": "enabled (RFC792)"},
        ]

    def ip_routes(self) -> list:
        gd, gb = "#1f6e43", "#e3f0e8"
        raw = [
            ("10.66.0.6/32", "3.066.000.006", "ARQ", gd, gb, "UP", "CORVUS-06"),
            ("10.66.0.4/32", "3.066.000.004", "ARQ", gd, gb, "UP", "MERLIN-04"),
            ("10.66.0.9/32", "3.066.000.009", "ARQ", gd, gb, "DEGRADED", "OSPREY-09"),
            ("239.0.0.1/32", "broadcast", "non-ARQ ×2", T.FG_MUTED, "#eceef1", "UP", "NET-ALL group"),
            ("10.66.0.2/32", "3.066.000.002", "ARQ", gd, gb, "DOWN", "HARRIER-02"),
        ]
        out = []
        for cidr, node, mode, mfg, mbg, st, links in raw:
            out.append({"cidr": cidr, "node": node, "mode": mode, "mode_fg": mfg, "mode_bg": mbg,
                        "st": st, "links": links,
                        "st_fg": T.GREEN_DARK if st == "UP" else (T.AMBER if st == "DEGRADED" else T.FG_GHOST2),
                        "dot": T.GREEN if st == "UP" else (T.AMBER if st == "DEGRADED" else T.RED)})
        return out

    def ip_qos(self) -> list:
        a = self.theme.accent
        raw = [
            ("EF · Voice", "0x2E", "12", "ARQ", True),
            ("AF41 · Video", "0x22", "10", "ARQ", True),
            ("CS6 · Network Ctrl", "0x30", "15", "ARQ", True),
            ("AF21 · OAM", "0x12", "6", "ARQ", True),
            ("CS0 · Best Effort", "0x00", "4", "non-ARQ", False),
        ]
        return [{"label": l, "dscp": d, "prio": p, "mode": m, "on": on,
                 "sw_bg": a if on else "#c4c6cb", "row_op": 1.0 if on else 0.55} for l, d, p, m, on in raw]

    def ip_log(self) -> list:
        a = self.theme.accent
        raw = [
            ("14:22:03.901", "10.66.0.1", "10.66.0.6", "TCP", "1280 B", "ARQ", "CONFIRMED"),
            ("14:22:01.550", "10.66.0.6", "10.66.0.1", "TCP", "1280 B", "ARQ", "RECV"),
            ("14:21:55.770", "10.66.0.1", "10.66.0.4", "TCP", "1280 B", "ARQ", "SENT"),
            ("14:21:52.110", "10.66.0.1", "239.0.0.1", "UDP", "512 B", "non-ARQ", "SENT"),
            ("14:21:48.330", "10.66.0.9", "10.66.0.1", "ICMP", "64 B", "ARQ", "RECV"),
            ("14:21:40.900", "10.66.0.1", "10.66.0.2", "TCP", "1280 B", "ARQ", "NO LINK"),
            ("14:21:33.221", "10.66.0.1", "10.66.0.6", "UDP", "880 B", "ARQ", "SENT"),
            ("14:21:20.010", "10.66.0.4", "10.66.0.1", "TCP", "1280 B", "ARQ", "RECV"),
            ("14:21:05.660", "10.66.0.1", "10.66.0.9", "ICMP", "64 B", "ARQ", "QUEUED"),
        ]
        out = []
        for time, src, dst, proto, ln, mode, result in raw:
            ok = result in ("SENT", "RECV", "CONFIRMED")
            out.append({"time": time, "src": src, "dst": dst, "proto": proto, "len": ln, "mode": mode,
                        "result": result,
                        "proto_color": T.AMBER if proto == "ICMP" else (T.PURPLE if proto == "UDP" else a),
                        "res_color": T.GREEN_DARK if ok else (T.AMBER if result == "QUEUED" else T.RED)})
        return out

    # ------------------------------------------------------------ raw sis srv
    def sk_kpis(self) -> list:
        return [
            {"label": "TCP Connections", "value": "5", "unit": "of 16", "delta": "4 bound · 1 idle"},
            {"label": "Primitives / s", "value": "11", "unit": "msg", "delta": "req + ind + confirm"},
            {"label": "Socket Throughput", "value": "7.3", "unit": "KB/s", "delta": "↑ steady", "delta_color": T.GREEN_DARK},
            {"label": "Server Uptime", "value": "02:14", "unit": "h:m", "delta": "since 12:08 UTC"},
        ]

    def sk_server(self) -> list:
        return [
            {"k": "BIND ADDRESS", "v": "127.0.0.1"},
            {"k": "TCP PORT", "v": "5066"},
            {"k": "MAX CLIENTS", "v": "16"},
            {"k": "MESSAGE FRAMING", "v": "SIS wrapper (0x90EB)"},
            {"k": "KEEP-ALIVE", "v": "S_KEEP_ALIVE · 30 s"},
            {"k": "BYTE ORDER", "v": "big-endian / MSB"},
        ]

    def sk_clients(self) -> list:
        t = self.theme
        raw = [
            ("#1", "127.0.0.1:51420", "HFCHAT Orderwire", "5", "15", "BOUND", "12:09:55"),
            ("#4", "127.0.0.1:51902", "HMTP — Mail Submit", "3", "8", "BOUND", "13:58:30"),
            ("#5", "127.0.0.1:51977", "HFPOP — Mail Retrieve", "4", "8", "BOUND", "12:40:11"),
            ("#2", "127.0.0.1:51558", "IP Client", "9", "8", "BOUND", "12:08:40"),
            ("#3", "127.0.0.1:52071", "unbound (handshake)", "—", "—", "CONNECTED", "14:21:02"),
        ]
        out = []
        for cid, remote, client, sap, rank, st, since in raw:
            bound = st == "BOUND"
            out.append({"id": cid, "remote": remote, "client": client, "sap": sap, "rank": rank,
                        "st": st, "since": since,
                        "st_fg": T.GREEN_DARK if bound else (T.AMBER if st == "CONNECTED" else T.FG_GHOST2),
                        "st_bg": T.GREEN_BG if bound else (T.AMBER_BG if st == "CONNECTED" else "#eceef1"),
                        "sap_bg": "#c9ccd1" if sap == "—" else t.sap_color(sap),
                        "client_fg": T.FG_BODY if bound else T.FG_DIM})
        return out

    def sk_wire(self) -> list:
        a = self.theme.accent
        raw = [
            ("14:22:08.412", "S → C", "S_UNIDATA_INDICATION", "5", "46 B"),
            ("14:22:03.901", "S → C", "S_UNIDATA_REQUEST_CONFIRM", "9", "12 B"),
            ("14:22:02.118", "C → S", "S_UNIDATA_REQUEST", "5", "40 B"),
            ("14:21:55.770", "C → S", "S_UNIDATA_REQUEST", "9", "1292 B"),
            ("14:21:02.880", "C → S", "S_BIND_REQUEST", "—", "8 B"),
            ("14:21:02.910", "S → C", "S_BIND_REJECTED", "—", "6 B"),
            ("14:20:48.330", "S → C", "S_KEEP_ALIVE", "all", "4 B"),
            ("14:20:05.221", "S → C", "S_UNIDATA_REQUEST_CONFIRM", "5", "12 B"),
        ]
        out = []
        for time, direction, name, sap, size in raw:
            color = T.RED if "REJECT" in name else (
                T.GREEN_DARK if ("ACCEPT" in name or "CONFIRM" in name or "AVAIL" in name) else T.FG_BODY)
            out.append({"time": time, "dir": direction, "name": name, "sap": sap, "size": size,
                        "dir_fg": a if direction == "C → S" else T.PURPLE, "color": color})
        return out

    # ------------------------------------------------------------- file xfer
    @property
    def ft_is_rcop(self) -> bool:
        return self.ft_proto == "RCOP"

    def ft_dests(self) -> list:
        return [
            {"addr": "3.066.000.006", "label": "3.066.000.006 · CORVUS-06"},
            {"addr": "3.066.000.004", "label": "3.066.000.004 · MERLIN-04"},
            {"addr": "3.066.000.009", "label": "3.066.000.009 · OSPREY-09"},
            {"addr": "3.066.000.255", "label": "3.066.000.255 · GROUP / broadcast"},
        ]

    def _ft_active_count(self) -> int:
        return len([j for j in self.ft_queue if j["st"] in ("QUEUED", "SENDING")])

    def ft_kpis(self) -> list:
        gd = T.GREEN_DARK
        q = str(self._ft_active_count())
        if self.live:
            mtu = getattr(self.controller, "max_user_data_bytes", 128) if self.controller else 128
            active = sum(1 for j in self.ft_queue if j["st"] == "SENDING")
            return [
                {"label": "Active Transfer", "value": str(active), "unit": "job",
                 "delta": f"{self.ft_proto} · {'ARQ' if self.ft_is_rcop else 'non-ARQ'}"},
                {"label": "Queue Depth", "value": q, "unit": "jobs",
                 "delta": "SAP 6/7 bound", "delta_color": gd},
                {"label": "Files Received", "value": str(len(self.ft_received)), "unit": "files",
                 "delta": "reassembled" if self.ft_received else "none yet",
                 "delta_color": gd if self.ft_received else T.FG_DIM},
                {"label": "SIS MTU", "value": str(mtu), "unit": "oct", "delta": "max U-PDU / chunk"},
            ]
        if self.ft_is_rcop:
            return [
                {"label": "Active Transfer", "value": "1", "unit": "job", "delta": "imagery_tile · 38%"},
                {"label": "Goodput", "value": "1.7", "unit": "kb/s", "delta": "ARQ in-order", "delta_color": gd},
                {"label": "Blocks ACKed", "value": "142", "unit": "of 371", "delta": "0 retransmit", "delta_color": gd},
                {"label": "Queue Depth", "value": q, "unit": "jobs", "delta": "RCOP connection up"},
            ]
        return [
            {"label": "Active Transfer", "value": "0", "unit": "job", "delta": "datagram · fire-and-forget"},
            {"label": "Throughput", "value": "2.4", "unit": "kb/s", "delta": "non-ARQ broadcast", "delta_color": gd},
            {"label": "Datagrams Sent", "value": "38", "unit": "PDUs", "delta": "no delivery confirm"},
            {"label": "Queue Depth", "value": q, "unit": "jobs", "delta": "UDOP socket open"},
        ]

    def ft_proto_params(self) -> list:
        if self.ft_is_rcop:
            return [{"k": "SAP", "v": "6"}, {"k": "TRANSPORT", "v": "ARQ · in-order"},
                    {"k": "CONNECTION", "v": "OPEN · 3.066.000.006"}, {"k": "BLOCK SIZE", "v": "512 oct"},
                    {"k": "WINDOW", "v": "8 blocks"}, {"k": "RETRANSMIT", "v": "selective ARQ"}]
        return [{"k": "SAP", "v": "7"}, {"k": "TRANSPORT", "v": "non-ARQ · datagram"},
                {"k": "CONNECTION", "v": "connectionless"}, {"k": "PDU SIZE", "v": "1024 oct"},
                {"k": "DELIVERY", "v": "best-effort"}, {"k": "RETRANSMIT", "v": "none"}]

    @staticmethod
    def _ext_of(name: str) -> str:
        parts = (name or "").split(".")
        return parts[-1][:4].upper() if len(parts) > 1 else "BIN"

    def ft_staged_view(self) -> list:
        return [{**f, "ext": self._ext_of(f["name"])} for f in self.ft_staged]

    def ft_staged_summary(self) -> str:
        if not self.ft_staged:
            return "No files selected"
        total = sum(f.get("bytes", 0) for f in self.ft_staged)
        n = len(self.ft_staged)
        return f"{n} file{'s' if n > 1 else ''} · {_fmt_size(total)} → {self.ft_proto}"

    def ft_prios(self) -> list:
        a = self.theme.accent
        out = []
        for n in (4, 6, 8, 10):
            on = self.ft_pri == n
            out.append({"n": n, "bg": a if on else "#fbfbfc", "fg": "#fff" if on else T.FG_MUTED,
                        "border": a if on else "#c4c6cb"})
        return out

    def ft_queue_view(self) -> list:
        a = self.theme.accent
        def qst(st):
            if st in ("DELIVERED", "SENT", "RECEIVED"):
                return T.GREEN_DARK, T.GREEN_BG, T.GREEN
            if st == "SENDING":
                return a, self.theme.tint(0.9), a
            if st == "FAILED":
                return T.RED, "#f6e1de", T.RED
            return T.FG_DIM, "#eceef1", T.FG_GHOST2
        out = []
        for j in self.ft_queue:
            fg, bg, barc = qst(j["st"])
            proto = (T.GREEN_DARK, T.GREEN_BG) if j["proto"] == "RCOP" else (T.PURPLE, "#ece5f4")
            out.append({**j, "ext": self._ext_of(j["name"]), "pct": f"{j.get('pct', 0)}%",
                        "bar_color": barc, "st_fg": fg, "st_bg": bg,
                        "proto_fg": proto[0], "proto_bg": proto[1]})
        return out

    def ft_log(self) -> list:
        a = self.theme.accent
        def color(n):
            if "REJECT" in n or "ERROR" in n or "RESET" in n:
                return T.RED
            if "CONFIRM" in n or "ACCEPT" in n or "DELIVER" in n:
                return T.GREEN_DARK
            return a
        if self.live:
            return [{"time": t, "dir": d, "name": n, "proto": p, "detail": dt,
                     "color": color(n), "dir_fg": a if d == "TX" else T.PURPLE}
                    for t, d, n, p, dt in self.ft_events]
        if self.ft_is_rcop:
            raw = [
                ("14:22:09", "TX", "RCOP_DATA_BLOCK", "RCOP", "blk 142/371 · 512 oct"),
                ("14:22:09", "RX", "RCOP_ACK", "RCOP", "win ack · up to 141"),
                ("14:22:06", "TX", "RCOP_DATA_BLOCK", "RCOP", "blk 141/371"),
                ("14:21:58", "TX", "RCOP_CONNECT_REQUEST", "RCOP", "→ 3.066.000.006"),
                ("14:21:59", "RX", "RCOP_CONNECT_CONFIRM", "RCOP", "accepted · win 8"),
                ("14:20:11", "TX", "S_UNIDATA_REQUEST", "RCOP", "SAP 6 · ARQ"),
                ("14:20:11", "RX", "S_UNIDATA_REQUEST_CONFIRM", "RCOP", "NODE DELIVERY"),
                ("14:15:02", "TX", "S_BIND_REQUEST", "RCOP", "SAP 6 · rank 8"),
            ]
        else:
            raw = [
                ("14:22:04", "TX", "UDOP_DATAGRAM", "UDOP", "→ 3.066.000.255 · 1024 oct"),
                ("14:22:04", "TX", "S_UNIDATA_REQUEST", "UDOP", "SAP 7 · non-ARQ"),
                ("14:22:04", "RX", "S_UNIDATA_REQUEST_CONFIRM", "UDOP", "no delivery confirm"),
                ("14:21:50", "TX", "UDOP_DATAGRAM", "UDOP", "wx_grib · pdu 11/11"),
                ("14:21:48", "TX", "UDOP_DATAGRAM", "UDOP", "wx_grib · pdu 1/11"),
                ("14:15:04", "TX", "S_BIND_REQUEST", "UDOP", "SAP 7 · rank 4"),
                ("14:15:04", "RX", "S_BIND_ACCEPT", "UDOP", "accepted"),
            ]
        return [{"time": t, "dir": d, "name": n, "proto": p, "detail": dt,
                 "color": color(n), "dir_fg": a if d == "TX" else T.PURPLE} for t, d, n, p, dt in raw]

    def ft_delivery_mode(self) -> dict:
        if self.ft_is_rcop:
            return {"label": "ARQ · in-order", "fg": T.GREEN_DARK, "bg": T.GREEN_BG}
        return {"label": "non-ARQ · datagram", "fg": T.PURPLE, "bg": "#ece5f4"}

    def ft_proto_desc(self) -> tuple:
        if self.ft_is_rcop:
            return ("RCOP — Reliable Connection-Oriented",
                    "Opens a connection to the peer and delivers the file as ACK-ed blocks with "
                    "selective ARQ retransmission. Use for files that must arrive intact.")
        return ("UDOP — Unreliable Datagram",
                "Sends the file as independent datagrams with no connection or acknowledgement. "
                "Lowest latency; use for broadcast or loss-tolerant data.")

    # ------------------------------------------------------------------ config
    def config_view(self) -> dict:
        a = self.theme.accent
        tab = self.cfg_tab
        meta = {
            "chat": {"sap": "5",
                     "title": "HFCHAT Orderwire — Subnetwork Service Requirements",
                     "subtitle": "SAP ID 5 · point-to-point default per Annex F.7.3",
                     "note": "Annex F: HFCHAT clients MAY bind using Rank = 15. Point-to-point default "
                     "is ARQ + NODE DELIVERY + IN-ORDER. Point-to-multipoint uses non-ARQ with a "
                     "configurable repeat count and NO delivery confirmation."},
            "ip": {"sap": "9", "rank": 8, "arq": True, "deliv": "NODE DELIVERY", "pri": 6, "in_order": True,
                   "title": "IP Client — Subnetwork Service Requirements",
                   "subtitle": "SAP ID 9 · MANDATORY · QoS-mapped delivery per Annex F.12",
                   "note": "Annex F: the IP client MUST be able to override default service type and "
                   "set delivery mode per datagram. Unicast → ARQ; multicast → non-ARQ. QoS labels map "
                   "to traffic priority. Rank = 15 discouraged unless performing subnet management."},
        }[tab]
        if tab == "chat":
            c = self.chat_cfg_draft
            deliv = {"none": "NO CONFIRMATION", "node": "NODE DELIVERY",
                     "client": "CLIENT DELIVERY"}[c["confirm"]]
            data = {**meta, "rank": c["rank"], "arq": c["arq"], "deliv": deliv,
                    "pri": c["priority"], "in_order": c["in_order"]}
        else:
            data = meta
        prios = [{"n": n, "on": n == data["pri"]} for n in (0, 4, 6, 12, 15)]
        return {"tab": tab, "accent": a, "prios": prios, "rank_pct": f"{data['rank'] / 15 * 100}%",
                "editable": tab == "chat", "dirty": self.chat_cfg != self.chat_cfg_draft, **data}

    # ------------------------------------------------------------------ modem
    def modem_view(self) -> dict:
        t = self.theme
        m = self.modem
        linked = m["linked"]
        rates = [{"n": n, "active": m["rate"] == n} for n in (75, 150, 300, 600, 1200, 2400, 4800)]
        ils = [
            {"v": "ZERO", "desc": "No interleaving · min latency"},
            {"v": "SHORT", "desc": "≈ 0.6 s span"},
            {"v": "LONG", "desc": "≈ 4.8 s · best fading immunity"},
        ]
        for il in ils:
            il["active"] = m["interleaver"] == il["v"]

        green = {"label": "LINKED", "fg": T.GREEN_DARK, "bg": T.GREEN_BG, "border": T.GREEN_BORDER,
                 "dot": T.GREEN, "halo": T.GREEN_HALO}
        amber = {"label": "CONNECTING", "fg": T.AMBER, "bg": T.AMBER_BG, "border": "#e3cfa0",
                 "dot": T.AMBER, "halo": "#f0e2c0"}
        red = {"label": "OFFLINE", "fg": T.RED_DARK, "bg": T.RED_BG, "border": T.RED_BORDER,
               "dot": T.RED, "halo": "#f0cfc9"}

        if self.live:
            ls = self._live_status
            running = bool(ls.get("running"))
            connected = bool(ls.get("connected"))
            rate = int(ls.get("rate") or m["rate"])
            stat = green if connected else (amber if running else red)
            return {"ip": m["ip"], "port": m["port"], "rate": rate, "linked": connected,
                    "rate_label": f"{rate} bps", "rates": rates, "ils": ils, "stat": stat,
                    "top_label": "MODEM LINKED" if connected else ("MODEM CONNECTING" if running else "MODEM OFFLINE"),
                    "btn_label": "Disconnect" if running else "Connect Modem",
                    "btn_bg": T.RED if running else t.accent}

        stat = green if linked else red
        return {"ip": m["ip"], "port": m["port"], "rate": m["rate"], "linked": linked,
                "rate_label": f"{m['rate']} bps", "rates": rates, "ils": ils, "stat": stat,
                "top_label": "MODEM LINKED" if linked else "MODEM OFFLINE",
                "btn_label": "Disconnect" if linked else "Connect Modem",
                "btn_bg": T.RED if linked else t.accent}

    # -------------------------------------------------------------- status bar
    def statusbar_view(self) -> dict:
        """Dynamic fields for the bottom status bar (SIS/clients/traffic/link)."""
        if self.live:
            s = self._live_status
            running = bool(s.get("running"))
            connected = bool(s.get("connected"))
            rate = int(s.get("rate") or 0)
            return {
                "sis_label": "SIS 127.0.0.1:5066 LISTENING" if running else "SIS OFFLINE",
                "sis_dot": T.GREEN if running else T.RED,
                "clients": f"{self._bound_sap_count()} CLIENTS BOUND",
                "traffic": f"TX {self.live_tx} · RX {self.live_rx}",
                "right": f"110D · {rate} bps · {'LINKED' if connected else 'NO LINK'}",
                "node": f"NODE {self.node['address']}",
            }
        n = self.node
        return {
            "sis_label": "SIS 127.0.0.1:5066 LISTENING", "sis_dot": T.GREEN,
            "clients": "5 CLIENTS BOUND", "traffic": "TX 14 · RX 2",
            "right": f"{n['waveform']} · {n['dataRate']} · SNR {n['snr']}",
            "node": f"NODE {n['address']}",
        }

    # ------------------------------------------------------------------- mail
    def mail_view(self) -> dict:
        a = self.theme.accent
        t = self.theme
        folder = self.mail_folder
        is_compose = folder == "compose"
        # While composing, the list still shows the inbox — resolve row fields
        # against this effective folder, not the literal "compose".
        folder_eff = "inbox" if is_compose else folder

        staged_extra = [
            {"id": "x1", "from": "MERLIN-04", "addr": "duty@merlin-04.s5066", "node": "3.066.000.004",
             "subj": "Re: relay schedule", "time": "14:19", "size": "1.4 KB", "unread": True,
             "mime": "text/plain", "body": "Copy your last. Relay window confirmed for 15:10Z. Will "
             "stage the bulletin on SAP 3 and flag NODE DELIVERY.\n\nMERLIN duty"},
            {"id": "x2", "from": "OSPREY-09", "addr": "wx@osprey-09.s5066", "node": "3.066.000.009",
             "subj": "WX OBS 1400Z", "time": "14:22", "size": "0.9 KB", "unread": True,
             "mime": "text/plain", "body": "OBS 1400Z: wind 240/12kt, vis 8km, 3/8 cloud 2400ft. MUF "
             "holding ~11 MHz. No change to plan."},
        ]
        base_inbox = [
            {"id": "i1", "from": "CORVUS-06", "addr": "ops@corvus-06.s5066", "node": "3.066.000.006",
             "subj": "CQ report 8142 kHz", "time": "14:05", "size": "2.1 KB", "unread": True,
             "mime": "text/plain", "body": "FALCON, confirming SNR +18 dB on 8142. Holding hard link "
             "for the file push.\n\nRecommend 2400 bps window 14:30-15:00Z. Mail-object will be "
             "~14 KB, S-MIME signed.\n\n-- CORVUS watch"},
            {"id": "i2", "from": "HQ-LISBON", "addr": "noc@hq.s5066", "node": "3.066.000.001",
             "subj": "Daily subnet bulletin", "time": "12:40", "size": "6.3 KB", "unread": False,
             "mime": "multipart/mixed", "body": "Subnet bulletin 22 JUN.\n\n1. ALE scan list updated, "
             "see attachment.\n2. SAP allocation unchanged.\n3. HFPOP poll interval set to 10 min for "
             "all nodes.\n\nNOC"},
            {"id": "i3", "from": "KESTREL-07", "addr": "ops@kestrel-07.s5066", "node": "3.066.000.007",
             "subj": "Re: link test", "time": "09:12", "size": "0.7 KB", "unread": False,
             "mime": "text/plain", "body": "Link test nominal both directions. BER 1e-4. Closing ticket."},
        ]
        inbox = staged_extra[:self.poll_n] + base_inbox
        sent = [
            {"id": "s1", "to": "ops@corvus-06.s5066", "subj": "Re: CQ report 8142 kHz", "time": "13:58",
             "size": "1.2 KB", "status": "DELIVERED", "conf": "NODE DELIVERY", "pct": 100,
             "body": "Copy CORVUS. Confirmed window 14:30Z, will hold 2400 bps. Send the signed object "
             "when ready."},
            {"id": "s2", "to": "noc@hq.s5066", "subj": "Station status FALCON-01", "time": "11:20",
             "size": "0.8 KB", "status": "DELIVERED", "conf": "CLIENT DELIVERY", "pct": 100,
             "body": "All SAPs nominal. 3 clients bound. No faults to report."},
        ]
        outbox = self.outbox
        unread = len([m for m in inbox if m["unread"]])
        queued_kb = f"{sum(float(o['size'].split()[0]) for o in outbox):.1f}" if outbox else "0.0"

        lst = sent if folder_eff == "sent" else outbox if folder_eff == "outbox" else inbox
        idx = min(self.mail_sel, max(0, len(lst) - 1))
        cur = lst[idx] if lst else None

        def st_clr(s):
            if s == "DELIVERED":
                return T.GREEN_DARK, T.GREEN_BG
            if s == "TRANSFERRING":
                return T.AMBER, T.AMBER_BG
            return T.FG_GHOST2, "#eceef1"

        def init_of(w):
            return "".join(c for c in (w or "?") if c.isalnum())[:2].upper()

        folders = []
        for key, name, sub, badge in [("inbox", "Inbox", "HFPOP poll", unread),
                                      ("outbox", "Outbox", "HMTP queue", len(outbox)),
                                      ("sent", "Sent", "HMTP submit", 0)]:
            active = key == folder or (is_compose and key == "inbox")
            folders.append({"key": key, "name": name, "sub": sub, "badge": badge,
                            "badge_show": badge > 0,
                            "row_bg": t.tint(0.88) if active else "transparent",
                            "bar": a if active else "transparent",
                            "fg": T.FG if active else "#34373c", "weight": 700 if active else 500,
                            "sub_fg": a if active else T.FG_GHOST,
                            "badge_bg": T.AMBER if key == "outbox" else T.GREEN})

        rows = []
        for i, m in enumerate(lst):
            who = m["from"] if folder_eff == "inbox" else (m.get("to", "").split("@")[0].upper())
            sel = i == idx and not is_compose
            c = st_clr(m["status"]) if m.get("status") else None
            rows.append({"init": init_of(who), "who": who, "subj": m["subj"], "time": m["time"],
                         "size": m["size"], "preview": (m.get("body", "").split("\n")[0])[:50],
                         "unread_dot": a if m.get("unread") else "transparent",
                         "name_w": 700 if m.get("unread") else 600, "is_out": folder_eff != "inbox",
                         "status": m.get("status", ""), "status_fg": c[0] if c else T.FG_MUTED,
                         "status_bg": c[1] if c else "#eceef1",
                         "show_bar": folder_eff == "outbox" and m.get("status") == "TRANSFERRING",
                         "pct": f"{m.get('pct', 0)}%", "idx": i,
                         "row_bg": t.tint(0.9) if sel else "#ffffff",
                         "bar": a if sel else "transparent",
                         "av_bg": a if sel else "#d3d6db", "av_fg": "#fff" if sel else T.FG_MUTED})

        cur_is_inbox = folder_eff == "inbox"
        cur_view = None
        if cur:
            cc = st_clr(cur["status"]) if cur.get("status") else (T.FG_MUTED, "#eceef1")
            cur_view = {
                "who": cur["from"] if cur_is_inbox else cur.get("to", "").split("@")[0].upper(),
                "addr": cur["addr"] if cur_is_inbox else cur.get("to", ""), "node": cur.get("node", "—"),
                "subj": cur["subj"], "time": cur["time"], "size": cur["size"],
                "mime": cur.get("mime", "text/plain"), "body": cur.get("body", ""),
                "init": init_of(cur["from"] if cur_is_inbox else cur.get("to", "")),
                "has_progress": not cur_is_inbox, "conf": cur.get("conf", ""),
                "status_fg": cc[0], "status_bg": cc[1], "pct": f"{cur.get('pct', 0)}%",
                "dir_label": ("RECEIVED · S_UNIDATA_INDICATION" if cur_is_inbox else
                              ("CONFIRMED · S_UNIDATA_REQUEST_CONFIRM" if cur.get("status") == "DELIVERED"
                               else "IN TRANSIT · S_UNIDATA_REQUEST")),
            }

        to = self.compose["to"] or "(recipient)"
        subj = self.compose["subj"] or "(no subject)"
        first_body = (self.compose["body"].split("\n")[0] if self.compose["body"] else "")[:38] or "message body..."
        pipe = [("EHLO falcon-01.s5066", "c"), (f"MAIL FROM:<watch@falcon-01.s5066>", "c"),
                (f"RCPT TO:<{to}>", "c"), ("DATA", "c"), (f"Subject: {subj}", "d"),
                (first_body, "d"), (".", "d"), ("QUIT", "c")]
        pipe_lines = [{"t": tt, "color": a if k == "c" else T.FG_MUTED} for tt, k in pipe]

        bind_rows = [
            {"k": "SAP — HMTP (submit)", "v": "3 · Rank 8 · Pri 6"},
            {"k": "SAP — HFPOP (retrieve)", "v": "4 · Rank 8 · Pri 6"},
            {"k": "Transmission Mode", "v": "ARQ"},
            {"k": "Delivery Confirmation", "v": "NODE DELIVERY"},
            {"k": "Deliver In Order", "v": "IN-ORDER"},
            {"k": "Char Encoding", "v": "ITA5 · 7-bit LSB, MSB=0"},
            {"k": "Service Extensions", "v": "PIPELINING · 8BITMIME"},
        ]
        kpis = [
            {"label": "Inbox", "value": str(unread), "unit": "unread", "delta": f"{len(inbox)} objects via HFPOP"},
            {"label": "Outbox Queue", "value": str(len(outbox)), "unit": "objects",
             "delta": f"{queued_kb} KB pending · ARQ", "delta_color": T.AMBER if outbox else T.FG_DIM},
            {"label": "Avg Mail-Object", "value": "38", "unit": "s/obj", "delta": "↓ enforced pipelining", "delta_color": T.GREEN_DARK},
            {"label": "Delivery Conf", "value": "100", "unit": "%", "delta": "NODE DELIVERY", "delta_color": T.GREEN_DARK},
        ]
        return {"kpis": kpis, "folders": folders, "rows": rows, "cur": cur_view, "is_compose": is_compose,
                "show_read": (not is_compose) and cur is not None, "pipe_lines": pipe_lines,
                "bind_rows": bind_rows, "unread": unread, "pipe_bg": t.tint(0.94),
                "list_title": "OUTBOX" if folder_eff == "outbox" else "SENT" if folder_eff == "sent" else "INBOX",
                "compose": self.compose}

    # ------------------------------------------------------------------- menus
    def menu_defs(self) -> list:
        """(menu label, [(item label, shortcut, screen-or-None)]) — None = no-op."""
        return [
            ("File", [("New Client Profile…", "Ctrl+N", None), ("Open Profile…", "Ctrl+O", None),
                      ("Save Profile", "Ctrl+S", None), ("---", "", None),
                      ("Export SIS Wire Log…", "", "sissocket"), ("Print Subnet Status…", "Ctrl+P", None),
                      ("---", "", None), ("Quit Console", "Ctrl+Q", "__quit__")]),
            ("Subnet", [("Connect Modem / Link…", "Ctrl+L", "modem"), ("Disconnect Link", "", None),
                        ("---", "", None), ("Bind All Clients", "", None), ("Unbind All Clients", "", None),
                        ("---", "", None), ("Hard Link Setup…", "", None), ("Broadcast Mode", "", None),
                        ("Data Rate & EOT…", "", "modem")]),
            ("Clients", [("HFCHAT Orderwire", "", "chat"), ("HF Mail", "", "mail"),
                         ("IP Client", "", "ipclient"), ("File Transfer (RCOP / UDOP)", "", "filexfer"),
                         ("Raw SIS Socket", "", "sissocket"), ("---", "", None),
                         ("New SIS Client…", "", None), ("Bind Selected", "", None), ("Unbind Selected", "", None)]),
            ("Tools", [("Traffic Monitor", "", "monitor"), ("Channel Scanner…", "", None),
                       ("ALE / Auto-Link…", "", None), ("Loopback Test", "", None),
                       ("Primitive Injector…", "", None), ("---", "", None), ("Configuration…", "Ctrl+,", "config")]),
            ("View", [("Subnet Dashboard", "", "dashboard"), ("Traffic Monitor", "", "monitor"),
                      ("---", "", None), ("SIS Wire Log", "", "sissocket"), ("Refresh", "Ctrl+R", None)]),
            ("Help", [("STANAG 5066 Reference", "", None), ("Annex F Primitive Map", "", None),
                      ("Keyboard Shortcuts", "Ctrl+/", None), ("---", "", None), ("About this Console", "", None)]),
        ]
