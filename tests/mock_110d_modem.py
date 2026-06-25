"""Mock do modem MIL-STD-188-110D (lado DCE/servidor TCP do Anexo A).

Espelha o comportamento de `rds-hf/.../net_reactor.c` o suficiente para exercitar
o `Tcp110dModemAdapter` sem o backend real:
  - Handshake reativo: CONNECT → CONNECTACK → CONNECTION_PROBE.
  - Initial Setup / Transmit Setup / Tx Status (FLUSHED) / Carrier Detect iniciais.
  - FSM de TX: TRANSMIT_ARM → (PORT_READY) → TRANSMIT_START → DATA_TRANSFER… →
    (LAST) key-up → FLUSHED, com Tx Status não-solicitado a cada transição.
  - Entrega de RX (ar → DTE): Carrier Detect(DETECTED) + DATA_TRANSFER
    (FIRST_ONLY/CONTINUATION/empty LAST) + Carrier Detect(NONE).

Um burst "OTA" keyed-up pelo DTE é entregue via callback `on_air_tx(bytes)`. Um
:class:`MockAir` cruza dois modems (TX de um → RX do outro) para topologia de
dois nós; em loopback, `on_air_tx` aponta para o próprio `deliver_air_rx`.

Usa o codec validado (`src.modem.appendix_a_codec`) para (de)serializar — os
mesmos bytes do C de referência.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Optional

from src.modem.appendix_a_codec import (
    CarrierDetectPayload,
    CarrierState,
    ConnectPayload,
    DataTransferPayload,
    InitialSetupPayload,
    MAX_TCP_DATA_BYTES,
    PROTOCOL_VERSION,
    PacketOrder,
    PacketReader,
    PacketType,
    PayloadCommand,
    SyncFlag,
    TransmitSetupPayload,
    TxDataNakPayload,
    TxStateWire,
    TxStatusPayload,
    build_command,
    build_connect,
    build_connectack,
    encode_packet,
)


def _now() -> float:
    return time.monotonic()


class _Conn:
    """Estado de uma conexão DTE."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = PacketReader()
        self.phase = "handshake"            # handshake | running
        self.hstep = "WAIT_CONNECT"
        self.tx_state = TxStateWire.FLUSHED
        self.fifo = bytearray()             # bytes acumulados desta janela TX
        self.last_send = _now()
        self.pending_rx: list[bytes] = []   # bursts OTA a entregar ao DTE
        self.lock = threading.Lock()
        self.armed_at: Optional[float] = None


