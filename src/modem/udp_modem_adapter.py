"""Adaptador de modem UDP direto — envia/recebe D_PDUs crus via UDP.

Substitui HFModemAdapter eliminando toda a pipeline de modem HF
(TxPipeline, RxPipeline, waveform baseband, calibração de ruído).

Mantém:
  - CSMA (sense window, backoff, max retries) idêntico ao HFModemAdapter
  - Half-duplex via _tx_in_progress Event
  - Thread de recepção UDP -> fila de frames
  - Batch TX com separador para o RX dividir D_PDUs
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from queue import Queue
from typing import Any

from src.flow_log import SYNC_BYTES, dpdu_wire_hint, flow_rx, flow_tx
from src.modem_if import ModemConfig, ModemInterface

# Marcadores de protocolo UDP direto
EOB = b"EOB"                          # Fim de burst (frame único)
BATCH_SEP = b"\xDE\xAD\xBE\xEF"      # Separador entre D_PDUs num batch

# ── CSMA ────────────────────────────────────────────────────────────────────
CSMA_SENSE_WINDOW_S: float = 0.05     # s — mais curto que HFModem (sem latência de waveform)
CSMA_BACKOFF_SLOT_S: float = 0.05     # s — slot base de backoff
CSMA_MAX_RETRIES: int = 20            # tentativas antes de forçar TX


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class UDPModemAdapter(ModemInterface):
    """ModemInterface que transporta D_PDUs crus via UDP (sem modem HF)."""

    def __init__(
        self,
        *,
        listen_port: int,
        target_address: tuple[str, int],
        sock: socket.socket | None = None,
        max_buffer_bytes: int = 8192,
        data_rate_bps: int = 2400,
    ) -> None:
        modem_config = ModemConfig(
            data_rate_bps=data_rate_bps,
            tx_enable=True,
            rx_carrier_detect=False,
            max_buffer_bytes=max_buffer_bytes,
        )
        super().__init__(config=modem_config)

        self._listen_port = listen_port
        self._target_address = target_address
        self._sock = sock
        self._rx_frames_queue: Queue[bytes] = Queue()
        self._rx_thread: threading.Thread | None = None
        self._rx_stop = threading.Event()

        # CSMA
        self._last_rx_activity: float = 0.0
        self._tx_in_progress = threading.Event()

    # ── CSMA ────────────────────────────────────────────────────────────

    def _csma_wait_for_clear(self) -> bool:
        """Espera canal livre antes de transmitir (CSMA)."""
        for attempt in range(1, CSMA_MAX_RETRIES + 1):
            # TX local em andamento
            if self._tx_in_progress.is_set():
                wait = CSMA_BACKOFF_SLOT_S * (1.0 + random.random())
                flow_tx("CSMA", f"Canal ocupado (TX local) — backoff {wait:.3f}s [{attempt}/{CSMA_MAX_RETRIES}]")
                time.sleep(wait)
                continue

            # RX ativo
            rx_age = time.time() - self._last_rx_activity
            if self._last_rx_activity > 0 and rx_age < CSMA_SENSE_WINDOW_S:
                wait = CSMA_BACKOFF_SLOT_S * (1.0 + random.random())
                flow_tx("CSMA", f"Canal ocupado (RX {rx_age*1000:.0f}ms) — backoff {wait:.3f}s [{attempt}/{CSMA_MAX_RETRIES}]")
                time.sleep(wait)
                continue

            # Canal livre
            if attempt > 1:
                flow_tx("CSMA", f"Canal livre após {attempt - 1} backoff(s)")
            return True

        flow_tx("CSMA", f"CSMA esgotou {CSMA_MAX_RETRIES} tentativas — forçando TX")
        return False

    # ── RX loop ─────────────────────────────────────────────────────────

    def _run_rx_loop(self) -> None:
        """Thread: recebe D_PDUs crus via UDP e coloca na fila."""
        if self._sock is None:
            return
        self._sock.settimeout(0.5)

        while not self._rx_stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError as e:
                print(f"[{_ts()}] [UDP Adapter] OSError RX na porta {self._listen_port}: {e}")
                break

            if not data:
                continue

            print(f"[{_ts()}] [UDP Adapter] RX UDP: recebido {len(data)} bytes de {addr} na porta {self._listen_port}")
            self._last_rx_activity = time.time()

            if data == EOB or data == b"EOF":
                # Marcador de controle, ignorar
                continue

            # Verificar se é um batch (contém separador)
            if BATCH_SEP in data:
                parts = data.split(BATCH_SEP)
                for part in parts:
                    if part and len(part) >= 2 and part[:2] == SYNC_BYTES:
                        self._rx_frames_queue.put(part)
                        hint = dpdu_wire_hint(part)
                        flow_rx("PHY", f"RX:{self._listen_port} D_PDU (batch) -> fila | {hint}")
            elif len(data) >= 2 and data[:2] == SYNC_BYTES:
                # Frame único
                self._rx_frames_queue.put(data)
                hint = dpdu_wire_hint(data)
                flow_rx("PHY", f"RX:{self._listen_port} D_PDU -> fila | {hint}")
            else:
                flow_rx("PHY", f"RX:{self._listen_port} dados descartados (sync!=90EB) len={len(data)}")

    # ── ModemInterface API ──────────────────────────────────────────────

    def modem_rx_start(self) -> None:
        self._rx_started = True
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(("0.0.0.0", self._listen_port))
            print(f"[{_ts()}] [UDP Adapter] Socket UDP bind em 0.0.0.0:{self._listen_port}")
        if self._rx_thread is None or not self._rx_thread.is_alive():
            self._rx_stop.clear()
            self._rx_thread = threading.Thread(target=self._run_rx_loop, daemon=True)
            self._rx_thread.start()

    def modem_rx_stop(self) -> None:
        self._rx_started = False
        self._rx_stop.set()

    def modem_tx_dpdu(self, dpdu_buffer: bytes, length: int | None = None) -> int:
        """Envia um D_PDU cru via UDP."""
        if not self._tx_enabled:
            self.modem_set_tx_enable(True)
        payload = bytes(dpdu_buffer[:length] if length is not None else dpdu_buffer)
        if len(payload) > self.config.max_buffer_bytes:
            raise ValueError("Frame exceeds configured modem buffer")
        if not payload:
            return 0

        self._csma_wait_for_clear()
        self._tx_in_progress.set()
        try:
            return self._do_tx_dpdu(payload)
        finally:
            self._tx_in_progress.clear()
            self._last_rx_activity = time.time()

    def _do_tx_dpdu(self, payload: bytes) -> int:
        """Envia D_PDU bytes diretamente via UDP."""
        hint = dpdu_wire_hint(payload)
        flow_tx("PHY", f"TX:{self._listen_port} -> {self._target_address[1]} | {hint}")
        print(f"[{_ts()}] [UDP Adapter] TX UDP único: enviando {len(payload)} bytes de {self._listen_port} para {self._target_address}")

        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.sendto(payload, self._target_address)
        return len(payload)

    def modem_tx_batch(self, dpdu_list: list[bytes]) -> int:
        """Transmite múltiplos D_PDUs num único datagrama UDP."""
        if not dpdu_list:
            return 0
        if len(dpdu_list) == 1:
            return self.modem_tx_dpdu(dpdu_list[0])

        if not self._tx_enabled:
            self.modem_set_tx_enable(True)

        self._csma_wait_for_clear()
        self._tx_in_progress.set()
        try:
            return self._do_tx_batch(dpdu_list)
        finally:
            self._tx_in_progress.clear()
            self._last_rx_activity = time.time()

    def _do_tx_batch(self, dpdu_list: list[bytes]) -> int:
        """Concatena D_PDUs com separador e envia num único datagrama UDP."""
        hints = []
        total_payload = 0
        for dpdu_buf in dpdu_list:
            payload = bytes(dpdu_buf)
            if len(payload) > self.config.max_buffer_bytes:
                raise ValueError("Frame exceeds configured modem buffer")
            if not payload:
                continue
            hints.append(dpdu_wire_hint(payload))
            total_payload += len(payload)

        # Concatenar com separador
        batch_data = BATCH_SEP.join(dpdu_list)
        hints_str = " + ".join(hints)
        flow_tx("PHY", f"TX:{self._listen_port} -> {self._target_address[1]} BATCH {len(dpdu_list)} D_PDUs | {hints_str}")
        print(f"[{_ts()}] [UDP Adapter] TX UDP batch: enviando {len(batch_data)} bytes ({len(dpdu_list)} D_PDUs) de {self._listen_port} para {self._target_address}")

        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.sendto(batch_data, self._target_address)
        return total_payload

    def modem_tx_burst(self, frames: list[bytes]) -> int:
        """Delega para modem_tx_batch."""
        return self.modem_tx_batch(frames)

    def modem_rx_read_frame(self) -> bytes | None:
        try:
            return self._rx_frames_queue.get_nowait()
        except Exception:
            return None

    def modem_rx_read(self, max_len: int) -> bytes:
        frame = self.modem_rx_read_frame()
        if frame is None:
            return b""
        return frame[:max_len]

    def modem_get_carrier_status(self) -> bool:
        if not self._rx_started:
            return False
        return not self._rx_frames_queue.empty()

    def connect(self, peer: ModemInterface) -> None:
        """Não usado (comunicação é por UDP)."""
        pass
