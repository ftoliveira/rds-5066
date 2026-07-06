"""NodeController — a Qt-friendly wrapper around a live STANAG 5066 node.

Boots and drives a real :class:`~src.stanag_node.StanagNode` over a
:class:`~src.modem.tcp_110d_adapter.Tcp110dModemAdapter` (MIL-STD-188-110D
Appendix A / TCP), exactly as ``chat_app_110d.py`` does, but headless and behind
Qt signals so the PyQt6 console can drive it.

Threading model (mirrors chat_app_110d):
- The node is stepped by a background daemon thread calling ``node.tick()`` every
  200 ms; the modem adapter runs its own TCP threads (started in the StanagNode
  constructor).
- SIS callbacks fire on that tick thread — each just emits a Qt signal, which Qt
  delivers to the GUI thread via a queued connection.
- A GUI-thread ``QTimer`` polls :meth:`status` every 500 ms and emits
  :attr:`status_changed`, so status reads never happen on the tick thread.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from src.cas import CasConfig
from src.modem.tcp_110d_adapter import Tcp110dConfig, Tcp110dModemAdapter
from src.stanag_node import StanagNode
from src.stypes import DeliveryMode

from .sis_server import InstrumentedSisServer

TICK_PERIOD_S = 0.2
STATUS_POLL_MS = 500


def _enum_name(v) -> str:
    return getattr(v, "name", None) or getattr(v, "value", None) or str(v)


class NodeController(QObject):
    """Owns one live STANAG 5066 node and exposes it to the UI via signals."""

    status_changed = pyqtSignal(dict)          # periodic status snapshot (GUI thread)
    unidata_received = pyqtSignal(dict)        # {sap, src_addr, src_sap, priority, text, updu}
    link_established = pyqtSignal(int, int)    # remote_addr, remote_sap
    link_terminated = pyqtSignal(int, bool)    # remote_addr, initiator_received_confirm
    request_rejected = pyqtSignal(int, str)    # sap_id, reason name
    node_error = pyqtSignal(str)

    def __init__(self, local_id: int, remote_id: int, host: str, port: int,
                 *, bitrate: int = 2400, interleaver: str = "long",
                 bound_saps=(3, 5, 6, 7), max_user_data_bytes: int = 128,
                 sis_host: str = "127.0.0.1", sis_port: int = 5066,
                 sis_max_clients: int = 16,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.local_id = local_id
        self.remote_id = remote_id
        self.host = host
        self.port = int(port)
        self.bitrate = int(bitrate)
        self.interleaver = interleaver
        self.bound_saps = tuple(bound_saps)
        self.max_user_data_bytes = int(max_user_data_bytes)

        self.node: Optional[StanagNode] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending_break = False

        # Raw SIS Socket Server (F.16) — started alongside the node, on its own
        # asyncio loop/thread (mirrors chat_app_110d._start_sis_api).
        self.sis_host = sis_host
        self.sis_port = int(sis_port)
        self.sis_max_clients = int(sis_max_clients)
        self.sis_server: Optional[InstrumentedSisServer] = None
        self._sis_loop: Optional[asyncio.AbstractEventLoop] = None
        self._sis_thread: Optional[threading.Thread] = None
        self._sis_thread_stop: Optional[threading.Event] = None
        self.sis_actual_host: Optional[str] = None
        self.sis_actual_port: Optional[int] = None

        self._poll = QTimer(self)
        self._poll.setInterval(STATUS_POLL_MS)
        self._poll.timeout.connect(self._emit_status)

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self.node is not None

    def start(self, *, host: Optional[str] = None, port=None,
              bitrate: Optional[int] = None, interleaver: Optional[str] = None) -> None:
        """Build the node + adapter and begin ticking. No-op if already running."""
        if self.node is not None:
            return
        if host is not None:
            self.host = host
        if port not in (None, ""):
            self.port = int(port)
        if bitrate is not None:
            self.bitrate = int(bitrate)
        if interleaver is not None:
            self.interleaver = interleaver

        adapter = Tcp110dModemAdapter(
            Tcp110dConfig(host=self.host, port=self.port, data_rate_bps=self.bitrate)
        )
        node = StanagNode(
            self.local_id, adapter,
            cas_config=CasConfig(call_timeout_seconds=15.0, break_timeout_seconds=10.0, max_retries=3),
            max_user_data_bytes=self.max_user_data_bytes,
            use_arq_data=True,
            soft_link_idle_timeout_ms=60_000,
            arq_reset_retransmit_ms=3000,
            arq_retx_timeout_ms=3000,
            arq_max_retries=5,
        )
        node.arq.data_rate_bps = self.bitrate
        node.arq.long_interleave = (self.interleaver.lower().startswith("long"))
        for sap in self.bound_saps:
            try:
                node.bind(sap)
            except Exception as exc:  # bind raises ValueError on reject
                self.node_error.emit(f"bind SAP {sap}: {exc}")
        node.register_callbacks(
            unidata_indication=self._on_unidata,
            request_rejected=self._on_rejected,
            hard_link_established=self._on_established,
            hard_link_terminated=self._on_terminated,
        )
        self.node = node
        self._stop.clear()
        self._pending_break = False
        self._thread = threading.Thread(target=self._loop, name="s5066-tick", daemon=True)
        self._thread.start()
        self._start_sis_server()
        self._poll.start()
        self._emit_status()

    def stop(self) -> None:
        """Stop ticking and tear the node/adapter down. No-op if not running."""
        self._poll.stop()
        self._stop_sis_server()
        self._stop.set()
        node = self.node
        self.node = None
        if node is not None:
            try:
                node.modem.modem_rx_stop()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._emit_status()

    # -------------------------------------------------- Raw SIS Socket (F.16)
    def _start_sis_server(self) -> None:
        """Start the Raw SIS Socket Server in an asyncio loop on its own thread.

        Mirrors ``chat_app_110d._start_sis_api``: external SIS clients can then
        bind SAPs over TCP. Port 0 binds an ephemeral port (used by tests); the
        actual bound port is read back in :meth:`_sis_main`.
        """
        if self._sis_loop is not None or self.node is None:
            return
        self.sis_server = InstrumentedSisServer(
            self.node, host=self.sis_host, port=self.sis_port,
            max_connections=self.sis_max_clients)
        self.sis_actual_host = None
        self.sis_actual_port = None
        self._sis_thread_stop = threading.Event()
        self._sis_loop = asyncio.new_event_loop()
        self._sis_thread = threading.Thread(target=self._run_sis_loop,
                                            name="s5066-sis", daemon=True)
        self._sis_thread.start()

    def _run_sis_loop(self) -> None:
        loop = self._sis_loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._sis_main())
        except Exception as exc:  # pragma: no cover - defensive
            self.node_error.emit(f"sis server: {exc}")
        finally:
            loop.close()

    async def _sis_main(self) -> None:
        server = self.sis_server
        try:
            await server.start()
            addr = server._server.sockets[0].getsockname()
            self.sis_actual_host, self.sis_actual_port = addr[0], int(addr[1])
            # Keep the loop alive until stop() releases the event.
            await asyncio.get_event_loop().run_in_executor(
                None, self._sis_thread_stop.wait)
        finally:
            await server.stop()

    def _stop_sis_server(self) -> None:
        """Signal the SIS loop to finish and tear the server down."""
        if self._sis_loop is None:
            return
        # Setting the event unblocks the run_in_executor wait in _sis_main, whose
        # `finally` awaits server.stop(); the loop then completes on its own. The
        # no-op callback just nudges the loop to notice promptly.
        if self._sis_thread_stop is not None:
            self._sis_thread_stop.set()
        try:
            self._sis_loop.call_soon_threadsafe(lambda: None)
        except RuntimeError:
            pass
        if self._sis_thread is not None:
            self._sis_thread.join(timeout=3.0)
        self._sis_loop = None
        self._sis_thread = None
        self._sis_thread_stop = None
        self.sis_server = None
        self.sis_actual_host = None
        self.sis_actual_port = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            node = self.node
            if node is not None:
                try:
                    if self._pending_break:
                        self._pending_break = False
                        node.break_link()
                    node.tick(int(time.monotonic() * 1000))
                except Exception as exc:
                    self.node_error.emit(f"tick: {exc}")
            time.sleep(TICK_PERIOD_S)

    # ------------------------------------------------------------------ callbacks
    # These run on the tick thread; emitting a signal marshals to the GUI thread.
    def _on_unidata(self, indication) -> None:
        updu = getattr(indication, "updu", b"")
        sap = getattr(indication, "dest_sap", 0)
        try:
            text = updu.decode("ascii" if sap == 5 else "utf-8", "replace")
        except Exception:
            text = repr(updu)
        self.unidata_received.emit({
            "sap": sap, "src_addr": getattr(indication, "src_addr", 0),
            "src_sap": getattr(indication, "src_sap", 0),
            "priority": getattr(indication, "priority", 0),
            "text": text, "updu": bytes(updu),
        })

    def _on_rejected(self, sap_id: int, reason) -> None:
        self.request_rejected.emit(int(sap_id), _enum_name(reason))

    def _on_established(self, remote_addr: int, remote_sap: int) -> None:
        self.link_established.emit(int(remote_addr), int(remote_sap))

    def _on_terminated(self, remote_addr: int, initiator_received_confirm: bool = False) -> None:
        # Defer the physical CAS break to the next tick (avoids re-entering the
        # node from inside its own callback).
        self._pending_break = True
        self.link_terminated.emit(int(remote_addr), bool(initiator_received_confirm))

    # ------------------------------------------------------------------ commands
    def set_rate(self, bps: int) -> None:
        self.bitrate = int(bps)
        if self.node is not None:
            self.node.arq.data_rate_bps = int(bps)

    def set_interleaver(self, value: str) -> None:
        self.interleaver = value
        if self.node is not None:
            self.node.arq.long_interleave = value.lower().startswith("long")

    def hard_link_establish(self, sap_id: int, dest_sap: int, *, priority: int = 15) -> None:
        if self.node is not None:
            self.node.hard_link_establish(sap_id, priority, self.remote_id, dest_sap)

    def hard_link_terminate(self, sap_id: int) -> None:
        if self.node is not None:
            self.node.hard_link_terminate(sap_id, remote_addr=self.remote_id)

    def send_unidata(self, sap_id: int, dest_sap: int, payload: bytes, *,
                     priority: int = 4, ttl_seconds: float = 0.0,
                     mode: Optional[DeliveryMode] = None) -> None:
        if self.node is not None:
            self.node.unidata_request(sap_id, self.remote_id, dest_sap, priority,
                                      ttl_seconds, mode=mode, updu=payload)

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        """Best-effort snapshot of live node state (read on the GUI thread)."""
        node = self.node
        if node is None:
            off = {"running": False, "connected": False, "rate": self.bitrate}
            off.update(self._sis_status())
            return off
        snap = {"running": True, "connected": False, "rate": self.bitrate,
                "blocking": 0, "cas": "IDLE", "sis_state": "IDLE", "sis_type": "-",
                "dts": "-", "arq_state": "-", "arq_window": 0, "arq_unacked": 0,
                "arq_queue": 0, "arq_lwe": 0, "arq_uwe": 0, "reset_pending": False,
                "tx_queue": 0}
        snap.update(self._sis_status())
        try:
            m = node.modem
            snap["connected"] = bool(getattr(m, "is_connected", False))
            snap["rate"] = int(getattr(m, "reported_data_rate", 0) or getattr(m.config, "data_rate_bps", 0))
            snap["blocking"] = int(getattr(m, "reported_blocking_factor", 0) or 0)
        except Exception:
            pass
        try:
            snap["cas"] = _enum_name(node.cas.state)
        except Exception:
            pass
        try:
            ls = node._link_session
            snap["sis_state"] = _enum_name(ls.state)
            snap["sis_type"] = _enum_name(ls.link_type)
        except Exception:
            pass
        try:
            snap["dts"] = _enum_name(node.dts.state)
        except Exception:
            pass
        try:
            arq = node.arq
            snap["arq_state"] = _enum_name(arq._tx_state)
            snap["arq_window"] = len(arq._tx_window)
            # Frames still in flight (not yet ACKed=1) — drives file-transfer
            # progress so the count drains to zero once the peer confirms.
            snap["arq_unacked"] = sum(1 for s in arq._tx_window.values()
                                      if getattr(s, "status", 0) != 1)
            snap["arq_queue"] = len(arq._tx_queue)
            snap["arq_lwe"] = int(arq._tx_lwe)
            snap["arq_uwe"] = int(arq._tx_uwe)
            snap["reset_pending"] = bool(arq.reset_pending)
        except Exception:
            pass
        try:
            snap["tx_queue"] = len(node._tx_queue)
        except Exception:
            pass
        return snap

    def _sis_status(self) -> dict:
        """SIS-server fields for the status snapshot (F.16 screen)."""
        srv = self.sis_server
        if srv is None or self.sis_actual_port is None:
            return {"sis_server_running": False, "sis_server_host": self.sis_host,
                    "sis_server_port": None, "sis_max_clients": self.sis_max_clients,
                    "sis_clients": [], "sis_wire": [], "sis_prim_count": 0}
        return {
            "sis_server_running": True,
            "sis_server_host": self.sis_actual_host,
            "sis_server_port": self.sis_actual_port,
            "sis_max_clients": self.sis_max_clients,
            "sis_clients": srv.client_rows(),
            "sis_wire": srv.wire_rows(),
            "sis_prim_count": srv.prim_count,
        }

    def _emit_status(self) -> None:
        self.status_changed.emit(self.status())