class MockModem110d:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        data_rate: int = 2400,
        blocking_factor: int = 600,
        sync_flag: int = SyncFlag.SYNCHRONOUS,
        keepalive_period: float = 2.0,
        arm_ready_delay: float = 0.0,       # >0 simula Receiver Master (NOT_READY→READY)
        on_air_tx: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.data_rate = data_rate
        self.blocking_factor = blocking_factor
        self.sync_flag = sync_flag
        self.keepalive_period = keepalive_period
        self.arm_ready_delay = arm_ready_delay
        self.on_air_tx = on_air_tx

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(2)
        self.port = self._srv.getsockname()[1]

        self._stop = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._conns: list[_Conn] = []
        # contadores p/ asserts de teste
        self.bursts_received = 0
        self.connections_made = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> "MockModem110d":
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self.drop_connections()

    def drop_connections(self) -> None:
        """Derruba as conexões DTE ativas mantendo o servidor escutando.

        Usado para testar reconexão do adaptador (o servidor continua aceitando).
        """
        for c in list(self._conns):
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.sock.close()
            except OSError:
                pass

    # ── entrega de RX (ar → DTE) ─────────────────────────────────────────

    def deliver_air_rx(self, data: bytes) -> None:
        """Injeta um burst OTA para ser entregue ao DTE conectado como DATA RX."""
        for c in list(self._conns):
            if c.phase == "running":
                with c.lock:
                    c.pending_rx.append(bytes(data))

    # ── accept + handler por conexão ─────────────────────────────────────

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._srv.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = _Conn(sock)
            self._conns.append(conn)
            self.connections_made += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _send(self, conn: _Conn, data: bytes) -> None:
        try:
            conn.sock.sendall(data)
            conn.last_send = _now()
        except OSError:
            pass

    def _handle(self, conn: _Conn) -> None:
        # modem inicia o handshake mandando o seu CONNECT (igual ao tcp_accept)
        self._send(conn, build_connect(PROTOCOL_VERSION))
        conn.sock.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data = conn.sock.recv(4096)
            except socket.timeout:
                self._tick(conn)
                continue
            except OSError:
                break
            if not data:
                break
            conn.reader.feed(data)
            for ptype, payload in conn.reader.read_all():
                if conn.phase == "handshake":
                    self._handshake(conn, ptype, payload)
                else:
                    self._dispatch_running(conn, ptype, payload)
            self._tick(conn)
        self._cleanup(conn)

    def _cleanup(self, conn: _Conn) -> None:
        try:
            conn.sock.close()
        except OSError:
            pass
        if conn in self._conns:
            self._conns.remove(conn)

    # ── handshake ────────────────────────────────────────────────────────

    def _handshake(self, conn: _Conn, ptype: int, payload: bytes) -> None:
        if conn.hstep == "WAIT_CONNECT":
            if ptype == PacketType.CONNECT:
                if (not payload) or ConnectPayload.decode(payload).version != PROTOCOL_VERSION:
                    self._cleanup(conn)
                    return
                self._send(conn, build_connectack(PROTOCOL_VERSION))
                conn.hstep = "WAIT_ACK"
        elif conn.hstep == "WAIT_ACK":
            if ptype == PacketType.CONNECTACK:
                if (not payload) or ConnectPayload.decode(payload).version != PROTOCOL_VERSION:
                    self._cleanup(conn)
                    return
                self._send(conn, self._data(bytes((PayloadCommand.CONNECTION_PROBE,))))
                conn.hstep = "WAIT_PROBE"
        elif conn.hstep == "WAIT_PROBE":
            if ptype == PacketType.DATA and payload and payload[0] == PayloadCommand.CONNECTION_PROBE:
                self._handshake_done(conn)

    def _handshake_done(self, conn: _Conn) -> None:
        # Initial Setup
        self._send(conn, self._data(InitialSetupPayload(
            round_trip_time=5, min_socket_latency=0, max_socket_latency=5000,
            sync_flag=self.sync_flag, async_data_bits=3, async_stop_bits=0,
            async_parity=0, async_data_mode=0).encode()))
        # Transmit Setup
        self._send(conn, self._data(TransmitSetupPayload(
            tx_data_rate=self.data_rate, tx_blocking_factor=self.blocking_factor).encode()))
        # Tx Status (FLUSHED)
        self._send_tx_status(conn)
        # Carrier Detect (sem portadora)
        self._send_carrier(conn, detected=False)
        conn.phase = "running"

    # ── dispatch (running) ───────────────────────────────────────────────

    def _dispatch_running(self, conn: _Conn, ptype: int, payload: bytes) -> None:
        if ptype == PacketType.ERROR:
            self._cleanup(conn)
            return
        if ptype != PacketType.DATA or not payload:
            return                          # keep-alive vazio
        cmd = payload[0]
        if cmd == PayloadCommand.TRANSMIT_ARM:
            self._on_arm(conn)
        elif cmd == PayloadCommand.TRANSMIT_START:
            self._on_start(conn)
        elif cmd == PayloadCommand.DATA_TRANSFER:
            self._on_data(conn, payload)
        elif cmd == PayloadCommand.REQUEST_TX_STATUS:
            self._send_tx_status(conn)
        elif cmd == PayloadCommand.CONNECTION_PROBE:
            self._send(conn, self._data(bytes((PayloadCommand.CONNECTION_PROBE,))))
        # ABORT_RECEPTION e outros: ignora

    def _on_arm(self, conn: _Conn) -> None:
        if conn.tx_state != TxStateWire.FLUSHED:
            return
        conn.fifo.clear()
        if self.arm_ready_delay > 0:
            conn.tx_state = TxStateWire.QUEUES_ARMED_PORT_NOT_READY
            conn.armed_at = _now()
        else:
            conn.tx_state = TxStateWire.QUEUES_ARMED_PORT_READY
        self._send_tx_status(conn)

    def _on_start(self, conn: _Conn) -> None:
        if conn.tx_state != TxStateWire.QUEUES_ARMED_PORT_READY:
            self._send_tx_status(conn)
            return
        conn.tx_state = TxStateWire.STARTED
        self._send_tx_status(conn)

    def _on_data(self, conn: _Conn, payload: bytes) -> None:
        if conn.tx_state not in (TxStateWire.QUEUES_ARMED_PORT_READY, TxStateWire.STARTED):
            # NAK: filas não armadas (espelha tcp_on_data_transfer)
            dt = DataTransferPayload.decode(payload)
            self._send(conn, self._data(TxDataNakPayload(
                cause=0, nacked_packet_id=dt.packet_id).encode()))
            return
        dt = DataTransferPayload.decode(payload)
        conn.fifo += dt.data
        is_last = dt.packet_order in (PacketOrder.LAST, PacketOrder.FIRST_AND_LAST)
        if is_last:
            burst = bytes(conn.fifo)
            conn.fifo.clear()
            conn.tx_state = TxStateWire.FLUSHED
            self._send_tx_status(conn)
            self.bursts_received += 1
            if self.on_air_tx is not None and burst:
                self.on_air_tx(burst)       # key-up: entrega ao "ar"

    # ── tick: keep-alive, promoção NOT_READY→READY, RX pump ──────────────

    def _tick(self, conn: _Conn) -> None:
        if conn.phase != "running":
            return
        now = _now()
        # Receiver Master: promove para PORT_READY após o atraso configurado
        if (conn.tx_state == TxStateWire.QUEUES_ARMED_PORT_NOT_READY
                and conn.armed_at is not None and now - conn.armed_at >= self.arm_ready_delay):
            conn.tx_state = TxStateWire.QUEUES_ARMED_PORT_READY
            conn.armed_at = None
            self._send_tx_status(conn)
        # keep-alive
        if now - conn.last_send >= self.keepalive_period:
            self._send(conn, encode_packet(PacketType.DATA, b""))
        # RX pump: entrega bursts pendentes do ar
        bursts: list[bytes] = []
        with conn.lock:
            if conn.pending_rx:
                bursts = conn.pending_rx
                conn.pending_rx = []
        for burst in bursts:
            self._deliver_rx_burst(conn, burst)

    def _deliver_rx_burst(self, conn: _Conn, burst: bytes) -> None:
        self._send_carrier(conn, detected=True)
        chunks = [burst[i:i + MAX_TCP_DATA_BYTES] for i in range(0, len(burst), MAX_TCP_DATA_BYTES)]
        n = len(chunks)
        for idx, chunk in enumerate(chunks):
            order = PacketOrder.FIRST_ONLY if idx == 0 else PacketOrder.CONTINUATION
            self._send(conn, self._data(DataTransferPayload(order, bytes(12), chunk).encode()))
        # LAST vazio = EOM (espelha tcp_rx_pump em queda de portadora)
        self._send(conn, self._data(DataTransferPayload(PacketOrder.LAST, bytes(12), b"").encode()))
        self._send_carrier(conn, detected=False)

    # ── encoders auxiliares ──────────────────────────────────────────────

    @staticmethod
    def _data(payload: bytes) -> bytes:
        return encode_packet(PacketType.DATA, payload)

    def _send_tx_status(self, conn: _Conn) -> None:
        self._send(conn, self._data(TxStatusPayload(
            tx_state=conn.tx_state,
            serial_fifo_space=65536 - len(conn.fifo),
            serial_fifo_fill=len(conn.fifo),
            fifo_critical_ms=0, fifo_critical_bytes=0).encode()))

    def _send_carrier(self, conn: _Conn, *, detected: bool) -> None:
        self._send(conn, self._data(CarrierDetectPayload(
            carrier_state=CarrierState.DETECTED if detected else CarrierState.NONE,
            rx_data_rate=self.data_rate if detected else 0,
            rx_blocking_factor=self.blocking_factor if detected else 0).encode()))


class MockAir:
    """Cruza dois `MockModem110d`: TX de A → RX de B e vice-versa (dois nós)."""

    def __init__(self, **kwargs) -> None:
        self.modem_a = MockModem110d(**kwargs)
        self.modem_b = MockModem110d(**kwargs)
        self.modem_a.on_air_tx = self.modem_b.deliver_air_rx
        self.modem_b.on_air_tx = self.modem_a.deliver_air_rx

    def start(self) -> "MockAir":
        self.modem_a.start()
        self.modem_b.start()
        return self

    def stop(self) -> None:
        self.modem_a.stop()
        self.modem_b.stop()
