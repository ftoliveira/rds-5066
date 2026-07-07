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
from .backend.ale_controller import (
    OCC_NAMES, RF_POWER_STEPS, SIDEBAND_NAMES, SOUND_MODE_NAMES,
)

SCREENS = [
    "dashboard", "monitor", "chat", "mail",
    "ipclient", "filexfer", "radio", "sissocket", "modem", "config",
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

# Fatia 6 — HF Mail + IP Client SAPs (Annex F Table F-1).
HMTP_SAP = 3    # HF Mail Transfer Protocol — SMTP-over-HF submit (F.5)
HFPOP_SAP = 4   # HF POP3 — mail retrieval (F.6)
IP_SAP = 9      # IP Client — IPv4 datagram transport (F.12)


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


class _ClientNode:
    """Minimal ``StanagNode`` stand-in for the Annex F clients (Fatia 6).

    ``ip_client``/``hmtp``/``hf_pop3`` all reach the SIS through
    ``node.unidata_request``. With a controller, TX is forwarded to the live
    node — ``send_unidata`` always targets ``controller.remote_id`` (the same
    address the clients resolve to), so ``dest_addr`` is dropped. Without one it
    swallows output, letting a server-side parser (``HMTPServer``) decode an
    inbound buffer without emitting a reply onto the wire.
    """

    def __init__(self, controller=None):
        self._controller = controller

    def unidata_request(self, sap_id, dest_addr, dest_sap, priority,
                        ttl_seconds, mode=None, updu=b"") -> None:
        if self._controller is not None:
            self._controller.send_unidata(sap_id, dest_sap, updu, priority=priority,
                                          ttl_seconds=ttl_seconds, mode=mode)

    def bind(self, *args, **kwargs) -> None:   # SAPs already bound by the controller
        pass


# ---- ALE radio (remote-control protocol) demo + defaults -------------------
# Offline snapshot of an ``alel_state_t`` (same keys AleController decodes).
ALE_STATE_DEFAULT = {
    "fsm": 0, "fsm_name": "AVAILABLE", "linked": False,
    "scanning": False, "scan_rate": 5, "cur_channel": -1,
    "sinad": -1, "ber": -1, "rssi": -1, "noise": -1,
    "twa_remain": 0, "twa_max": 0, "self_addr": "", "link_peer": "",
    "voice_open": False, "ptt": False, "rx_voice": False,
    "sideband": 0, "sideband_name": "USB",
    "frames_rx": 0, "sounds_rx": 0, "words_valid": 0,
    "tx_active": False, "tx_power": 0.0, "tx_refl": 0.0, "tx_vswr": 0.0,
    "tx_power_unit": "W", "tx_refl_unit": "W",
    "sounding": False, "sounding_channel": -1,
    "forced": False, "active_service": "",
    "tcc_max": 0, "tm_max": 0, "occupancy_detect": False, "tx_power_dbm": 0,
}

_ALE_DEMO_CHANNELS = [
    ("3.596", "NVIS-A", "HF"), ("5.357", "REGION-1", "HF"), ("7.102", "LONG-A", "HF"),
    ("10.145", "LONG-B", "HF"), ("14.109", "DX-1", "HF"), ("18.106", "DX-2", "HF"),
    ("21.096", "DX-3", "HF"), ("24.928", "LONG-C", "HF"),
]
_ALE_DEMO_OCC = [1, 0, 2, 1, 1, 0, 1, 0]     # per-channel occupancy enum
_ALE_DEMO_LQA = [24, 12, 27, 21, 29, 9, 18, 6]


def _ale_demo_scene(prof: dict) -> dict:
    """A full, self-consistent Radio Control demo scene (no backend)."""
    channels = [{"idx": i, "freq": f, "name": nm, "band": b, "enabled": True}
                for i, (f, nm, b) in enumerate(_ALE_DEMO_CHANNELS)]
    scan = [{"label": f, "lqa": _ALE_DEMO_LQA[i], "occ": _ALE_DEMO_OCC[i],
             "occ_name": OCC_NAMES[_ALE_DEMO_OCC[i]]}
            for i, (f, nm, b) in enumerate(_ALE_DEMO_CHANNELS)]
    state = dict(ALE_STATE_DEFAULT)
    state.update(fsm=2, fsm_name="LINKED", linked=True, scanning=False, scan_rate=5,
                 cur_channel=4, sinad=22, ber=3, rssi=-71, noise=-103,
                 twa_remain=118, twa_max=300, self_addr="BR1", link_peer="BR2",
                 sideband=0, sideband_name="USB", frames_rx=184213, sounds_rx=42,
                 words_valid=9317, tx_power=44.2, tx_refl=1.1, tx_vswr=1.4,
                 tx_power_unit="W", tx_refl_unit="W", tcc_max=127, tm_max=30,
                 occupancy_detect=True, tx_power_dbm=47)
    lqa = {"n_channels": len(channels), "peers": [
        {"addr": "BR2", "online": True, "lqa": _ALE_DEMO_LQA},
        {"addr": "BR3", "online": True, "lqa": [17, 8, 22, 19, 25, 5, 14, 31]},
    ]}
    sound_hist = [
        {"t": "14:19:02", "ch": 4, "q": 29, "ack": "BR2"},
        {"t": "14:16:41", "ch": 3, "q": 21, "ack": "BR2"},
        {"t": "14:12:10", "ch": 6, "q": 18, "ack": "—"},
    ]
    log = [
        {"kind": 4, "kind_name": "LQA", "t": "14:19:02", "text": "LQA BR2 ch4 SINAD 29"},
        {"kind": 0, "kind_name": "RX", "t": "14:18:50", "text": "LINKED with BR2 on ch4"},
        {"kind": 3, "kind_name": "SND", "t": "14:16:41", "text": "Sounding ch3 acked by BR2"},
        {"kind": 2, "kind_name": "SYS", "t": "14:10:00", "text": "Scan resumed (5 ch/s)"},
    ]
    amd = [{"from": "BR2", "t": "14:18:33", "read": False,
            "text": "QSL, holding 14109. Send traffic when ready."}]
    return {"state": state, "channels": channels, "scan": scan, "lqa": lqa,
            "sound_hist": sound_hist, "log": log, "amd": amd}


class ConsoleModel(QObject):
    screen_changed = pyqtSignal(str)   # navigation → window switches the stack
    changed = pyqtSignal(str)          # a data topic changed → screen rebuilds
    accent_changed = pyqtSignal()      # accent changed → full restyle

    def __init__(self, node: str = "A", accent: str = DEFAULT_ACCENT,
                 modem_host: Optional[str] = None, modem_port: Optional[str] = None,
                 controller=None, ale_controller=None):
        super().__init__()
        self.theme = Theme(accent)
        prof = NODE_PROFILES.get(node.upper(), NODE_PROFILES["A"])
        self.screen = "dashboard"

        # Fase 2: quando um NodeController está ligado, o modelo opera em modo
        # "live" e os accessors ligados devolvem estado real em vez de demo.
        self.controller = controller
        self.live = controller is not None
        self._live_status: dict = {"running": False, "connected": False}

        # Radio Control (protocolo ALE 2G, UDP 54001). Independente do STANAG:
        # quando um AleController está ligado, o ecrã Radio Control mostra
        # telemetria real; sem ele, mostra o snapshot de demonstração.
        self.ale = ale_controller
        self.ale_live = ale_controller is not None
        self._ale_reachable = False
        self._ale_state: dict = dict(ALE_STATE_DEFAULT)
        self._ale_channels: List[dict] = []
        self._ale_scan: List[dict] = []
        self._ale_lqa: dict = {"n_channels": 0, "peers": []}
        self._ale_sound_hist: List[dict] = []
        self._ale_log: List[dict] = []      # newest first (cap 200)
        self._ale_amd: List[dict] = []      # received AMD, newest first (cap 50)
        self._ale_struct_key: tuple = ()    # last structural STATE fingerprint
        self._ale_tele_paint = 0.0          # throttle stamp for radio_tele repaints
        # Demo snapshot (used unless ale_live); a full, self-consistent scene.
        self._ale_demo = _ale_demo_scene(prof)
        # Editable drafts for the control forms (silent setters — no rebuild).
        self.ale_call_addr = ""
        self.ale_call_channel = ""          # "" = auto (-1)
        self.ale_group_members = ""         # comma/space separated
        self.ale_net_id = ""
        self.ale_amd_dest = ""
        self.ale_amd_text = ""
        self.ale_chedit_idx = ""
        self.ale_chedit_freq = ""
        self.ale_chedit_name = ""
        self.ale_twa_draft = ""             # Twa (s) pending Apply
        self.ale_sound_mode = 1             # 0=SINGLE 1=SCANNING 2=HANDSHAKE
        self.ale_force_service = "am"       # tenant for FORCE_LINK/NORMAL

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

        # ---- live mail (Fatia 6): HMTP SAP 3 submit / HFPOP SAP 4 retrieve ----
        # Recebidos e enviados ao vivo; a fila `mail_view` ramifica nestas listas.
        self.live_inbox: List[dict] = []    # mail-objects recebidos (SAP 3/4)
        self.live_sent: List[dict] = []     # mail-objects submetidos via HMTP
        self.live_outbox: List[dict] = []   # (reservado) submissões em trânsito
        self.mail_seq = 0

        # ---- live IP client (Fatia 6): SAP 9 ----
        self.ip_events: List[dict] = []     # log de datagramas (newest first)
        self.ip_tx = 0
        self.ip_rx = 0
        self.ip_dropped = 0

        # Clientes Annex F (lazy; construídos no 1.º uso live) — ver _ensure_clients.
        self._client_node = None
        self._ip = None
        self._hmtp = None
        self._hfpop = None
        self._hmtp_server = None

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
        # Raw SIS Socket screen (F.16): repaint when the server toggles, a client
        # connects/binds/leaves, or a new primitive crosses the wire.
        if (prev.get("sis_server_running") != snap.get("sis_server_running")
                or prev.get("sis_server_port") != snap.get("sis_server_port")
                or prev.get("sis_prim_count") != snap.get("sis_prim_count")
                or prev.get("sis_clients") != snap.get("sis_clients")):
            self.changed.emit("sissocket")
        # IP Client routes/KPIs read the link state — repaint when it toggles
        # (rare; datagram TX/RX repaint "ipclient" directly).
        if (prev.get("connected") != snap.get("connected")
                or prev.get("running") != snap.get("running")):
            self.changed.emit("ipclient")
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
        if sap == HMTP_SAP:
            self._handle_mail_rx(updu, src)
            return
        if sap == HFPOP_SAP:
            self._handle_pop_rx(updu, src)
            return
        if sap == IP_SAP:
            self._handle_ip_rx(updu, src)
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

    # ===================================================== Annex F clients (F6)
    # HF Mail (HMTP SAP 3 / HFPOP SAP 4) and IP Client (SAP 9). The real
    # ``annex_f`` clients drive the wire format; a ``_ClientNode`` routes their
    # TX through ``controller.send_unidata`` and their RX decode runs on the GUI
    # thread from ``on_rx`` (so their callbacks never touch the tick thread).
    def _ensure_clients(self) -> None:
        if self.controller is None or self._client_node is not None:
            return
        from src.annex_f.hf_pop3 import HFPOP3Client
        from src.annex_f.hmtp import HMTPClient, HMTPServer
        from src.annex_f.ip_client import IPClient

        local = self.controller.local_id
        remote = self.controller.remote_id
        self._client_node = _ClientNode(self.controller)
        table = {f"10.66.0.{local}": local, f"10.66.0.{remote}": remote}
        self._ip = IPClient(self._client_node, address_table=table)
        self._ip.on_ip_received = self._on_ip_received
        self._hmtp = HMTPClient(self._client_node)
        self._hfpop = HFPOP3Client(self._client_node)
        self._hfpop.on_message_retrieved = self._on_pop_message
        # RX-only parser: a swallow node keeps it from replying on the wire.
        self._hmtp_server = HMTPServer(_ClientNode())
        self._hmtp_server.on_mail_received = self._on_mail_received

    def _mail_ident(self) -> tuple:
        """(sender, hostname) for outbound HMTP, derived from this node."""
        host = self.node["callsign"].lower() + ".s5066"
        return f"watch@{host}", host

    # ---- HF Mail: submit (HMTP), poll (HFPOP), RX parse ----
    def _submit_mail_live(self) -> None:
        self._ensure_clients()
        from src.annex_f.hmtp import MailMessage
        c = self.compose
        to = c["to"] or "(no recipient)"
        subj = c["subj"] or "(no subject)"
        body = c["body"] or ""
        remote = self.controller.remote_id
        sender, host = self._mail_ident()
        data_body = f"Subject: {subj}\r\n\r\n{body}"
        try:
            self._hmtp.send_batch(remote, host, [MailMessage(sender, [to], data_body)],
                                  priority=6)
            self.live_tx += 1
        except Exception as exc:
            self.live_rejected += 1
            self._log_event("S_UNIDATA_REQUEST_REJECTED", sap=HMTP_SAP, src="local",
                            result="ERROR", detail=f"HMTP submit: {exc}")
            self.mail_folder = "sent"
            self.changed.emit("mail")
            return
        size = _fmt_size(len(data_body.encode("utf-8", "replace")))
        self.live_sent.insert(0, {
            "id": f"m{self.mail_seq}", "to": to, "subj": subj, "time": _now()[:5],
            "size": size, "status": "SENT", "conf": "HMTP · ARQ", "pct": 100,
            "mime": "text/plain", "body": body})
        self.mail_seq += 1
        self._log_event("S_UNIDATA_REQUEST", sap=HMTP_SAP, src="local", dst=f"·{remote:03d}",
                        size=size, result="OK",
                        detail=f"HMTP submit → node {remote} · {to}")
        self.compose["subj"] = ""
        self.compose["body"] = ""
        self.mail_folder = "sent"
        self.mail_sel = 0
        self.changed.emit("mail")

    def _poll_hfpop_live(self) -> None:
        self._ensure_clients()
        remote = self.controller.remote_id
        try:
            self._hfpop.retrieve(remote)   # RETR — pede as mensagens ao par
            self.live_tx += 1
            self._log_event("S_UNIDATA_REQUEST", sap=HFPOP_SAP, src="local",
                            dst=f"·{remote:03d}", result="OK",
                            detail=f"HFPOP RETR → node {remote}")
        except Exception as exc:
            self.live_rejected += 1
            self._log_event("S_UNIDATA_REQUEST_REJECTED", sap=HFPOP_SAP, src="local",
                            result="ERROR", detail=f"HFPOP: {exc}")
        self.mail_folder = "inbox"
        self.mail_sel = 0
        self.changed.emit("mail")

    def _handle_mail_rx(self, raw: bytes, src: int) -> None:
        """Inbound HMTP submission (SAP 3): parse into a mail-object."""
        self._ensure_clients()
        try:
            self._hmtp_server._on_data_received(src, raw)   # → _on_mail_received
        except Exception:
            pass
        self.changed.emit("mail")

    def _handle_pop_rx(self, raw: bytes, src: int) -> None:
        """Inbound HFPOP data (SAP 4): a server reply → retrieved messages."""
        self._ensure_clients()
        try:
            self._hfpop._on_data_received(src, raw)   # → _on_pop_message (if RETR body)
        except Exception:
            pass
        self.changed.emit("mail")

    def _on_mail_received(self, msg) -> None:
        subj, body = self._split_subject(msg.body)
        src_email = msg.sender or "unknown@peer.s5066"
        who = src_email.split("@")[-1].split(".")[0].upper() or "PEER"
        self.live_inbox.insert(0, {
            "id": f"rx{len(self.live_inbox)}", "from": who, "addr": src_email,
            "node": "—", "subj": subj or "(no subject)", "time": _now()[:5],
            "size": _fmt_size(len(msg.body.encode("utf-8", "replace"))),
            "unread": True, "mime": "text/plain", "body": body})

    def _on_pop_message(self, msg_number: int, body: str) -> None:
        subj, text = self._split_subject(body)
        self.live_inbox.insert(0, {
            "id": f"pop{len(self.live_inbox)}", "from": "MAILDROP",
            "addr": "hfpop@peer.s5066", "node": "—",
            "subj": subj or f"Message {msg_number}", "time": _now()[:5],
            "size": _fmt_size(len(body.encode("utf-8", "replace"))),
            "unread": True, "mime": "text/plain", "body": text})

    @staticmethod
    def _split_subject(raw: str) -> tuple:
        """Split an RFC-822-ish body into (subject, body); tolerant of \\r\\n / \\n."""
        text = (raw or "").replace("\r\n", "\n")
        subj = ""
        lines = text.split("\n")
        i = 0
        while i < len(lines) and lines[i].strip():
            if lines[i].lower().startswith("subject:"):
                subj = lines[i].split(":", 1)[1].strip()
            i += 1
        body = "\n".join(lines[i + 1:]) if i < len(lines) else ""
        return subj, (body or text).strip()

    # ---- IP Client (SAP 9): craft/send a test datagram, RX decode ----
    def send_ip_test(self) -> None:
        """Send a minimal IPv4 datagram to the peer via the Annex F IP client."""
        if not (self.live and self.controller is not None and self.controller.running):
            return
        self._ensure_clients()
        local = self.controller.local_id
        remote = self.controller.remote_id
        dst_ip = f"10.66.0.{remote}"
        datagram = self._make_ip_datagram(local, remote, b"PING from Subnet Console")
        info = self._parse_ip(datagram)
        ok = False
        try:
            ok = self._ip.send_ip_datagram(datagram)   # unicast → ARQ (SAP 9)
        except Exception:
            ok = False
        if ok:
            self.ip_tx += 1
            self.live_tx += 1
            self._log_ip(f"10.66.0.{local}", dst_ip, info, "SENT")
            self._log_event("S_UNIDATA_REQUEST", sap=IP_SAP, src="local",
                            dst=f"·{remote:03d}", size=f"{info['length']} B", result="OK",
                            detail=f"IP {info['proto']} → {dst_ip} · {info['length']} B")
        else:
            self.ip_dropped += 1
            self.live_rejected += 1
            self._log_ip(f"10.66.0.{local}", dst_ip, info, "DROPPED")
        self.changed.emit("ipclient")

    def _handle_ip_rx(self, raw: bytes, src: int) -> None:
        self._ensure_clients()
        try:
            self._ip._on_data_received(src, raw)   # valida + → _on_ip_received
        except Exception:
            pass

    def _on_ip_received(self, data: bytes, src_addr: int) -> None:
        self.ip_rx += 1
        info = self._parse_ip(data)
        self._log_ip(info["src"], info["dst"], info, "RECV")
        self.changed.emit("ipclient")

    def _log_ip(self, src_ip: str, dst_ip: str, info: dict, result: str) -> None:
        mode = "non-ARQ" if info["multicast"] else "ARQ"
        self.ip_events.insert(0, {
            "time": time.strftime("%H:%M:%S"), "src": src_ip, "dst": dst_ip,
            "proto": info["proto"], "len": f"{info['length']} B", "mode": mode,
            "result": result})
        del self.ip_events[120:]

    @staticmethod
    def _make_ip_datagram(local: int, remote: int, payload: bytes) -> bytes:
        """Craft a minimal valid IPv4 datagram 10.66.0.<local> → 10.66.0.<remote>."""
        total = 20 + len(payload)
        h = bytearray(20)
        h[0] = 0x45                       # version 4, IHL 5 (20 bytes)
        h[1] = 0x00                       # DSCP/TOS 0 → routine, ARQ (unicast)
        h[2:4] = total.to_bytes(2, "big")
        h[6] = 0x00                       # flags/fragment: no DF, offset 0
        h[8] = 64                         # TTL
        h[9] = 6                          # protocol: TCP
        h[12:16] = bytes([10, 66, 0, local & 0xFF])
        h[16:20] = bytes([10, 66, 0, remote & 0xFF])
        return bytes(h) + payload

    @staticmethod
    def _parse_ip(data: bytes) -> dict:
        """Pull src/dst/proto/length from an IPv4 header for the datagram log."""
        proto_names = {1: "ICMP", 6: "TCP", 17: "UDP"}
        if len(data) < 20:
            return {"src": "—", "dst": "—", "proto": "IP", "length": len(data),
                    "multicast": False}
        length = int.from_bytes(data[2:4], "big") or len(data)
        src = ".".join(str(b) for b in data[12:16])
        dst = ".".join(str(b) for b in data[16:20])
        return {"src": src, "dst": dst, "proto": proto_names.get(data[9], str(data[9])),
                "length": length, "multicast": (data[16] & 0xF0) == 0xE0}

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

    # ============================================================= ALE radio
    # Remote-control protocol (docs/PROTOCOLO-CONTROLE-REMOTO.md). Two repaint
    # topics keep the screen usable while STATE streams at ~5 Hz:
    #   "radio"      — structural rebuild (controls, mode, channel table, status);
    #                  fired only when a config/mode/link field changes.
    #   "radio_tele" — live-display refresh (telemetry readouts, scan, LQA, log,
    #                  AMD); fired on every STATE (throttled) and table update, so
    #                  the fast analog numbers refresh without touching the inputs.
    def _ale_structural_key(self, st: dict) -> tuple:
        return (st.get("fsm"), st.get("link_peer"), st.get("self_addr"),
                st.get("sideband"), st.get("scan_rate"), st.get("tx_power_dbm"),
                st.get("occupancy_detect"), st.get("twa_max"), st.get("tcc_max"),
                st.get("tm_max"), st.get("forced"), st.get("active_service"),
                st.get("scanning"))

    def _paint_tele(self) -> None:
        now = time.monotonic()
        if now - self._ale_tele_paint < 0.25:
            return
        self._ale_tele_paint = now
        self.changed.emit("radio_tele")
        self.changed.emit("toolbar")

    def on_ale_state(self, st: dict) -> None:
        self._ale_state = st
        key = self._ale_structural_key(st)
        if key != self._ale_struct_key:
            self._ale_struct_key = key
            self.changed.emit("radio")
            self.changed.emit("toolbar")
        self._paint_tele()

    def on_ale_channels(self, ch: list) -> None:
        self._ale_channels = ch
        self.changed.emit("radio")      # channel table is structural
        self.changed.emit("toolbar")    # current-frequency label reads the table

    def on_ale_scan(self, rows: list) -> None:
        self._ale_scan = rows
        self.changed.emit("radio_tele")

    def on_ale_lqa(self, lqa: dict) -> None:
        self._ale_lqa = lqa
        self.changed.emit("radio_tele")

    def on_ale_sound_hist(self, rows: list) -> None:
        self._ale_sound_hist = rows
        self.changed.emit("radio_tele")

    def on_ale_log(self, entry: dict) -> None:
        self._ale_log.insert(0, dict(entry))
        del self._ale_log[200:]
        self.changed.emit("radio_tele")

    def on_ale_amd(self, msg: dict) -> None:
        self._ale_amd.insert(0, dict(msg))
        del self._ale_amd[50:]
        self.changed.emit("radio_tele")

    def on_ale_conn(self, up: bool) -> None:
        self._ale_reachable = bool(up)
        if not up:   # backend went silent → fall back to an offline snapshot
            self._ale_state = dict(ALE_STATE_DEFAULT)
            self._ale_struct_key = ()
        self.changed.emit("radio")
        self.changed.emit("toolbar")
        self.changed.emit("statusbar")

    # ---- data sources (live vs demo) ----
    def _radio_state(self) -> dict:
        return self._ale_state if self.ale_live else self._ale_demo["state"]

    def _radio_channels(self) -> list:
        return self._ale_channels if self.ale_live else self._ale_demo["channels"]

    def _radio_scan(self) -> list:
        return self._ale_scan if self.ale_live else self._ale_demo["scan"]

    def _radio_lqa(self) -> dict:
        return self._ale_lqa if self.ale_live else self._ale_demo["lqa"]

    def _radio_sound_hist(self) -> list:
        return self._ale_sound_hist if self.ale_live else self._ale_demo["sound_hist"]

    def _radio_log(self) -> list:
        return self._ale_log if self.ale_live else self._ale_demo["log"]

    def _radio_amd(self) -> list:
        return self._ale_amd if self.ale_live else self._ale_demo["amd"]

    def _radio_online(self) -> bool:
        return (not self.ale_live) or self._ale_reachable

    # ---- colours ----
    def _lqa_color(self, v) -> str:
        if v is None or v < 0 or v >= 31:
            return T.FG_GHOST
        if v >= 22:
            return T.GREEN_DARK
        if v >= 12:
            return T.AMBER
        return T.RED

    def _occ_color(self, occ) -> str:
        return {0: T.FG_GHOST, 1: T.GREEN_DARK, 2: T.AMBER, 3: T.RED}.get(occ, T.FG_GHOST)

    # ---- accessors ----
    def radio_status(self) -> dict:
        t = self.theme
        st = self._radio_state()
        if not self._radio_online():
            return {"label": "RADIO OFFLINE", "fg": T.RED_DARK, "bg": T.RED_BG,
                    "border": T.RED_BORDER, "dot": T.RED, "halo": "#f0cfc9"}
        if st.get("linked"):
            return {"label": f"LINKED · {st.get('link_peer') or '—'}", "fg": T.GREEN_DARK,
                    "bg": T.GREEN_BG, "border": T.GREEN_BORDER, "dot": T.GREEN, "halo": T.GREEN_HALO}
        if st.get("fsm") == 1:
            return {"label": "LINKING", "fg": T.AMBER, "bg": T.AMBER_BG, "border": "#e3cfa0",
                    "dot": T.AMBER, "halo": "#f0e2c0"}
        if st.get("forced"):
            return {"label": "FORCED", "fg": T.AMBER, "bg": T.AMBER_BG, "border": "#e3cfa0",
                    "dot": T.AMBER, "halo": "#f0e2c0"}
        if st.get("scanning"):
            return {"label": "SCANNING", "fg": t.accent, "bg": t.accent_note_bg,
                    "border": t.accent_note_border, "dot": t.accent, "halo": t.accent_note_bg}
        return {"label": "AVAILABLE", "fg": T.FG_MUTED, "bg": "#eef1f4",
                "border": T.INPUT_BORDER, "dot": "#9aa0a6", "halo": "#e3e6ea"}

    def rf_readouts(self) -> dict:
        """Toolbar FREQ/MODE/RATE/SNR — overridden by live ALE telemetry."""
        n = self.node
        out = {"freq": n["freq"], "mode": n["waveform"], "rate": n["dataRate"],
               "snr": n["snr"], "snr_color": T.GREEN_DARK}
        if not self.ale_live:
            return out
        st = self._ale_state
        chans = self._ale_channels
        cur = st.get("cur_channel", -1)
        if isinstance(cur, int) and 0 <= cur < len(chans) and chans[cur].get("freq"):
            out["freq"] = f"{chans[cur]['freq']} MHz"
        elif not self._ale_reachable:
            out["freq"] = "— MHz"
        sb = st.get("sideband_name")
        if sb:
            out["mode"] = f"ALE {sb}"
        sinad = st.get("sinad")
        if isinstance(sinad, int) and sinad >= 0:
            out["snr"] = f"{sinad} dB"
        elif self.ale_live:
            out["snr"] = "—"
            out["snr_color"] = T.FG_GHOST
        return out

    def radio_view(self) -> dict:
        t = self.theme
        a = t.accent
        st = self._radio_state()
        chans = self._radio_channels()
        scan = self._radio_scan()
        online = self._radio_online()
        cur = st.get("cur_channel", -1)
        cur_ch = chans[cur] if isinstance(cur, int) and 0 <= cur < len(chans) else None

        def sig(v):     # SINAD/BER: doc §5 "<0 = sem medida"
            return "—" if (v is None or (isinstance(v, int) and v < 0)) else str(v)

        def lvl(v):     # RSSI/NOISE dBm — negative is normal; blank when offline
            return str(v) if (online and v is not None) else "—"

        vswr = st.get("tx_vswr") or 0.0
        vswr_color = (T.RED if vswr >= 2.0 else T.AMBER if vswr > 1.5 else T.GREEN_DARK) if vswr else T.FG_GHOST
        kpis = [
            {"label": "FREQUENCY", "value": cur_ch["freq"] if cur_ch else "—", "unit": "MHz"},
            {"label": "TX POWER", "value": str(st.get("tx_power_dbm")) if st.get("tx_power_dbm") else "—",
             "unit": "dBm"},
            {"label": "VSWR", "value": f"{vswr:.2f}" if vswr else "—",
             "delta": "match ok" if vswr and vswr <= 1.5 else ("high" if vswr else ""),
             "delta_color": vswr_color},
            {"label": "SINAD", "value": sig(st.get("sinad")), "unit": "dB"},
            {"label": "BER", "value": sig(st.get("ber"))},
            {"label": "RSSI", "value": lvl(st.get("rssi")), "unit": "dBm"},
        ]
        readouts = [
            ("NOISE", f"{lvl(st.get('noise'))}" + (" dBm" if online and st.get("noise") is not None else "")),
            ("FWD PWR", f"{st.get('tx_power', 0):.1f} {st.get('tx_power_unit') or ''}".strip()
             if st.get("tx_active") or st.get("tx_power") else "—"),
            ("REFL PWR", f"{st.get('tx_refl', 0):.1f} {st.get('tx_refl_unit') or ''}".strip()
             if st.get("tx_active") or st.get("tx_refl") else "—"),
            ("Twa", f"{st.get('twa_remain', 0)} / {st.get('twa_max', 0)} s"),
            ("Tcc / Tm", f"{st.get('tcc_max', 0)} / {st.get('tm_max', 0)} s"),
            ("FRAMES RX", str(st.get("frames_rx", 0))),
            ("WORDS OK", str(st.get("words_valid", 0))),
            ("SOUNDS RX", str(st.get("sounds_rx", 0))),
        ]

        # channel table merged with per-channel scan occupancy/quality
        channels_view = []
        for c in chans:
            i = c.get("idx")
            sc = scan[i] if isinstance(i, int) and i < len(scan) else None
            lqa_v = sc["lqa"] if sc else 31
            occ = sc["occ"] if sc else 0
            channels_view.append({
                "idx": i, "freq": c.get("freq", ""), "name": c.get("name", ""),
                "band": c.get("band", ""), "current": (i == cur),
                "lqa": lqa_v, "lqa_txt": str(lqa_v) if 0 <= lqa_v < 31 else "—",
                "lqa_color": self._lqa_color(lqa_v),
                "occ_name": OCC_NAMES.get(occ, "?"), "occ_color": self._occ_color(occ),
            })

        # LQA matrix (peers × channels)
        lqa = self._radio_lqa()
        ncol = min(int(lqa.get("n_channels", 0) or len(chans)), len(chans), 16)
        lqa_labels = [chans[i]["freq"] for i in range(ncol)] if chans else []
        lqa_peers = []
        for p in lqa.get("peers", []):
            cells = []
            for j in range(ncol):
                v = p["lqa"][j] if j < len(p["lqa"]) else 31
                cells.append({"txt": str(v) if 0 <= v < 31 else "·", "color": self._lqa_color(v)})
            lqa_peers.append({"addr": p.get("addr", "?"), "online": p.get("online", True), "cells": cells})

        sound_hist = [{"t": r.get("t", ""), "ch": r.get("ch", -1), "q": r.get("q", 0),
                       "q_color": self._lqa_color(r.get("q", 0)), "ack": r.get("ack", "—")}
                      for r in self._radio_sound_hist()]

        LOGCOL = {"RX": T.GREEN_DARK, "TX": a, "SYS": T.FG_MUTED, "SND": T.PURPLE,
                  "LQA": T.AMBER, "ERR": T.RED}
        log = [{"t": e.get("t", ""), "kind": e.get("kind_name", ""),
                "color": LOGCOL.get(e.get("kind_name", ""), T.FG_MUTED), "text": e.get("text", "")}
               for e in self._radio_log()]
        amd = [{"from": m.get("from", "?"), "t": m.get("t", ""), "read": m.get("read", False),
                "text": m.get("text", "")} for m in self._radio_amd()]

        return {
            "accent": a, "online": online, "live": self.ale_live,
            "status": self.radio_status(),
            "self_addr": st.get("self_addr") or "—", "link_peer": st.get("link_peer") or "",
            "linked": bool(st.get("linked")), "fsm_name": st.get("fsm_name", ""),
            "forced": bool(st.get("forced")), "scanning": bool(st.get("scanning")),
            "active_service": st.get("active_service") or "",
            "kpis": kpis, "readouts": readouts,
            # control states
            "tx_power_dbm": st.get("tx_power_dbm", 0),
            "power_steps": [{"n": n, "active": n == st.get("tx_power_dbm")} for n in RF_POWER_STEPS],
            "sideband": st.get("sideband", 0),
            "sidebands": [{"v": v, "name": nm, "active": v == st.get("sideband")}
                          for v, nm in SIDEBAND_NAMES.items()],
            "scan_rate": st.get("scan_rate", 0),
            "scan_rates": [{"n": n, "active": n == st.get("scan_rate")} for n in (2, 5, 10)],
            "occupancy": bool(st.get("occupancy_detect")),
            "twa_max": st.get("twa_max", 0), "twa_remain": st.get("twa_remain", 0),
            "sound_mode": self.ale_sound_mode,
            "sound_modes": [{"v": v, "name": nm, "active": v == self.ale_sound_mode}
                            for v, nm in SOUND_MODE_NAMES.items()],
            "force_service": self.ale_force_service,
            "force_services": [{"v": s, "active": s == self.ale_force_service}
                               for s in ("am", "fm", "110")],
            # drafts
            "call_addr": self.ale_call_addr, "call_channel": self.ale_call_channel,
            "group_members": self.ale_group_members, "net_id": self.ale_net_id,
            "amd_dest": self.ale_amd_dest, "amd_text": self.ale_amd_text,
            "chedit_idx": self.ale_chedit_idx, "chedit_freq": self.ale_chedit_freq,
            "chedit_name": self.ale_chedit_name, "twa_draft": self.ale_twa_draft,
            # tables
            "channels": channels_view, "lqa_labels": lqa_labels, "lqa_peers": lqa_peers,
            "sound_hist": sound_hist, "log": log, "amd": amd,
        }

    # ---- silent draft setters (no rebuild — keep focus while typing) ----
    def set_ale_call_addr(self, v: str) -> None:
        self.ale_call_addr = v[:15]

    def set_ale_call_channel(self, v: str) -> None:
        self.ale_call_channel = "".join(c for c in v if c.isdigit() or c == "-")[:4]

    def set_ale_group(self, v: str) -> None:
        self.ale_group_members = v

    def set_ale_net(self, v: str) -> None:
        self.ale_net_id = v[:15]

    def set_ale_amd_dest(self, v: str) -> None:
        self.ale_amd_dest = v[:15]

    def set_ale_amd_text(self, v: str) -> None:
        self.ale_amd_text = v[:91]

    def set_ale_chedit_idx(self, v: str) -> None:
        self.ale_chedit_idx = "".join(c for c in v if c.isdigit())[:2]

    def set_ale_chedit_freq(self, v: str) -> None:
        self.ale_chedit_freq = v[:7]

    def set_ale_chedit_name(self, v: str) -> None:
        self.ale_chedit_name = v[:11]

    def set_ale_twa(self, v: str) -> None:
        self.ale_twa_draft = "".join(c for c in v if c.isdigit())[:5]

    # ---- demo helpers ----
    def _ale_demo_log(self, kind: int, name: str, text: str) -> None:
        self._ale_demo["log"].insert(0, {"kind": kind, "kind_name": name, "t": _now(), "text": text})
        del self._ale_demo["log"][200:]

    def _parse_call_channel(self) -> int:
        s = (self.ale_call_channel or "").strip()
        try:
            return int(s)
        except ValueError:
            return -1

    # ---- commands ----
    def ale_call(self) -> None:
        addr = (self.ale_call_addr or "").strip()
        if not addr:
            return
        ch = self._parse_call_channel()
        if self.ale_live and self.ale is not None:
            self.ale.send_call(addr, ch)
        else:
            d = self._ale_demo["state"]
            d.update(fsm=2, fsm_name="LINKED", linked=True, link_peer=addr, scanning=False)
            if 0 <= ch < len(self._ale_demo["channels"]):
                d["cur_channel"] = ch
            self._ale_demo_log(1, "TX", f"CALL {addr}" + (f" ch{ch}" if ch >= 0 else " (auto)"))
            self.changed.emit("radio")
            self.changed.emit("toolbar")

    def ale_group_call(self) -> None:
        members = [m for m in (self.ale_group_members or "").replace(",", " ").split() if m][:5]
        if not members:
            return
        if self.ale_live and self.ale is not None:
            self.ale.send_group(members)
        else:
            self._ale_demo["state"].update(fsm=3, fsm_name="GROUP_LINKED", linked=True,
                                           link_peer=members[0], scanning=False)
            self._ale_demo_log(1, "TX", f"GROUP CALL {' '.join(members)}")
            self.changed.emit("radio")

    def ale_net_call(self) -> None:
        netid = (self.ale_net_id or "").strip()
        if not netid:
            return
        if self.ale_live and self.ale is not None:
            self.ale.send_net(netid)
        else:
            self._ale_demo["state"].update(fsm=4, fsm_name="NET_LINKED", linked=True,
                                           link_peer=netid, scanning=False)
            self._ale_demo_log(1, "TX", f"NET CALL {netid}")
            self.changed.emit("radio")

    def ale_terminate(self) -> None:
        if self.ale_live and self.ale is not None:
            self.ale.send_term("")
        else:
            self._ale_demo["state"].update(fsm=0, fsm_name="AVAILABLE", linked=False,
                                           link_peer="", forced=False, scanning=True)
            self._ale_demo_log(2, "SYS", "Link terminated · scan resumed")
            self.changed.emit("radio")
            self.changed.emit("toolbar")

    def ale_sound(self) -> None:
        if self.ale_live and self.ale is not None:
            self.ale.send_sound(self.ale_sound_mode)
        else:
            st = self._ale_demo["state"]
            self._ale_demo["sound_hist"].insert(0, {"t": _now(), "ch": st.get("cur_channel", -1),
                                                    "q": 0, "ack": "—"})
            del self._ale_demo["sound_hist"][8:]
            self._ale_demo_log(3, "SND", f"Sounding ({SOUND_MODE_NAMES.get(self.ale_sound_mode, '?')})")
            self.changed.emit("radio")
            self.changed.emit("radio_tele")

    def set_ale_sound_mode(self, v: int) -> None:
        self.ale_sound_mode = int(v)
        self.changed.emit("radio")

    def ale_send_amd(self) -> None:
        text = (self.ale_amd_text or "").strip()
        if not text:
            return
        dest = (self.ale_amd_dest or "").strip()
        if self.ale_live and self.ale is not None:
            self.ale.send_amd(dest, text)
        else:
            self._ale_demo_log(1, "TX", f"AMD→{dest or 'link'}: {text}")
        self.ale_amd_text = ""
        self.changed.emit("radio")

    def ale_set_tx_power(self, dbm: int) -> None:
        if self.ale_live and self.ale is not None:
            self.ale.send_config(tx_power_dbm=int(dbm))
        else:
            self._ale_demo["state"].update(tx_power_dbm=int(dbm),
                                           tx_power=round(10 ** (int(dbm) / 10) / 1000.0, 1))
            self._ale_demo_log(2, "SYS", f"TX power → {dbm} dBm")
            self.changed.emit("radio")
            self.changed.emit("toolbar")

    def ale_set_sideband(self, sb: int) -> None:
        if self.ale_live and self.ale is not None:
            self.ale.send_config(sideband=int(sb))
        else:
            self._ale_demo["state"].update(sideband=int(sb),
                                           sideband_name=SIDEBAND_NAMES.get(int(sb), str(sb)))
            self._ale_demo_log(2, "SYS", f"Sideband → {SIDEBAND_NAMES.get(int(sb), sb)}")
            self.changed.emit("radio")
            self.changed.emit("toolbar")

    def ale_set_scan_rate(self, n: int) -> None:
        if self.ale_live and self.ale is not None:
            self.ale.send_config(scan_rate=int(n))
        else:
            self._ale_demo["state"].update(scan_rate=int(n))
            self._ale_demo_log(2, "SYS", f"Scan rate → {n} ch/s")
            self.changed.emit("radio")

    def ale_set_occupancy(self, on: bool) -> None:
        if self.ale_live and self.ale is not None:
            self.ale.send_config(occupancy_detect=1 if on else 0)
        else:
            self._ale_demo["state"].update(occupancy_detect=bool(on))
            self._ale_demo_log(2, "SYS", f"Occupancy detect → {'ON' if on else 'OFF'}")
            self.changed.emit("radio")

    def ale_apply_twa(self) -> None:
        s = (self.ale_twa_draft or "").strip()
        if not s:
            return
        val = int(s)
        if self.ale_live and self.ale is not None:
            self.ale.send_config(twa_s=val)
        else:
            self._ale_demo["state"].update(twa_max=val, twa_remain=val)
            self._ale_demo_log(2, "SYS", f"Twa → {val} s")
            self.changed.emit("radio")
        self.ale_twa_draft = ""

    def set_ale_force_service(self, v: str) -> None:
        self.ale_force_service = v
        self.changed.emit("radio")

    def ale_park(self) -> None:
        """FORCE_LINK: park the channel in the Channel field (manual mode)."""
        ch = self._parse_call_channel()
        st = self._radio_state()
        if ch < 0:
            ch = st.get("cur_channel", 0)
            if ch < 0:
                ch = 0
        if self.ale_live and self.ale is not None:
            self.ale.send_force_link(ch, True, self.ale_force_service)
        else:
            self._ale_demo["state"].update(forced=True, scanning=False, cur_channel=ch,
                                           active_service=self.ale_force_service)
            self._ale_demo_log(2, "SYS", f"FORCED on ch{ch} ({self.ale_force_service})")
            self.changed.emit("radio")
            self.changed.emit("toolbar")

    def ale_normal(self) -> None:
        """FORCE_LINK forced=0 — leave manual mode, resume scan/sounding."""
        st = self._radio_state()
        ch = st.get("cur_channel", 0)
        if ch < 0:
            ch = 0
        if self.ale_live and self.ale is not None:
            self.ale.send_force_link(ch, False, self.ale_force_service)
        else:
            self._ale_demo["state"].update(forced=False, scanning=True, active_service="")
            self._ale_demo_log(2, "SYS", "NORMAL · scan resumed")
            self.changed.emit("radio")

    def ale_chedit_apply(self) -> None:
        s = (self.ale_chedit_idx or "").strip()
        if not s:
            return
        idx = int(s)
        freq = (self.ale_chedit_freq or "").strip()
        name = (self.ale_chedit_name or "").strip()
        if self.ale_live and self.ale is not None:
            self.ale.send_chedit(idx, freq, name)
        else:
            for c in self._ale_demo["channels"]:
                if c["idx"] == idx:
                    if freq:
                        c["freq"] = freq
                    if name:
                        c["name"] = name
                    break
            self._ale_demo_log(2, "SYS", f"CHEDIT ch{idx} {freq or ''} {name or ''}".strip())
            self.changed.emit("radio")
            self.changed.emit("toolbar")

    def ale_prefill_channel(self, idx: int) -> None:
        """Channel-row click: seed the Call/Edit forms with this channel."""
        self.ale_call_channel = str(idx)
        self.ale_chedit_idx = str(idx)
        for c in self._radio_channels():
            if c.get("idx") == idx:
                self.ale_chedit_freq = c.get("freq", "")
                self.ale_chedit_name = c.get("name", "")
                break
        self.changed.emit("radio")

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
        if self.live and self.controller is not None:
            self._poll_hfpop_live()
            return
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
        if self.live and self.controller is not None:
            self._submit_mail_live()
            return
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
    def _ip_bound(self) -> bool:
        return bool(self.controller is not None and IP_SAP in self.controller.bound_saps)

    def ip_kpis(self) -> list:
        gd = T.GREEN_DARK
        if self.live:
            s = self._live_status
            connected = bool(s.get("connected"))
            mtu = getattr(self.controller, "max_user_data_bytes", 128) if self.controller else 128
            return [
                {"label": "Datagrams TX", "value": str(self.ip_tx), "unit": "pkt",
                 "delta": f"SAP 9 · {'ARQ up' if connected else 'no link'}",
                 "delta_color": gd if connected else T.FG_DIM},
                {"label": "Datagrams RX", "value": str(self.ip_rx), "unit": "pkt",
                 "delta": "reassembled" if self.ip_rx else "none yet",
                 "delta_color": gd if self.ip_rx else T.FG_DIM},
                {"label": "Dropped", "value": str(self.ip_dropped), "unit": "pkt",
                 "delta": "no route / MTU", "delta_color": T.RED if self.ip_dropped else T.FG_DIM},
                {"label": "Link MTU", "value": str(mtu), "unit": "oct",
                 "delta": "max U-PDU / frame", "delta_color": gd},
            ]
        return [
            {"label": "Datagrams TX", "value": "5 902", "unit": "pkt", "delta": "1.21 MB"},
            {"label": "Datagrams RX", "value": "1 339", "unit": "pkt", "delta": "402 KB"},
            {"label": "Dropped", "value": "12", "unit": "pkt", "delta": "no link / TTL", "delta_color": T.RED},
            {"label": "Path MTU", "value": "1280", "unit": "bytes", "delta": "PMTU RFC1191", "delta_color": T.GREEN_DARK},
        ]

    def ip_bind(self) -> list:
        if self.live:
            local = self.controller.local_id if self.controller is not None else 0
            remote = self.controller.remote_id if self.controller is not None else 0
            return [
                {"k": "HF NODE ADDRESS", "v": self.node["address"]},
                {"k": "LOCAL IP", "v": f"10.66.0.{local} / 24"},
                {"k": "PEER IP", "v": f"10.66.0.{remote}"},
                {"k": "SAP ID", "v": "9 · IP Client · MANDATORY"},
                {"k": "BINDING", "v": "BOUND · S_BIND_ACCEPT" if self._ip_bound() else "UNBOUND"},
                {"k": "QoS MODE", "v": "DSCP · DiffServ (Table 9)"},
            ]
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
        if self.live:
            remote = self.controller.remote_id if self.controller is not None else 0
            connected = bool(self._live_status.get("connected"))
            running = bool(self._live_status.get("running"))
            peer_st = "UP" if connected else ("IDLE" if running else "DOWN")
            raw = [
                (f"10.66.0.{remote}/32", f"3.066.000.{remote:03d}", "ARQ", gd, gb,
                 peer_st, f"NODE {remote}"),
                ("239.0.0.1/32", "broadcast", "non-ARQ ×2", T.FG_MUTED, "#eceef1",
                 "UP" if running else "DOWN", "NET-ALL group"),
            ]
        else:
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
                        "st_fg": T.GREEN_DARK if st == "UP" else (T.AMBER if st in ("DEGRADED", "IDLE") else T.FG_GHOST2),
                        "dot": T.GREEN if st == "UP" else (T.AMBER if st in ("DEGRADED", "IDLE") else T.RED)})
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
        if self.live:
            out = []
            for e in self.ip_events:
                ok = e["result"] in ("SENT", "RECV", "CONFIRMED")
                out.append({**e,
                            "proto_color": T.AMBER if e["proto"] == "ICMP" else (T.PURPLE if e["proto"] == "UDP" else a),
                            "res_color": T.GREEN_DARK if ok else (T.AMBER if e["result"] == "QUEUED" else T.RED)})
            return out
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
    def sk_status(self) -> dict:
        """Header status: is the F.16 socket server listening, and where."""
        if self.live:
            s = self._live_status
            running = bool(s.get("sis_server_running"))
            host = s.get("sis_server_host") or self.controller.sis_host
            port = s.get("sis_server_port")
            return {"listening": running,
                    "label": f"LISTENING · {host}:{port}" if running else "SERVER OFFLINE",
                    "color": T.GREEN_DARK if running else T.RED,
                    "dot": T.GREEN if running else T.RED}
        return {"listening": True, "label": "LISTENING · 127.0.0.1:5066",
                "color": T.GREEN_DARK, "dot": T.GREEN}

    def sk_kpis(self) -> list:
        gd = T.GREEN_DARK
        if self.live:
            s = self._live_status
            clients = s.get("sis_clients") or []
            bound = sum(1 for c in clients if c["state"] == "BOUND")
            mx = int(s.get("sis_max_clients") or 16)
            running = bool(s.get("sis_server_running"))
            return [
                {"label": "TCP Connections", "value": str(len(clients)), "unit": f"of {mx}",
                 "delta": f"{bound} bound · {len(clients) - bound} idle"},
                {"label": "Primitives", "value": str(int(s.get("sis_prim_count") or 0)),
                 "unit": "msg", "delta": "req + ind + confirm"},
                {"label": "Bound Sockets", "value": str(bound), "unit": "SAPs",
                 "delta": "via Raw SIS" if bound else "none yet",
                 "delta_color": gd if bound else T.FG_DIM},
                {"label": "Server", "value": "UP" if running else "DOWN", "unit": "",
                 "delta": "listening" if running else "offline",
                 "delta_color": gd if running else T.RED},
            ]
        return [
            {"label": "TCP Connections", "value": "5", "unit": "of 16", "delta": "4 bound · 1 idle"},
            {"label": "Primitives / s", "value": "11", "unit": "msg", "delta": "req + ind + confirm"},
            {"label": "Socket Throughput", "value": "7.3", "unit": "KB/s", "delta": "↑ steady", "delta_color": T.GREEN_DARK},
            {"label": "Server Uptime", "value": "02:14", "unit": "h:m", "delta": "since 12:08 UTC"},
        ]

    def sk_server(self) -> list:
        if self.live:
            s = self._live_status
            host = s.get("sis_server_host") or self.controller.sis_host
            port = s.get("sis_server_port")
            return [
                {"k": "BIND ADDRESS", "v": str(host)},
                {"k": "TCP PORT", "v": str(port) if port else "—"},
                {"k": "MAX CLIENTS", "v": str(int(s.get("sis_max_clients") or 16))},
                {"k": "MESSAGE FRAMING", "v": "SIS wrapper (0x90EB)"},
                {"k": "PROTOCOL", "v": "Annex A.2.2 binary"},
                {"k": "BYTE ORDER", "v": "big-endian / MSB"},
            ]
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
        if self.live:
            out = []
            for c in self._live_status.get("sis_clients") or []:
                bound = c["state"] == "BOUND"
                sap = c.get("sap")
                sap_str = str(sap) if sap is not None else "—"
                name = SAP_NAMES.get(sap, f"SAP {sap}") if bound else "unbound (handshake)"
                out.append({"id": f"#{c['conn_id']}", "remote": c["remote"], "client": name,
                            "sap": sap_str, "rank": str(c["rank"]) if bound else "—",
                            "st": c["state"], "since": c["since"],
                            "st_fg": T.GREEN_DARK if bound else T.AMBER,
                            "st_bg": T.GREEN_BG if bound else T.AMBER_BG,
                            "sap_bg": "#c9ccd1" if sap_str == "—" else t.sap_color(sap_str),
                            "client_fg": T.FG_BODY if bound else T.FG_DIM})
            return out
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

        def color(name):
            if "REJECT" in name:
                return T.RED
            if "ACCEPT" in name or "CONFIRM" in name or "AVAIL" in name or "ESTABLISHED" in name:
                return T.GREEN_DARK
            return T.FG_BODY

        if self.live:
            out = []
            for w in self._live_status.get("sis_wire") or []:
                out.append({"time": w["time"], "dir": w["dir"], "name": w["name"],
                            "sap": w["sap"], "size": f"{w['size']} B",
                            "dir_fg": a if w["dir"] == "C → S" else T.PURPLE,
                            "color": color(w["name"])})
            return out
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
        return [{"time": tm, "dir": d, "name": nm, "sap": sap, "size": sz,
                 "dir_fg": a if d == "C → S" else T.PURPLE, "color": color(nm)}
                for tm, d, nm, sap, sz in raw]

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
        # Fatia 6 (live): as caixas passam a ser o tráfego HMTP/HFPOP real — inbox
        # são os mail-objects recebidos, sent os submetidos por este nó.
        if self.live:
            inbox = list(self.live_inbox)
            sent = list(self.live_sent)
            outbox = list(self.live_outbox)
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
