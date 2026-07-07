"""Minimal mock of the ALE 2G radio-control backend (UDP :54001).

Speaks the protocol in ``docs/PROTOCOLO-CONTROLE-REMOTO.md`` well enough to drive
the Subnet Console's Radio Control screen end to end in tests: it learns a client
on any datagram (HELLO included), fans out an ``emit_scene`` (CHANNELS + SCAN +
LQA + SOUND_HIST + STATE) to a new peer, then streams STATE periodically and
executes commands (CALL / TERM / SOUND / CONFIG / FORCE_LINK / CHEDIT / AMD),
answering each with an immediate STATE (and a LOG / AMD_RX where relevant).

Uses the exact wire structs from :mod:`ale_controller`, so a passing round-trip
exercises the real encoder/decoder against a peer, not a mirror of itself.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Optional

from src.interface.subnet_console.backend.ale_controller import (
    ALEL_MAGIC, ALEL_VERSION,
    CMD_AMD, CMD_CALL, CMD_CHEDIT, CMD_CONFIG, CMD_FORCE_LINK, CMD_GROUP,
    CMD_HELLO, CMD_NET, CMD_SOUND, CMD_TERM,
    EVT_AMD_RX, EVT_CHANNELS, EVT_LOG, EVT_LQA, EVT_SCAN, EVT_SOUND_HIST, EVT_STATE,
    CFG_KEEP_U8, CFG_KEEP_U16,
    _HDR, _STATE, _LOG, _LQA_HDR, _LQA_PEER, _SCAN_HDR, _SCAN_ROW,
    _AMD_RX, _SH_HDR, _SH_ROW, _CH_HDR, _CH_ROW,
)


def _z(s: str, n: int) -> bytes:
    return (s or "").encode("ascii", "replace")[: n - 1]


_DEMO_CH = [("3.596", "NVIS-A"), ("5.357", "REGION-1"), ("7.102", "LONG-A"),
            ("10.145", "LONG-B"), ("14.109", "DX-1"), ("18.106", "DX-2")]
_DEMO_LQA = [24, 12, 27, 21, 29, 9]
_DEMO_OCC = [1, 0, 2, 1, 1, 0]


class MockAleBackend:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 self_addr: str = "BR1", state_period_s: float = 0.1):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.1)
        self.host, self.port = self.sock.getsockname()[0], self.sock.getsockname()[1]
        self._period = state_period_s
        self._clients = set()
        self._seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.channels = [{"idx": i, "freq": f, "name": nm, "band": "HF"}
                         for i, (f, nm) in enumerate(_DEMO_CH)]
        self.state = {
            "fsm": 0, "scanning": 1, "scan_rate": 5, "cur_channel": 4,
            "sinad": 22, "ber": 3, "rssi": -71, "noise": -103, "twa_remain": 118,
            "twa_max": 300, "self_addr": self_addr, "link_peer": "", "linked": 0,
            "sideband": 0, "frames_rx": 1000, "sounds_rx": 7, "words_valid": 300,
            "tx_active": 0, "tx_power": 44.2, "tx_refl": 1.1, "tx_vswr": 1.4,
            "tx_power_unit": "W", "tx_refl_unit": "W", "sounding": 0,
            "sounding_channel": -1, "forced": 0, "active_service": "",
            "tcc_max": 127, "tm_max": 30, "occupancy_detect": 1, "tx_power_dbm": 47,
        }
        self.sound_hist = [{"t": "14:19:02", "ch": 4, "q": 29, "ack": "BR2"}]

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "MockAleBackend":
        self._thread = threading.Thread(target=self._loop, name="mock-ale", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            try:
                dg, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                dg, addr = None, None
            except OSError:
                break
            if dg is not None:
                self._handle(dg, addr)
            now = time.monotonic()
            if self._clients and now - last >= self._period:
                last = now
                self._fanout(EVT_STATE, self._enc_state())

    # ------------------------------------------------------------------ tx
    def _send(self, addr, mtype: int, payload: bytes) -> None:
        hdr = _HDR.pack(ALEL_MAGIC, ALEL_VERSION, mtype, self._seq, len(payload))
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        try:
            self.sock.sendto(hdr + payload, addr)
        except OSError:
            pass

    def _fanout(self, mtype: int, payload: bytes) -> None:
        for c in list(self._clients):
            self._send(c, mtype, payload)

    def _emit_scene(self, addr) -> None:
        self._send(addr, EVT_CHANNELS, self._enc_channels())
        self._send(addr, EVT_SCAN, self._enc_scan())
        self._send(addr, EVT_LQA, self._enc_lqa())
        self._send(addr, EVT_SOUND_HIST, self._enc_sound_hist())
        self._send(addr, EVT_STATE, self._enc_state())

    # ------------------------------------------------------------------ rx
    def _handle(self, dg: bytes, addr) -> None:
        if len(dg) < _HDR.size:
            return
        magic, ver, mtype, _seq, ln = _HDR.unpack_from(dg, 0)
        if magic != ALEL_MAGIC or ver != ALEL_VERSION or _HDR.size + ln > len(dg):
            return
        pl = dg[_HDR.size:_HDR.size + ln]
        is_new = addr not in self._clients
        self._clients.add(addr)
        if is_new:
            self._emit_scene(addr)

        s = self.state
        if mtype == CMD_HELLO:
            return
        elif mtype == CMD_CALL:
            dest, ch = struct.unpack_from("<16sh", pl, 0)
            s.update(fsm=2, linked=1, link_peer=dest.split(b"\x00", 1)[0].decode(), scanning=0)
            if ch >= 0:
                s["cur_channel"] = ch
            self._fanout(EVT_LOG, self._enc_log(1, "TX", f"CALL {s['link_peer']}"))
        elif mtype == CMD_GROUP:
            s.update(fsm=3, linked=1, scanning=0)
        elif mtype == CMD_NET:
            s.update(fsm=4, linked=1, scanning=0)
        elif mtype == CMD_TERM:
            s.update(fsm=0, linked=0, link_peer="", forced=0, scanning=1)
            self._fanout(EVT_LOG, self._enc_log(2, "SYS", "Link terminated"))
        elif mtype == CMD_SOUND:
            mode = pl[0] if pl else 1
            self.sound_hist.insert(0, {"t": "00:00:00", "ch": s["cur_channel"], "q": 20, "ack": "—"})
            del self.sound_hist[8:]
            self._fanout(EVT_SOUND_HIST, self._enc_sound_hist())
            self._fanout(EVT_LOG, self._enc_log(3, "SND", f"Sounding mode {mode}"))
        elif mtype == CMD_CONFIG:
            (scan_rate, sideband, sounding_en, occ, twa, txp, tcc, tm) = struct.unpack_from(
                "<BBBBHhHH", pl, 0)
            if scan_rate != 0:
                s["scan_rate"] = scan_rate
            if sideband != CFG_KEEP_U8:
                s["sideband"] = sideband
            if occ != CFG_KEEP_U8:
                s["occupancy_detect"] = occ
            if twa != CFG_KEEP_U16:
                s["twa_max"] = twa
                s["twa_remain"] = twa
            if txp > 0:
                s["tx_power_dbm"] = txp
            if tcc != 0:
                s["tcc_max"] = tcc
            if tm != 0:
                s["tm_max"] = tm
        elif mtype == CMD_FORCE_LINK:
            ch, forced, service = struct.unpack_from("<hB8s", pl, 0)
            s["forced"] = forced
            s["active_service"] = service.split(b"\x00", 1)[0].decode() if forced else ""
            s["scanning"] = 0 if forced else 1
            if ch >= 0:
                s["cur_channel"] = ch
        elif mtype == CMD_CHEDIT:
            idx, freq, name = struct.unpack_from("<h8s12s", pl, 0)
            fr = freq.split(b"\x00", 1)[0].decode()
            nm = name.split(b"\x00", 1)[0].decode()
            for c in self.channels:
                if c["idx"] == idx:
                    if fr:
                        c["freq"] = fr
                    if nm:
                        c["name"] = nm
            self._fanout(EVT_CHANNELS, self._enc_channels())
        elif mtype == CMD_AMD:
            dest, _cf, text = struct.unpack_from("<16sB3x92s", pl, 0)
            # echo the AMD back as if received on the air, for RX-path testing
            self._fanout(EVT_AMD_RX, self._enc_amd_rx(
                s["self_addr"], text.split(b"\x00", 1)[0].decode()))
        # every command answers with an immediate STATE
        self._fanout(EVT_STATE, self._enc_state())

    # ------------------------------------------------------------------ encoders
    def _enc_state(self) -> bytes:
        s = self.state
        return _STATE.pack(
            s["fsm"], 0, s["scanning"], s["scan_rate"], s["cur_channel"],
            s["sinad"], s["ber"], s["rssi"], s["noise"], s["twa_remain"], s["twa_max"],
            _z(s["self_addr"], 16), _z(s["link_peer"], 16),
            1 if s["linked"] else 0, 0, 0, s["sideband"],
            s["frames_rx"], s["sounds_rx"], s["words_valid"], s["tx_active"],
            s["tx_power"], s["tx_refl"], s["tx_vswr"],
            _z(s["tx_power_unit"], 8), _z(s["tx_refl_unit"], 8),
            s["sounding"], s["sounding_channel"], s["forced"], _z(s["active_service"], 8),
            s["tcc_max"], s["tm_max"], s["occupancy_detect"], 0, s["tx_power_dbm"])

    def _enc_channels(self) -> bytes:
        out = _CH_HDR.pack(len(self.channels))
        for c in self.channels:
            out += _CH_ROW.pack(c["idx"], _z(c["freq"], 8), _z(c["name"], 12),
                                _z(c["band"], 4), 1)
        # pad to full 16 rows
        out += b"\x00" * (_CH_ROW.size * (16 - len(self.channels)))
        return out

    def _enc_scan(self) -> bytes:
        out = _SCAN_HDR.pack(len(self.channels))
        for i, c in enumerate(self.channels):
            out += _SCAN_ROW.pack(_z(c["freq"], 8), _DEMO_LQA[i % len(_DEMO_LQA)],
                                  _DEMO_OCC[i % len(_DEMO_OCC)])
        out += b"\x00" * (_SCAN_ROW.size * (16 - len(self.channels)))
        return out

    def _enc_lqa(self) -> bytes:
        peers = [("BR2", _DEMO_LQA), ("BR3", [v // 2 for v in _DEMO_LQA])]
        out = _LQA_HDR.pack(len(peers), len(self.channels))
        for addr, lqa in peers:
            lqbuf = bytes((lqa + [31] * 16)[:16])
            out += _LQA_PEER.pack(_z(addr, 16), 1, lqbuf)
        out += b"\x00" * (_LQA_PEER.size * (12 - len(peers)))
        return out

    def _enc_sound_hist(self) -> bytes:
        out = _SH_HDR.pack(len(self.sound_hist))
        for r in self.sound_hist:
            out += _SH_ROW.pack(_z(r["t"], 12), r["ch"], r["q"], _z(r["ack"], 16))
        out += b"\x00" * (_SH_ROW.size * (8 - len(self.sound_hist)))
        return out

    def _enc_log(self, kind: int, _name: str, text: str) -> bytes:
        return _LOG.pack(kind, _z("00:00:00", 12), _z(text, 64))

    def _enc_amd_rx(self, frm: str, text: str) -> bytes:
        return _AMD_RX.pack(_z(frm, 16), _z("00:00:00", 12), 0, _z(text, 92))
