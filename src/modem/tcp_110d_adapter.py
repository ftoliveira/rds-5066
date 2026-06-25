"""Adaptador DTE: STANAG 5066 (DTS) ↔ MIL-STD-188-110D Annex A sobre TCP.

Implementa o contrato *duck-typed* de `ModemInterface` falando o protocolo TCP
(TDSI) do Anexo A com o modem `rds-hf` (que é o servidor/DCE). O núcleo
DTS/ARQ/CAS/SIS do nó STANAG não muda: este adaptador entra como mais um modem.

Modelo de threads (PLANO §8):
  - `tick()` do StanagNode (externo): chama `modem_tx_burst`/`modem_rx_read_frame`
    e **nunca bloqueia** — TX apenas enfileira janelas.
  - Thread de conexão/RX: connect → handshake → loop `recv` → `PacketReader` →
    dispatch; mantém estado e `notify`. Reconecta com backoff em queda.
  - Thread TX worker: drena a fila de janelas; orquestra ARM → pré-fill → START →
    CONTINUATION… → LAST → DRAIN para cada janela (1 janela = 1 burst OTA).
  - Thread keep-alive: envia DATA vazio a cada 2 s; sem DATA por 30 s → reconecta.

Fidelidade de byte é garantida pelo codec puro `appendix_a_codec` (validado
contra os vetores golden do C do modem). O re-split de D_PDUs recebidos usa o
helper compartilhado `dpdu_framing`.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from queue import Empty, Queue

from src.flow_log import flow_rx, flow_tx
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
    build_data_transfer,
    build_keepalive,
)
from src.modem.dpdu_framing import DpduReassembler
from src.modem_if import ModemConfig, ModemInterface


def _now() -> float:
    return time.monotonic()


class _Disconnect(Exception):
    """Sinaliza que a conexão deve ser derrubada (ex.: pacote ERROR do modem)."""


@dataclass(slots=True)
class Tcp110dConfig:
    """Configuração do adaptador DTE 110D/TCP (PLANO §4.4)."""

    host: str = "127.0.0.1"
    port: int = 3000                       # porta TCP fixa do Anexo A (PLANO §10)

    # timeouts de handshake (lan_constants.h / A.5.1.2)
    connect_timeout: float = 3.0
    connectack_timeout: float = 3.0
    probe_timeout: float = 6.0

    # keep-alive (A.5.1.2): envia a cada 2 s; sem DATA por 30 s → reconecta
    keepalive_period: float = 2.0
    keepalive_timeout: float = 30.0

    # janela TX
    prefill_blocking_factors: int = 3      # pré-fill ≥ N × TxBlockingFactor antes do START
    max_data_bytes: int = MAX_TCP_DATA_BYTES
    tx_flushed_timeout: float = 5.0        # espera FLUSHED antes do ARM
    tx_ready_timeout: float = 30.0         # espera PORT_READY (tolera Receiver Master)
    tx_drain_timeout: float = 10.0         # espera retorno a FLUSHED após LAST
    start_retry_period: float = 0.010      # reenvio de START (A.5.1.2: 10 ms)
    start_retries: int = 50

    expect_sync_mode: bool = True          # modo síncrono obrigatório (Annex D)

    # reconexão (backoff exponencial)
    reconnect_backoff_initial: float = 0.5
    reconnect_backoff_max: float = 8.0

    # taxa inicial reportada antes do Transmit Setup do modem
    data_rate_bps: int = 2400


class Tcp110dModemAdapter(ModemInterface):
    """`ModemInterface` que transporta D_PDUs via o protocolo TCP do Anexo A."""

    def __init__(self, config: Tcp110dConfig | None = None, *, max_buffer_bytes: int = 8192) -> None:
        cfg = config or Tcp110dConfig()
        super().__init__(config=ModemConfig(
            data_rate_bps=cfg.data_rate_bps,
            tx_enable=True,
            rx_carrier_detect=False,
            max_buffer_bytes=max_buffer_bytes,
        ))
        self.tcp_config = cfg

        # threads & lifecycle
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._started = False
        self._conn_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None
        self._ka_thread: threading.Thread | None = None

        # socket + serialização de escrita
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._reader = PacketReader()

        # fila de janelas TX (1 item = 1 burst OTA); deque de D_PDUs RX
        self._tx_queue: Queue[list[bytes]] = Queue()
        self._rx_frames: deque[bytes] = deque()
        self._rx_lock = threading.Lock()
        self._rx_reassembler = DpduReassembler()

        # estado compartilhado (protegido por _cond)
        self._cond = threading.Condition()
        self._tx_state: int = TxStateWire.FLUSHED
        self._fifo_space: int = 0
        self._fifo_fill: int = 0
        self._fifo_critical_ms: int = 0
        self._fifo_critical_bytes: int = 0
        self._carrier_state: bool = False
        self._tx_data_rate: int = cfg.data_rate_bps
        self._tx_blocking_factor: int = 0
        self._rx_data_rate: int = 0
        self._rx_blocking_factor: int = 0
        self._rtt_ms: int = 0
        self._last_send: float = 0.0
        self._last_recv: float = 0.0

    # ── estado público (para UI / introspecção) ─────────────────────────

    @property
    def is_connected(self) -> bool:
        """True quando o handshake com o modem está concluído e o link ativo."""
        return self._connected.is_set()

    @property
    def reported_data_rate(self) -> int:
        """Última TxDataRate reportada pelo modem (Transmit Setup), em bps."""
        return self.config.data_rate_bps

    @property
    def reported_blocking_factor(self) -> int:
        return self._tx_blocking_factor

    # ── ciclo de vida ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._conn_thread = threading.Thread(target=self._run_connection, name="110d-conn", daemon=True)
        self._tx_thread = threading.Thread(target=self._run_tx_worker, name="110d-tx", daemon=True)
        self._ka_thread = threading.Thread(target=self._run_keepalive, name="110d-ka", daemon=True)
        self._conn_thread.start()
        self._tx_thread.start()
        self._ka_thread.start()
        flow_rx("110D", f"adaptador iniciado → alvo {self.tcp_config.host}:{self.tcp_config.port}")

    def stop(self) -> None:
        self._stop.set()
        self._connected.clear()
        with self._cond:
            self._cond.notify_all()
        self._force_disconnect("stop")
        for th in (self._conn_thread, self._tx_thread, self._ka_thread):
            if th is not None and th.is_alive() and th is not threading.current_thread():
                th.join(timeout=2.0)
        self._started = False

    # ── ModemInterface (duck-typed) ──────────────────────────────────────

    def modem_rx_start(self) -> None:
        self._rx_started = True
        self.start()

    def modem_rx_stop(self) -> None:
        self._rx_started = False
        self.stop()

    def modem_set_tx_enable(self, enabled: bool) -> None:
        self._tx_enabled = bool(enabled)

    def modem_tx_burst(self, frames: list[bytes]) -> int:
        """Enfileira uma janela (1 burst OTA) e retorna — **nunca bloqueia**."""
        if not self._started:
            self.start()
        window = [bytes(f) for f in frames if f]
        if not window:
            return 0
        total = sum(len(f) for f in window)
        if total > self.config.max_buffer_bytes:
            raise ValueError("Frame burst exceeds configured modem buffer")
        self._tx_queue.put(window)
        return total

    def modem_tx_dpdu(self, dpdu_buffer: bytes, length: int | None = None) -> int:
        payload = bytes(dpdu_buffer[:length] if length is not None else dpdu_buffer)
        if not payload:
            return 0
        if len(payload) > self.config.max_buffer_bytes:
            raise ValueError("Frame exceeds configured modem buffer")
        if not self._started:
            self.start()
        self._tx_queue.put([payload])    # janela degenerada de 1 D_PDU
        return len(payload)

    def modem_rx_read_frame(self) -> bytes | None:
        with self._rx_lock:
            if self._rx_frames:
                return self._rx_frames.popleft()
        return None

    def modem_rx_read(self, max_len: int) -> bytes:
        frame = self.modem_rx_read_frame()
        if frame is None:
            return b""
        return frame[:max_len]

    def modem_get_carrier_status(self) -> bool:
        if not self._connected.is_set():
            return False
        with self._cond:
            carrier = self._carrier_state
        with self._rx_lock:
            pending = bool(self._rx_frames)
        return carrier or pending

    def connect(self, peer: ModemInterface) -> None:
        """Não usado (comunicação é por TCP com o modem)."""

    # ── thread de conexão/RX ─────────────────────────────────────────────

    def _run_connection(self) -> None:
        cfg = self.tcp_config
        backoff = cfg.reconnect_backoff_initial
        while not self._stop.is_set():
            sock = self._try_connect()
            if sock is None:
                self._stop.wait(backoff)
                backoff = min(backoff * 2, cfg.reconnect_backoff_max)
                continue
            try:
                ok = self._do_handshake(sock)
            except Exception as exc:                       # noqa: BLE001
                flow_rx("110D", f"handshake exceção: {exc!r}")
                ok = False
            if not ok:
                self._safe_close(sock)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, cfg.reconnect_backoff_max)
                continue

            backoff = cfg.reconnect_backoff_initial
            now = _now()
            with self._cond:
                self._last_recv = now
                self._last_send = now
            self._sock = sock
            self._connected.set()
            flow_rx("110D", f"conectado e handshake OK ({cfg.host}:{cfg.port})")
            try:
                self._run_rx_loop(sock)
            finally:
                self._connected.clear()
                self._sock = None
                with self._cond:
                    self._cond.notify_all()    # acorda worker/keepalive/waiters
                self._safe_close(sock)
                self._rx_reassembler.reset()
                flow_rx("110D", "desconectado do modem")
            self._stop.wait(cfg.reconnect_backoff_initial)

    def _try_connect(self) -> socket.socket | None:
        cfg = self.tcp_config
        try:
            sock = socket.create_connection((cfg.host, cfg.port), timeout=cfg.connect_timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock
        except OSError as exc:
            flow_rx("110D", f"connect {cfg.host}:{cfg.port} falhou: {exc}")
            return None

    def _do_handshake(self, sock: socket.socket) -> bool:
        """Handshake reativo do DTE: CONNECT ↔ CONNECTACK ↔ CONNECTION_PROBE (§5)."""
        cfg = self.tcp_config
        self._reader.reset()
        self._send_on(sock, build_connect(PROTOCOL_VERSION))
        state = "WAIT_CONNECT"
        deadline = _now() + cfg.connect_timeout
        while not self._stop.is_set():
            remaining = deadline - _now()
            if remaining <= 0:
                flow_rx("110D", f"handshake timeout em {state}")
                return False
            sock.settimeout(remaining)
            try:
                data = sock.recv(4096)
            except socket.timeout:
                flow_rx("110D", f"handshake timeout (recv) em {state}")
                return False
            except OSError as exc:
                flow_rx("110D", f"handshake erro de socket em {state}: {exc}")
                return False
            if not data:
                return False
            self._reader.feed(data)
            for ptype, payload in self._reader.read_all():
                if state == "WAIT_CONNECT":
                    if ptype == PacketType.CONNECT:
                        ver = ConnectPayload.decode(payload).version if payload else -1
                        if ver != PROTOCOL_VERSION:
                            flow_rx("110D", f"CONNECT versão {ver} != {PROTOCOL_VERSION} — abortando")
                            return False
                        self._send_on(sock, build_connectack(PROTOCOL_VERSION))
                        state = "WAIT_ACK"
                        deadline = _now() + cfg.connectack_timeout
                elif state == "WAIT_ACK":
                    if ptype == PacketType.CONNECTACK:
                        ver = ConnectPayload.decode(payload).version if payload else -1
                        if ver != PROTOCOL_VERSION:
                            flow_rx("110D", f"CONNECTACK versão {ver} != {PROTOCOL_VERSION} — abortando")
                            return False
                        state = "WAIT_PROBE"
                        deadline = _now() + cfg.probe_timeout
                elif state == "WAIT_PROBE":
                    if (ptype == PacketType.DATA and payload
                            and payload[0] == PayloadCommand.CONNECTION_PROBE):
                        self._send_on(sock, build_command(PayloadCommand.CONNECTION_PROBE))
                        return True
        return False

    def _run_rx_loop(self, sock: socket.socket) -> None:
        sock.settimeout(0.5)
        while not self._stop.is_set() and self._connected.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            with self._cond:
                self._last_recv = _now()
            self._reader.feed(data)
            for ptype, payload in self._reader.read_all():
                try:
                    self._dispatch_packet(ptype, payload)
                except _Disconnect:
                    return
                except Exception as exc:                    # noqa: BLE001
                    flow_rx("110D", f"erro no dispatch: {exc!r}")

    # ── dispatch de pacotes (running) ────────────────────────────────────

    def _dispatch_packet(self, ptype: int, payload: bytes) -> None:
        if ptype == PacketType.ERROR:
            flow_rx("110D", "recebido ERROR (0xFF) — derrubando conexão")
            raise _Disconnect
        if ptype != PacketType.DATA:
            return                          # CONNECT/CONNECTACK fora do handshake: ignora
        if not payload:
            return                          # keep-alive vazio (last_recv já atualizado)
        cmd = payload[0]
        if cmd == PayloadCommand.DATA_TRANSFER:
            self._on_data_transfer(payload)
        elif cmd == PayloadCommand.TX_STATUS:
            self._on_tx_status(payload)
        elif cmd == PayloadCommand.CARRIER_DETECT:
            self._on_carrier_detect(payload)
        elif cmd == PayloadCommand.TRANSMIT_SETUP:
            self._on_transmit_setup(payload)
        elif cmd == PayloadCommand.INITIAL_SETUP:
            self._on_initial_setup(payload)
        elif cmd == PayloadCommand.TX_DATA_NAK:
            self._on_tx_data_nak(payload)
        elif cmd == PayloadCommand.CONNECTION_PROBE:
            try:
                self._send(build_command(PayloadCommand.CONNECTION_PROBE))
            except ConnectionError:
                pass
        # demais comandos: ignora silenciosamente

    def _on_data_transfer(self, payload: bytes) -> None:
        dt = DataTransferPayload.decode(payload)
        if dt.packet_order in (PacketOrder.FIRST_ONLY, PacketOrder.FIRST_AND_LAST):
            self._rx_reassembler.reset()    # nova recepção OTA — descarta parciais antigos
        new_frames = self._rx_reassembler.feed(dt.data)
        if new_frames:
            with self._rx_lock:
                self._rx_frames.extend(new_frames)
            for f in new_frames:
                flow_rx("110D", f"D_PDU recebido ({len(f)} B) → fila RX")

    def _on_tx_status(self, payload: bytes) -> None:
        st = TxStatusPayload.decode(payload)
        with self._cond:
            self._tx_state = st.tx_state
            self._fifo_space = st.serial_fifo_space
            self._fifo_fill = st.serial_fifo_fill
            self._fifo_critical_ms = st.fifo_critical_ms
            self._fifo_critical_bytes = st.fifo_critical_bytes
            self._cond.notify_all()

    def _on_carrier_detect(self, payload: bytes) -> None:
        cd = CarrierDetectPayload.decode(payload)
        with self._cond:
            self._carrier_state = (cd.carrier_state == CarrierState.DETECTED)
            self._rx_data_rate = cd.rx_data_rate
            self._rx_blocking_factor = cd.rx_blocking_factor
            self._cond.notify_all()

    def _on_transmit_setup(self, payload: bytes) -> None:
        ts = TransmitSetupPayload.decode(payload)
        with self._cond:
            self._tx_data_rate = ts.tx_data_rate
            self._tx_blocking_factor = ts.tx_blocking_factor
            self._cond.notify_all()
        if ts.tx_data_rate > 0:
            # respostas de management/DRC informam a taxa correta (PLANO §4.4)
            self.config.data_rate_bps = ts.tx_data_rate
        flow_rx("110D", f"Transmit Setup: taxa={ts.tx_data_rate} bps blocking={ts.tx_blocking_factor}")

    def _on_initial_setup(self, payload: bytes) -> None:
        is_ = InitialSetupPayload.decode(payload)
        with self._cond:
            self._rtt_ms = is_.round_trip_time
        if self.tcp_config.expect_sync_mode and is_.sync_flag != SyncFlag.SYNCHRONOUS:
            flow_rx("110D", "ERRO DE CONFIG: modem reportou SyncFlag=0 (assíncrono); "
                            "STANAG 5066 exige modo síncrono (Annex D)")
        flow_rx("110D", f"Initial Setup: rtt={is_.round_trip_time}ms sync={is_.sync_flag}")

    def _on_tx_data_nak(self, payload: bytes) -> None:
        nak = TxDataNakPayload.decode(payload)
        cause_name = {0: "QUEUES_NOT_ARMED", 1: "TRANSMIT_UNDERRUN",
                      2: "MISSING_FIRST", 3: "MULTIPLE_FIRST"}.get(nak.cause, str(nak.cause))
        flow_tx("110D", f"TX_DATA_NAK causa={cause_name} — janela atual pode ser abortada")
        with self._cond:
            self._cond.notify_all()

    # ── thread TX worker ─────────────────────────────────────────────────

    def _run_tx_worker(self) -> None:
        while not self._stop.is_set():
            try:
                window = self._tx_queue.get(timeout=0.2)
            except Empty:
                continue
            if window is None:
                continue
            if not self._connected.is_set():
                # Aguarda (de forma limitada) a conexão subir antes de desistir —
                # cobre a corrida de janela enfileirada antes do handshake inicial
                # e reconexões breves. O `tick()` nunca bloqueia; só o worker.
                deadline = _now() + self.tcp_config.tx_flushed_timeout
                while (not self._connected.is_set() and not self._stop.is_set()
                       and _now() < deadline):
                    self._connected.wait(0.2)
                if not self._connected.is_set():
                    flow_tx("110D", f"janela descartada (sem conexão), {len(window)} D_PDU(s)")
                    continue
            try:
                self._transmit_window(window)
            except (ConnectionError, OSError) as exc:
                flow_tx("110D", f"janela abortada (conexão caiu): {exc}")
            except Exception as exc:                        # noqa: BLE001
                flow_tx("110D", f"erro na janela TX: {exc!r}")

    def _transmit_window(self, window: list[bytes]) -> None:
        cfg = self.tcp_config
        stream = b"".join(window)
        if not stream:
            return
        max_data = cfg.max_data_bytes
        packets = [stream[i:i + max_data] for i in range(0, len(stream), max_data)]
        n = len(packets)
        if n == 1:
            orders = [PacketOrder.FIRST_AND_LAST]
        else:
            orders = [PacketOrder.FIRST_ONLY] + [PacketOrder.CONTINUATION] * (n - 2) + [PacketOrder.LAST]

        # 1. aguarda FLUSHED antes de armar
        if not self._wait_for(lambda: self._tx_state == TxStateWire.FLUSHED, cfg.tx_flushed_timeout):
            flow_tx("110D", "timeout aguardando FLUSHED — abortando janela")
            return
        # 2. ARM
        self._send(build_command(PayloadCommand.TRANSMIT_ARM))
        # 3. aguarda PORT_READY (tolera ARMED_NOT_READY — Receiver Master, §6)
        if not self._wait_for(
                lambda: self._tx_state == TxStateWire.QUEUES_ARMED_PORT_READY, cfg.tx_ready_timeout):
            flow_tx("110D", "timeout aguardando QUEUES_ARMED_PORT_READY — abortando janela")
            return

        # pré-fill (bytes) ≥ N × TxBlockingFactor, sem enviar o pacote LAST
        with self._cond:
            blocking = self._tx_blocking_factor
        prefill_threshold = cfg.prefill_blocking_factors * max(blocking, 1)

        i = 0
        sent_bytes = 0
        while i < n - 1 and sent_bytes < prefill_threshold:
            self._send(build_data_transfer(orders[i], os.urandom(12), packets[i]))
            sent_bytes += len(packets[i])
            i += 1

        # 4. START explícito (apenas janelas multi-pacote; pacote único usa FIRST_AND_LAST)
        if n > 1:
            self._send(build_command(PayloadCommand.TRANSMIT_START))
            self._wait_started()

        # 5. pacotes restantes (CONTINUATION… LAST); send bloqueante = backpressure
        while i < n:
            self._send(build_data_transfer(orders[i], os.urandom(12), packets[i]))
            i += 1

        # 6. aguarda DRAIN → FLUSHED (janela concluída)
        if not self._wait_for(lambda: self._tx_state == TxStateWire.FLUSHED, cfg.tx_drain_timeout):
            flow_tx("110D", "timeout aguardando FLUSHED pós-LAST (janela pode ter completado)")
        flow_tx("110D", f"janela TX concluída: {n} pacote(s) DATA, {len(stream)} B")

    def _wait_started(self) -> bool:
        cfg = self.tcp_config
        done = {TxStateWire.STARTED, TxStateWire.DRAINING_OK,
                TxStateWire.DRAINING_FORCED, TxStateWire.FLUSHED}
        for _ in range(cfg.start_retries):
            if self._wait_for(lambda: self._tx_state in done, cfg.start_retry_period):
                return True
            try:
                self._send(build_command(PayloadCommand.TRANSMIT_START))
            except ConnectionError:
                return False
        return False

    # ── thread keep-alive ────────────────────────────────────────────────

    def _run_keepalive(self) -> None:
        cfg = self.tcp_config
        while not self._stop.is_set():
            self._stop.wait(0.25)
            if self._stop.is_set():
                break
            if not self._connected.is_set():
                continue
            now = _now()
            with self._cond:
                last_send = self._last_send
                last_recv = self._last_recv
            if now - last_recv > cfg.keepalive_timeout:
                flow_rx("110D", f"keep-alive timeout ({cfg.keepalive_timeout}s sem DATA) — reconectando")
                self._force_disconnect("keepalive timeout")
                continue
            if now - last_send >= cfg.keepalive_period:
                try:
                    self._send(build_keepalive())
                except ConnectionError:
                    pass

    # ── helpers de socket / estado ───────────────────────────────────────

    def _send_on(self, sock: socket.socket, data: bytes) -> None:
        """Escrita serializada num socket específico (usado no handshake)."""
        with self._send_lock:
            try:
                sock.sendall(data)
            except OSError as exc:
                raise ConnectionError(str(exc)) from exc
        with self._cond:
            self._last_send = _now()

    def _send(self, data: bytes) -> None:
        """Escrita serializada no socket corrente (running). Levanta se desconectado."""
        with self._send_lock:
            sock = self._sock
            if sock is None or not self._connected.is_set():
                raise ConnectionError("não conectado")
            try:
                sock.sendall(data)
            except OSError as exc:
                raise ConnectionError(str(exc)) from exc
        with self._cond:
            self._last_send = _now()

    def _wait_for(self, predicate, timeout: float) -> bool:
        """Espera `predicate()` virar verdadeiro (sob _cond) ou timeout/desconexão."""
        deadline = _now() + timeout
        with self._cond:
            while not predicate():
                if not self._connected.is_set():
                    return False
                remaining = deadline - _now()
                if remaining <= 0:
                    return predicate()
                self._cond.wait(remaining)
            return True

    def _force_disconnect(self, reason: str) -> None:
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._connected.clear()
        with self._cond:
            self._cond.notify_all()

    @staticmethod
    def _safe_close(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass
