"""Adaptador do modem HF (TxPipeline/RxPipeline + UDP) para a interface STANAG ModemInterface.

Um D_PDU (bytes) é enviado como um burst: bytes -> bits -> baseband -> UDP (chunks + EOB).
Múltiplos D_PDUs (batch/burst) são concatenados em bytes -> bits -> 1 transmit_message_baseband
(1 preâmbulo + stream contínuo de D_PDUs + EOB), conforme STANAG 5066 Annex C.3.
RX acumula amostras até EOB, decodifica com receive_streaming, empacota bits em bytes,
e varre o stream resultante procurando sync words 0x90EB para enfileirar D_PDUs individuais.
"""

from __future__ import annotations

import random
import socket
import threading
import time
from queue import Queue
from typing import Any

import numpy as np

from src.flow_log import SYNC_BYTES, dpdu_wire_hint, flow_rx, flow_tx
from src.modem_if import ModemConfig, ModemInterface


# Marcador de fim de burst no UDP (igual ao usado no benchmark para fim de transmissão).
EOB = b"EOB"

# ── CSMA ────────────────────────────────────────────────────────────────────
# Janela de silêncio: se o último chunk RX chegou há menos de CSMA_SENSE_WINDOW_S
# segundos, o canal é considerado ocupado.
CSMA_SENSE_WINDOW_S: float = 0.40   # s — tempo mínimo de silêncio antes de TX
CSMA_BACKOFF_SLOT_S: float = 0.30   # s — slot base de backoff (+ jitter aleatório)
CSMA_MAX_RETRIES: int = 20          # tentativas antes de forçar TX mesmo assim


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _dpdu_bytes_to_bits(dpdu_bytes: bytes) -> "Any":
    """Converte D_PDU bytes em array de bits (int8) para TxPipeline."""
    arr = np.frombuffer(dpdu_bytes, dtype=np.uint8)
    bits = np.unpackbits(arr).astype(np.int8)
    return bits


def _bits_to_dpdu_bytes(bits: "Any") -> bytes:
    """Empacota bits decodificados (int8) em bytes (múltiplo de 8)."""
    bits = np.asarray(bits, dtype=np.int8)
    byte_len = len(bits) // 8
    if byte_len == 0:
        return b""
    valid_bits = bits[: byte_len * 8]
    return np.packbits(valid_bits).tobytes()


# Nibbles de DPDUType que carregam payload de dados + CRC de dados.
# DATA_ONLY=0, DATA_ACK=2, EXPEDITED_DATA_ONLY=4, NON_ARQ=7, EXPEDITED_NON_ARQ=8
_DATA_CRC_NIBBLES: frozenset[int] = frozenset({0, 2, 4, 7, 8})


def _dpdu_wire_size(stream: bytes, offset: int) -> int:
    """Retorna o tamanho em bytes do D_PDU que começa em stream[offset].

    Suporta todos os tipos definidos em STANAG 5066 Annex C.
    Lança ValueError se o stream for curto ou o sync estiver ausente.
    """
    if len(stream) < offset + 8:
        raise ValueError("Stream curto demais para conter um D_PDU")
    if stream[offset:offset + 2] != SYNC_BYTES:
        raise ValueError(f"Sync 0x90EB não encontrado em offset {offset}")
    header_size = stream[offset + 5] & 0x1F
    address_size = (stream[offset + 5] >> 5) & 0x07
    # HDR_SIZE excludes address (v3 mandatory, C.3.2.5)
    payload_rel = 2 + header_size + address_size + 2
    dpdu_type_nibble = (stream[offset + 2] >> 4) & 0x0F
    if dpdu_type_nibble in _DATA_CRC_NIBBLES:
        ts_offset = offset + 6 + address_size         # type-specific header start (absoluto)
        if len(stream) < ts_offset + 2:
            raise ValueError("Stream curto para ler data_len")
        first = stream[ts_offset]
        second = stream[ts_offset + 1]
        data_len = ((first & 0x03) << 8) | second
        return payload_rel + data_len + 4             # tamanho relativo: dados + data_crc(4 bytes, CRC-32)
    return payload_rel                                # sem payload de dados


