"""AleController — a Qt-friendly UDP client for the ALE 2G radio-control protocol.

Speaks the modem/radio remote-control protocol described in
``docs/PROTOCOLO-CONTROLE-REMOTO.md`` on UDP ``:54001``: it announces itself with
``HELLO`` keepalives (~1 Hz), sends operator commands (CALL, TERM, SOUND, CONFIG,
FORCE_LINK, CHEDIT, …) and decodes the fan-out telemetry (STATE ~5 Hz plus the
CHANNELS / SCAN / LQA / SOUND_HIST / LOG / AMD tables and events).

Threading model (mirrors :class:`~.node_controller.NodeController`):
- A background daemon thread runs the blocking ``recv`` loop; every datagram is
  decoded and re-emitted as a Qt signal, delivered to the GUI thread by a queued
  connection. The RX thread never touches widgets.
- The keepalive (a GUI-thread ``QTimer``) and every command run on the GUI
  thread. ``sendto``/``recv`` on one connected UDP socket from two threads is fine
  on CPython, and a single socket keeps the source port fixed so the backend does
  not lose us as a learned peer.

This is *control + telemetry only*: audio/voice paths are out of scope for the
protocol (see the doc's §1).
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

# ---------------------------------------------------------------- protocol wire
ALEL_MAGIC = 0x414C4543          # "ALEC"
ALEL_VERSION = 4
ALEL_PORT_CTRL = 54001
ALEL_MAX_DGRAM = 1024

# Commands (Frontend → Backend)
CMD_CALL = 1
CMD_GROUP = 2
CMD_NET = 3
CMD_AMD = 4
CMD_TERM = 5
CMD_SOUND = 6
CMD_SET_MODE = 7
CMD_CONFIG = 8
CMD_HELLO = 9
CMD_FORCE_LINK = 10
CMD_CHEDIT = 11

# Events (Backend → Frontend)
EVT_STATE = 64
EVT_LOG = 65
EVT_LQA = 66
EVT_SCAN = 67
EVT_AMD_RX = 68
EVT_SOUND_HIST = 69
EVT_CHANNELS = 70

# CONFIG "leave unchanged" sentinels.
CFG_KEEP_U8 = 0xFF
CFG_KEEP_U16 = 0xFFFF

# RF power steps (dBm) offered by the radio (RF_POWER_*).
RF_POWER_STEPS = (30, 40, 43, 47, 60)

# ---- struct layouts (little-endian, packed; see doc §2/§4/§5) ----
_HDR = struct.Struct("<IHHII")                                    # 16 B
_STATE = struct.Struct("<BBBBhhhhhHH16s16sBBBBQQQBfff8s8sBhB8sHHBBh")  # 127 B
_LOG = struct.Struct("<B3x12s64s")                                # 80 B
_LQA_HDR = struct.Struct("<BBxx")                                 # 4 B
_LQA_PEER = struct.Struct("<16sB16s")                             # 33 B
_SCAN_HDR = struct.Struct("<Bxxx")                                # 4 B
_SCAN_ROW = struct.Struct("<8sbB")                                # 10 B
_AMD_RX = struct.Struct("<16s12sB3x92s")                          # 124 B
_SH_HDR = struct.Struct("<Bxxx")                                  # 4 B
_SH_ROW = struct.Struct("<12shh16s")                             # 32 B
_CH_HDR = struct.Struct("<Bxxx")                                  # 4 B
_CH_ROW = struct.Struct("<h8s12s4sBx")                            # 28 B

# ---- enum name tables (doc §6) ----
FSM_NAMES = {0: "AVAILABLE", 1: "LINKING", 2: "LINKED", 3: "GROUP_LINKED", 4: "NET_LINKED"}
SIDEBAND_NAMES = {0: "USB", 1: "LSB", 2: "DSB"}
OCC_NAMES = {0: "off", 1: "on", 2: "occ", 3: "busy"}
LOGKIND_NAMES = {0: "RX", 1: "TX", 2: "SYS", 3: "SND", 4: "LQA", 5: "ERR"}
SOUND_MODE_NAMES = {0: "SINGLE", 1: "SCANNING", 2: "HANDSHAKE"}

LINKED_FSM = (2, 3, 4)   # LINKED / GROUP_LINKED / NET_LINKED — "linked" for the UI

# No STATE for ~this long → the backend is considered silent/unreachable.
_STALE_S = 3.0


def _cstr(b: bytes) -> str:
    """Decode a fixed NUL-terminated buffer up to its first NUL (doc §7)."""
    return b.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _z(s: Optional[str], n: int) -> bytes:
    """Encode ``s`` for a fixed ``n``-byte buffer, always leaving a NUL terminator."""
    return (s or "").encode("ascii", "replace")[: n - 1]


class AleController(QObject):
    """Owns one UDP control session with the ALE radio backend, behind Qt signals."""

    state_changed = pyqtSignal(dict)         # decoded alel_state_t snapshot
    channels_changed = pyqtSignal(list)      # list[channel dict]
    scan_changed = pyqtSignal(list)          # list[scan-row dict]
    lqa_changed = pyqtSignal(dict)           # {n_channels, peers:[...]}
    sound_hist_changed = pyqtSignal(list)    # list[sounding dict]
    log_received = pyqtSignal(dict)          # one event-log line
    amd_received = pyqtSignal(dict)          # one received AMD
    connection_changed = pyqtSignal(bool)    # backend became reachable / went silent
    error = pyqtSignal(str)

    def __init__(self, host: str = "127.0.0.1", port: int = ALEL_PORT_CTRL,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self._sock: Optional[socket.socket] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seq = 0
        self._running = False
        self._reachable = False
        self._last_rx = 0.0

        # HELLO keepalive + reachability watchdog, on the GUI thread.
        self._hello = QTimer(self)
        self._hello.setInterval(1000)
        self._hello.timeout.connect(self._on_hello_tick)

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._running

    @property
    def reachable(self) -> bool:
        return self._reachable

    # ------------------------------------------------------------------ lifecycle
    def start(self, host: Optional[str] = None, port=None) -> None:
        """Open the socket and begin HELLO/RX. No-op if already started.

        UDP is connectionless: this just fixes the peer (so ``recv`` only sees the
        backend and the source port stays put) and starts announcing us as a peer.
        """
        if self._sock is not None:
            return
        if host:
            self.host = host
        if port not in (None, ""):
            self.port = int(port)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((self.host, self.port))
            s.settimeout(0.4)
        except OSError as exc:
            self.error.emit(f"connect {self.host}:{self.port}: {exc}")
            return
        self._sock = s
        self._stop.clear()
        self._reachable = False
        self._last_rx = 0.0
        self._running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, name="ale-rx", daemon=True)
        self._rx_thread.start()
        self._hello.start()
        self._send(CMD_HELLO)   # announce immediately

    def stop(self) -> None:
        """Stop keepalive/RX and close the socket. No-op if not started."""
        self._hello.stop()
        self._stop.set()
        was_running = self._running
        self._running = False
        s = self._sock
        self._sock = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        if self._rx_thread is not None:
            self._rx_thread.join(timeout=1.0)
        self._rx_thread = None
        if was_running and self._reachable:
            self._reachable = False
            self.connection_changed.emit(False)
        else:
            self._reachable = False

    # ------------------------------------------------------------------ RX
    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                dg = sock.recv(ALEL_MAX_DGRAM)
            except socket.timeout:
                continue
            except OSError:
                break   # socket closed by stop()
            self._handle(dg)

    def _handle(self, dg: bytes) -> None:
        if len(dg) < _HDR.size:
            return
        magic, ver, mtype, _seq, ln = _HDR.unpack_from(dg, 0)
        # Validation (doc §2 `alel_hdr_ok`): any mismatch → drop silently.
        if magic != ALEL_MAGIC or ver != ALEL_VERSION or _HDR.size + ln > len(dg):
            return
        pl = dg[_HDR.size:_HDR.size + ln]
        self._note_rx()
        try:
            if mtype == EVT_STATE and len(pl) >= _STATE.size:
                self.state_changed.emit(self._decode_state(pl))
            elif mtype == EVT_CHANNELS and len(pl) >= _CH_HDR.size:
                self.channels_changed.emit(self._decode_channels(pl))
            elif mtype == EVT_SCAN and len(pl) >= _SCAN_HDR.size:
                self.scan_changed.emit(self._decode_scan(pl))
            elif mtype == EVT_LQA and len(pl) >= _LQA_HDR.size:
                self.lqa_changed.emit(self._decode_lqa(pl))
            elif mtype == EVT_SOUND_HIST and len(pl) >= _SH_HDR.size:
                self.sound_hist_changed.emit(self._decode_sound_hist(pl))
            elif mtype == EVT_LOG and len(pl) >= _LOG.size:
                self.log_received.emit(self._decode_log(pl))
            elif mtype == EVT_AMD_RX and len(pl) >= _AMD_RX.size:
                self.amd_received.emit(self._decode_amd(pl))
        except struct.error as exc:   # pragma: no cover - defensive
            self.error.emit(f"decode type {mtype}: {exc}")

    def _note_rx(self) -> None:
        self._last_rx = time.monotonic()
        if not self._reachable:
            self._reachable = True
            self.connection_changed.emit(True)

    # ------------------------------------------------------------------ decoders
    @staticmethod
    def _decode_state(pl: bytes) -> dict:
        (fsm, _radio_mode, scanning, scan_rate, cur_channel, sinad, ber, rssi, noise,
         twa_remain, twa_max, self_addr, link_peer, voice_open, ptt, rx_voice, sideband,
         frames_rx, sounds_rx, words_valid, tx_active, tx_power, tx_refl, tx_vswr,
         tx_power_unit, tx_refl_unit, sounding, sounding_channel, forced, active_service,
         tcc_max, tm_max, occupancy_detect, _pad, tx_power_dbm) = _STATE.unpack_from(pl, 0)
        return {
            "fsm": fsm, "fsm_name": FSM_NAMES.get(fsm, str(fsm)),
            "linked": fsm in LINKED_FSM,
            "scanning": bool(scanning), "scan_rate": scan_rate,
            "cur_channel": cur_channel, "sinad": sinad, "ber": ber, "rssi": rssi, "noise": noise,
            "twa_remain": twa_remain, "twa_max": twa_max,
            "self_addr": _cstr(self_addr), "link_peer": _cstr(link_peer),
            "voice_open": bool(voice_open), "ptt": bool(ptt), "rx_voice": bool(rx_voice),
            "sideband": sideband, "sideband_name": SIDEBAND_NAMES.get(sideband, str(sideband)),
            "frames_rx": frames_rx, "sounds_rx": sounds_rx, "words_valid": words_valid,
            "tx_active": bool(tx_active), "tx_power": tx_power, "tx_refl": tx_refl,
            "tx_vswr": tx_vswr, "tx_power_unit": _cstr(tx_power_unit),
            "tx_refl_unit": _cstr(tx_refl_unit),
            "sounding": bool(sounding), "sounding_channel": sounding_channel,
            "forced": bool(forced), "active_service": _cstr(active_service),
            "tcc_max": tcc_max, "tm_max": tm_max, "occupancy_detect": bool(occupancy_detect),
            "tx_power_dbm": tx_power_dbm,
        }

    @staticmethod
    def _decode_channels(pl: bytes) -> List[dict]:
        (n,) = _CH_HDR.unpack_from(pl, 0)
        out = []
        off = _CH_HDR.size
        for _ in range(min(n, 16)):
            idx, freq, name, band, enabled = _CH_ROW.unpack_from(pl, off)
            off += _CH_ROW.size
            out.append({"idx": idx, "freq": _cstr(freq), "name": _cstr(name),
                        "band": _cstr(band), "enabled": bool(enabled)})
        return out

    @staticmethod
    def _decode_scan(pl: bytes) -> List[dict]:
        (n,) = _SCAN_HDR.unpack_from(pl, 0)
        out = []
        off = _SCAN_HDR.size
        for _ in range(min(n, 16)):
            label, lqa, occ = _SCAN_ROW.unpack_from(pl, off)
            off += _SCAN_ROW.size
            out.append({"label": _cstr(label), "lqa": lqa, "occ": occ,
                        "occ_name": OCC_NAMES.get(occ, str(occ))})
        return out

    @staticmethod
    def _decode_lqa(pl: bytes) -> dict:
        n_peers, n_channels = _LQA_HDR.unpack_from(pl, 0)
        off = _LQA_HDR.size
        peers = []
        for _ in range(min(n_peers, 12)):
            addr, online, lqa = _LQA_PEER.unpack_from(pl, off)
            off += _LQA_PEER.size
            peers.append({"addr": _cstr(addr), "online": bool(online),
                          "lqa": list(lqa[:min(n_channels, 16)])})
        return {"n_channels": n_channels, "peers": peers}

    @staticmethod
    def _decode_sound_hist(pl: bytes) -> List[dict]:
        (n,) = _SH_HDR.unpack_from(pl, 0)
        out = []
        off = _SH_HDR.size
        for _ in range(min(n, 8)):
            t, ch, q, ack = _SH_ROW.unpack_from(pl, off)
            off += _SH_ROW.size
            out.append({"t": _cstr(t), "ch": ch, "q": q, "ack": _cstr(ack)})
        return out

    @staticmethod
    def _decode_log(pl: bytes) -> dict:
        kind, t, text = _LOG.unpack_from(pl, 0)
        return {"kind": kind, "kind_name": LOGKIND_NAMES.get(kind, str(kind)),
                "t": _cstr(t), "text": _cstr(text)}

    @staticmethod
    def _decode_amd(pl: bytes) -> dict:
        frm, t, read, text = _AMD_RX.unpack_from(pl, 0)
        return {"from": _cstr(frm), "t": _cstr(t), "read": bool(read), "text": _cstr(text)}

    # ------------------------------------------------------------------ TX
    def _send(self, mtype: int, payload: bytes = b"") -> None:
        sock = self._sock
        if sock is None:
            return
        hdr = _HDR.pack(ALEL_MAGIC, ALEL_VERSION, mtype, self._seq, len(payload))
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        try:
            sock.send(hdr + payload)
        except OSError as exc:
            self.error.emit(f"send: {exc}")

    def _on_hello_tick(self) -> None:
        self._send(CMD_HELLO)
        if self._reachable and (time.monotonic() - self._last_rx) > _STALE_S:
            self._reachable = False
            self.connection_changed.emit(False)

    # ------------------------------------------------------------------ commands
    def send_call(self, addr: str, channel: int = -1) -> None:
        self._send(CMD_CALL, struct.pack("<16sh", _z(addr, 16), int(channel)))

    def send_group(self, members: List[str]) -> None:
        members = [m for m in (members or []) if m]
        n = max(1, min(5, len(members)))
        five = (members + ["", "", "", "", ""])[:5]
        buf = b"".join(struct.pack("<16s", _z(m, 16)) for m in five)
        self._send(CMD_GROUP, struct.pack("<B", n) + buf)

    def send_net(self, netid: str) -> None:
        self._send(CMD_NET, struct.pack("<16s", _z(netid, 16)))

    def send_amd(self, dest: str, text: str) -> None:
        self._send(CMD_AMD, struct.pack("<16sB3x92s", _z(dest, 16), 0, _z(text, 92)))

    def send_term(self, addr: str = "") -> None:
        self._send(CMD_TERM, struct.pack("<16s", _z(addr, 16)))

    def send_sound(self, mode: int = 1) -> None:
        self._send(CMD_SOUND, struct.pack("<B", int(mode) & 0xFF))

    def send_config(self, *, scan_rate: int = 0, sideband: int = CFG_KEEP_U8,
                    sounding_enabled: int = CFG_KEEP_U8, occupancy_detect: int = CFG_KEEP_U8,
                    twa_s: int = CFG_KEEP_U16, tx_power_dbm: int = 0,
                    tcc_max_s: int = 0, tm_max_s: int = 0) -> None:
        """Runtime config. Every field defaults to its "leave unchanged" sentinel,
        so callers set exactly one knob without zeroing the others (doc §4)."""
        self._send(CMD_CONFIG, struct.pack(
            "<BBBBHhHH", scan_rate & 0xFF, sideband & 0xFF, sounding_enabled & 0xFF,
            occupancy_detect & 0xFF, twa_s & 0xFFFF, int(tx_power_dbm),
            tcc_max_s & 0xFFFF, tm_max_s & 0xFFFF))

    def send_force_link(self, channel: int, forced: bool, service: str = "am") -> None:
        self._send(CMD_FORCE_LINK,
                   struct.pack("<hB8s", int(channel), 1 if forced else 0, _z(service, 8)))

    def send_chedit(self, idx: int, freq: str = "", name: str = "") -> None:
        self._send(CMD_CHEDIT, struct.pack("<h8s12s", int(idx), _z(freq, 8), _z(name, 12)))