def _dpdu_split_stream(stream: bytes) -> list[bytes]:
    """Divide um stream de bytes contendo D_PDUs concatenados em D_PDUs individuais.

    Percorre o stream de offset em offset usando _dpdu_wire_size para saber
    onde cada D_PDU termina. Ignora bytes espúrios antes do próximo sync.
    """
    result: list[bytes] = []
    pos = 0
    length = len(stream)
    while pos < length:
        # Procurar próximo sync 0x90EB
        if stream[pos:pos + 2] != SYNC_BYTES:
            next_sync = stream.find(SYNC_BYTES, pos + 1)
            print("next_sync", next_sync)
            if next_sync == -1:
                break
            pos = next_sync
        try:
            size = _dpdu_wire_size(stream, pos)
        except ValueError:
            # Sync falso ou stream corrompido — avançar 1 byte e tentar de novo
            pos += 1
            continue
        if pos + size > length:
            break
        result.append(stream[pos:pos + size])
        pos += size
    print(f"------- [{_ts()}] [HFModem RX] Stream dividido em {len(result)} D_PDUs (total {length} bytes)")
    return result


class HFModemAdapter(ModemInterface):
    """Implementação de ModemInterface usando modem HF real (waveform sobre UDP)."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        listen_port: int,
        target_address: tuple[str, int],
        sock: socket.socket | None = None,
        max_buffer_bytes: int = 8192,
    ) -> None:
        # ModemConfig compatível com a base (STANAG usa max_buffer_bytes)
        modem_config = ModemConfig(
            data_rate_bps=config.get("common", {}).get("bit_rate", 2400),
            tx_enable=True,
            rx_carrier_detect=False,
            max_buffer_bytes=max_buffer_bytes,
        )
        super().__init__(config=modem_config)

        self._config = config
        # HDR_SIZE_INCLUDES_ADDR removed — v3 mandates HDR_SIZE excludes address
        self._listen_port = listen_port
        self._target_address = target_address
        self._sock = sock
        self._rx_frames_queue: Queue[bytes] = Queue()
        self._rx_thread: threading.Thread | None = None
        self._rx_stop = threading.Event()
        self._rx_buffer = bytearray()
        self._noise_calibration: Any = None
        self._sample_rate: float = 0.0
        self._tx_pipeline: Any = None
        self._rx_pipeline: Any = None
        # CSMA: timestamp do último chunk RX recebido (0 = nunca)
        self._last_rx_activity: float = 0.0
        # CSMA: sinaliza que uma TX está em andamento neste nó
        self._tx_in_progress = threading.Event()

    def _ensure_tx_pipeline(self) -> Any:
        if self._tx_pipeline is None:
            from tx.pipeline import TxPipeline
            c = self._config.get("common", {})
            self._tx_pipeline = TxPipeline(
                bit_rate=c.get("bit_rate", 2400),
                interleave=c.get("interleave", "short"),
            )
        return self._tx_pipeline

    def _ensure_rx_pipeline(self) -> Any:
        if self._rx_pipeline is None:
            from rx.pipeline import RxPipeline
            c = self._config.get("common", {})
            self._rx_pipeline = RxPipeline(
                bit_rate=c.get("bit_rate", 2400),
                interleave=c.get("interleave", "short"),
            )
        return self._rx_pipeline

    def _get_sample_rate(self) -> float:
        if self._sample_rate <= 0:
            from core.constants import SYMBOL_RATE
            c = self._config.get("common", {})
            self._sample_rate = float(SYMBOL_RATE * c.get("sps", 20))
        return self._sample_rate

    def _csma_wait_for_clear(self) -> bool:
        """CSMA: espera canal livre antes de transmitir.

        Considera canal ocupado se:
          - outra TX deste nó está em andamento (_tx_in_progress), ou
          - chunks RX chegaram recentemente (menos de CSMA_SENSE_WINDOW_S atrás).

        Retorna True se o canal ficou livre; False se MAX_RETRIES foi esgotado
        (nesse caso a TX é forçada com aviso de log).
        """
        for attempt in range(1, CSMA_MAX_RETRIES + 1):
            # 1) TX própria em andamento (half-duplex: aguardar conclusão)
            if self._tx_in_progress.is_set():
                wait = CSMA_BACKOFF_SLOT_S * (1.0 + random.random())
                flow_tx("CSMA", f"Canal ocupado (TX local em andamento) — backoff {wait:.2f}s [tentativa {attempt}/{CSMA_MAX_RETRIES}]")
                time.sleep(wait)
                continue

            # 2) Canal ocupado por RX ativo (outra estação transmitindo)
            rx_age = time.time() - self._last_rx_activity
            if self._last_rx_activity > 0 and rx_age < CSMA_SENSE_WINDOW_S:
                wait = CSMA_BACKOFF_SLOT_S * (1.0 + random.random())
                flow_tx("CSMA", f"Canal ocupado (RX ativo, {rx_age*1000:.0f}ms atrás) — backoff {wait:.2f}s [tentativa {attempt}/{CSMA_MAX_RETRIES}]")
                time.sleep(wait)
                continue

            # Canal livre
            if attempt > 1:
                flow_tx("CSMA", f"Canal livre após {attempt - 1} backoff(s) — iniciando TX")
            else:
                flow_tx("CSMA", "Canal livre — iniciando TX imediatamente")
            return True

        flow_tx("CSMA", f"CSMA esgotou {CSMA_MAX_RETRIES} tentativas — forçando TX (possível colisão)")
        return False

    def _run_rx_loop(self) -> None:
        """Thread: recebe UDP, acumula até EOB, decodifica e coloca D_PDUs na fila."""
        c_common = self._config.get("common", {})
        c_rx = self._config.get("rx", {})
        sps = c_common.get("sps", 20)
        alpha = c_common.get("alpha", 0.25)
        sample_rate = self._get_sample_rate()
        sock_timeout = 0.5

        if self._sock is None:
            return
        self._sock.settimeout(sock_timeout)

        rx = self._ensure_rx_pipeline()
        eq_algorithm = c_rx.get("equalizer", "rls")
        eq_kw = {"eq_algorithm": eq_algorithm}
        if eq_algorithm == "rls":
            eq_kw["eq_num_taps"] = 21

        while not self._rx_stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break

            if data == EOB or data == b"EOF":
                if len(self._rx_buffer) == 0:
                    continue
                n_samples = len(self._rx_buffer) // 8  # complex64 = 8 bytes
                flow_rx("PHY", f"RX:{self._listen_port} EOB ~{n_samples} amostras -> decodificando")
                rx_signal = np.frombuffer(bytes(self._rx_buffer), dtype=np.complex64)
                self._rx_buffer.clear()
                if len(rx_signal) == 0:
                    continue

                try:
                    msgs = rx.receive_streaming(
                        rx_signal,
                        samples_per_symbol=sps,
                        alpha=alpha,
                        sample_rate=sample_rate,
                        equalize=c_rx.get("run_equalizer", True),
                        eq_fractional=c_rx.get("eq_fractional", True),
                        viterbi_soft=c_rx.get("viterbi_soft", True),
                        eq_kwargs=eq_kw,
                        energy_kwargs={
                            "threshold_db": c_rx.get("energy_threshold_db", 2.0),
                            "hysteresis_db": c_rx.get("energy_hysteresis_db", 2.0),
                        },
                        sync_kwargs={"threshold": c_rx.get("sync_threshold", 0.35)},
                        afc_kwargs={
                            "fft_size": c_rx.get("afc_fft_size", 32768),
                            "search_range": c_rx.get("afc_search_range", 40.0),
                        },
                        enable_preamble_refinement=c_rx.get("enable_preamble_refinement", True),
                        noise_calibration=self._noise_calibration,
                    )
                except Exception as e:
                    print(f"[{_ts()}] [HFModem RX] decode falhou: {e!r}")
                    continue
                for msg in msgs or []:
                    raw_bits = msg.get("data")
                    if raw_bits is not None and len(raw_bits) > 0:
                        frame_bytes = _bits_to_dpdu_bytes(raw_bits)
                        if not frame_bytes or len(frame_bytes) > self.config.max_buffer_bytes:
                            continue
                        if len(frame_bytes) < 2 or frame_bytes[:2] != SYNC_BYTES:
                            flow_rx("PHY", f"RX:{self._listen_port} D_PDU descartado (sync!=90EB) len={len(frame_bytes)}")
                            continue
                        # Separar D_PDUs individuais do stream (burst pode conter N D_PDUs)
                        dpdus = _dpdu_split_stream(frame_bytes)
                        if not dpdus:
                            flow_rx("PHY", f"RX:{self._listen_port} stream sem D_PDUs válidos len={len(frame_bytes)}")
                            continue
                        for dpdu_bytes in dpdus:
                            self._rx_frames_queue.put(dpdu_bytes)
                            hint = dpdu_wire_hint(dpdu_bytes)
                            flow_rx("PHY", f"RX:{self._listen_port} D_PDU -> fila | {hint}")
            else:
                self._rx_buffer.extend(data)
                # CSMA: canal ocupado enquanto chegam chunks de sinal
                self._last_rx_activity = time.time()

    def modem_rx_start(self) -> None:
        self._rx_started = True
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(("0.0.0.0", self._listen_port))
        if self._rx_thread is None or not self._rx_thread.is_alive():
            self._rx_stop.clear()
            self._rx_thread = threading.Thread(target=self._run_rx_loop, daemon=True)
            self._rx_thread.start()

    def modem_rx_stop(self) -> None:
        self._rx_started = False
        self._rx_stop.set()

    def modem_tx_dpdu(self, dpdu_buffer: bytes, length: int | None = None) -> int:
        if not self._tx_enabled:
            self.modem_set_tx_enable(True)
        payload = bytes(dpdu_buffer[:length] if length is not None else dpdu_buffer)
        if len(payload) > self.config.max_buffer_bytes:
            raise ValueError("Frame exceeds configured modem buffer")
        if not payload:
            return 0

        # CSMA — sense before transmit
        self._csma_wait_for_clear()
        self._tx_in_progress.set()
        try:
            return self._do_tx_dpdu(payload)
        finally:
            self._tx_in_progress.clear()
            # Guard time pós-TX: força CSMA_SENSE_WINDOW_S de silêncio antes da
            # próxima TX, dando tempo ao receptor remoto de reiniciar o pipeline.
            self._last_rx_activity = time.time()

    def _do_tx_dpdu(self, payload: bytes) -> int:
        hint = dpdu_wire_hint(payload)
        flow_tx("PHY", f"TX:{self._listen_port} -> {self._target_address[1]} burst | {hint}")

        data_bits = _dpdu_bytes_to_bits(payload)
        c_common = self._config.get("common", {})
        c_tx = self._config.get("tx", {})
        pre_preamble_ms = c_tx.get("pre_preamble_ms", 200.0)
        chunk_size = c_tx.get("chunk_size", 4800)

        tx = self._ensure_tx_pipeline()
        tx_signal, _, _ = tx.transmit_message_baseband(
            data_bits,
            samples_per_symbol=c_common.get("sps", 20),
            alpha=c_common.get("alpha", 0.25),
            pre_preamble_ms=pre_preamble_ms,
        )
        fs = self._get_sample_rate()
        total_samples = len(tx_signal)
        tx_signal = np.asarray(tx_signal, dtype=np.complex64)
        total_sent = 0
        start_time = time.time()
        chunk_count = 0
        while total_sent < total_samples:
            end_idx = min(total_sent + chunk_size, total_samples)
            chunk = tx_signal[total_sent:end_idx]
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.sendto(chunk.tobytes(), self._target_address)
            chunk_count += 1
            total_sent = end_idx
            expected_elapsed = total_sent / fs
            actual_elapsed = time.time() - start_time
            sleep_time = expected_elapsed - actual_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        if self._sock is not None:
            self._sock.sendto(EOB, self._target_address)
        flow_tx("PHY", f"TX:{self._listen_port} burst concluído {total_samples} amostras EOB | {hint}")
        return len(payload)

    def modem_tx_batch(self, dpdu_list: list[bytes]) -> int:
        """Transmite múltiplos D_PDUs em um único burst HF (half-duplex safe).

        Cada D_PDU recebe seu próprio preâmbulo; os sinais baseband são
        concatenados e enviados como um único burst com um EOB final.
        O RX consegue separar via receive_streaming (multi-mensagem).
        """
        if not dpdu_list:
            return 0
        if len(dpdu_list) == 1:
            return self.modem_tx_dpdu(dpdu_list[0])

        if not self._tx_enabled:
            self.modem_set_tx_enable(True)

        # CSMA — sense before transmit (batch)
        self._csma_wait_for_clear()
        self._tx_in_progress.set()
        try:
            return self._do_tx_batch(dpdu_list)
        finally:
            self._tx_in_progress.clear()
            # Guard time pós-TX: mesmo critério do modem_tx_dpdu.
            self._last_rx_activity = time.time()

    def _do_tx_batch(self, dpdu_list: list[bytes]) -> int:
        """Envia N D_PDUs em um único burst MIL-STD (1 preâmbulo + stream + EOB).

        Os bytes de todos os D_PDUs são concatenados e entregues ao modem como
        uma única chamada transmit_message_baseband, conforme STANAG 5066 Annex C.3.
        O RX decodifica um frame MIL-STD e usa _dpdu_split_stream para separar os
        D_PDUs individuais pelo sync 0x90EB.
        """
        c_common = self._config.get("common", {})
        c_tx = self._config.get("tx", {})
        sps = c_common.get("sps", 20)
        alpha = c_common.get("alpha", 0.25)
        pre_preamble_ms = c_tx.get("pre_preamble_ms", 200.0)
        chunk_size = c_tx.get("chunk_size", 4800)

        tx = self._ensure_tx_pipeline()
        fs = self._get_sample_rate()

        hints = []
        total_payload = 0
        burst_bytes = bytearray()
        for dpdu_buf in dpdu_list:
            payload = bytes(dpdu_buf)
            if len(payload) > self.config.max_buffer_bytes:
                raise ValueError("Frame exceeds configured modem buffer")
            if not payload:
                continue
            hints.append(dpdu_wire_hint(payload))
            total_payload += len(payload)
            burst_bytes.extend(payload)

        if not burst_bytes:
            return 0

        # Um único transmit_message_baseband para todo o stream de D_PDUs
        burst_bits = _dpdu_bytes_to_bits(bytes(burst_bytes))
        tx_signal, _, _ = tx.transmit_message_baseband(
            burst_bits,
            samples_per_symbol=sps,
            alpha=alpha,
            pre_preamble_ms=pre_preamble_ms,
        )
        tx_signal = np.asarray(tx_signal, dtype=np.complex64)
        total_samples = len(tx_signal)
        hints_str = " + ".join(hints)
        flow_tx("PHY", f"TX:{self._listen_port} -> {self._target_address[1]} BATCH {len(dpdu_list)} D_PDUs | {hints_str}")

        total_sent = 0
        start_time = time.time()
        chunk_count = 0
        while total_sent < total_samples:
            end_idx = min(total_sent + chunk_size, total_samples)
            chunk = tx_signal[total_sent:end_idx]
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.sendto(chunk.tobytes(), self._target_address)
            chunk_count += 1
            total_sent = end_idx
            expected_elapsed = total_sent / fs
            actual_elapsed = time.time() - start_time
            sleep_time = expected_elapsed - actual_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        if self._sock is not None:
            self._sock.sendto(EOB, self._target_address)
        flow_tx("PHY", f"TX:{self._listen_port} BATCH concluído {total_samples} amostras {len(dpdu_list)} D_PDUs EOB | {hints_str}")
        return total_payload

    def modem_tx_burst(self, frames: list[bytes]) -> int:
        """Delega para modem_tx_batch (burst HF real, Annex C.3)."""
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
        return not self._rx_frames_queue.empty() or len(self._rx_buffer) > 0

    def set_noise_calibration(self, calibration: Any) -> None:
        """Define calibração de ruído para o RX (array complex64)."""
        self._noise_calibration = calibration

    def connect(self, peer: ModemInterface) -> None:
        """Não usado no adaptador HF (comunicação é por UDP)."""
        pass
