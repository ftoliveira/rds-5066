"""Expedited Data ARQ Engine (stop-and-wait) para STANAG 5066.

Tipos 4 (EXPEDITED_DATA_ONLY) e 5 (EXPEDITED_ACK_ONLY).
Janela efetiva de 1 D_PDU (stop-and-wait), sequência separada do ARQ regular,
tracking de cpdu_id (8 bits).

Annex C.4.3: Expedited data transfer uses a separate sequence space
and stop-and-wait protocol per D_PDU segment.

C.3.7: Expedited D_PDUs support C_PDU START/END segmentation — large
C_PDUs are split into multiple D_PDUs, each sent with stop-and-wait.
"""

from __future__ import annotations

from typing import Callable, Optional
import time

from src.dpdu_frame import (
    build_expedited_ack_only,
    build_expedited_data_only,
    dpdu_calc_eot_field,
    dpdu_set_address,
    encode_dpdu,
)
from src.stypes import DPDU, DPDUType, Address

MAX_EXPEDITED_DATA_BYTES = 1023
EOW_EXPEDITED = 0
EXPEDITED_RETX_TIMEOUT_MS = 3000
EXPEDITED_MAX_RETRIES = 5
# C.3.7 spec (7): C_PDU ID modulo 16 for expedited
EXPEDITED_CPDU_ID_MOD = 16
# Frame sequence number: 0-255 exclusive pool for expedited
EXPEDITED_FSN_MOD = 256


def _log_exp(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [EXP-ARQ] {msg}")


class ExpeditedArqEngine:
    """Stop-and-wait ARQ para tipos 4/5 (Expedited Data).

    - Janela efetiva de 1: envia um segmento D_PDU, aguarda ACK antes do próximo.
    - C_PDUs > 1023 bytes são segmentados em múltiplos D_PDUs (C.3.7).
    - Fila separada de C_PDUs.
    - cpdu_id (8 bits) incrementado por C_PDU (avança só após último segmento ACKed).
    - tx_frame_seq (8 bits) incrementado por D_PDU dentro do C_PDU.
    """

    def __init__(
        self,
        local_node_address: int,
        remote_node_address: int,
        *,
        retx_timeout_ms: int = EXPEDITED_RETX_TIMEOUT_MS,
        max_retries: int = EXPEDITED_MAX_RETRIES,
        delivery_callback: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.local_node_address = local_node_address
        self.remote_node_address = remote_node_address
        self.retx_timeout_ms = retx_timeout_ms
        self.max_retries = max_retries
        self.delivery_callback = delivery_callback

        self._tx_queue: list[bytes] = []
        self._cpdu_id: int = 0
        self._tx_frame_seq: int = 0
        self._rx_frame_seq: int = 0

        # Estado TX stop-and-wait
        self._waiting_ack: bool = False
        self._current_encoded: Optional[bytes] = None
        self._current_dpdu: Optional[DPDU] = None
        self._tx_time_ms: int = 0
        self._retx_count: int = 0

        # Segmentos pendentes do C_PDU atual em transmissão
        self._tx_segments: list[tuple[DPDU, bytes]] = []

        # RX: buffer para reassembly do C_PDU corrente
        self._rx_parts: list[bytes] = []
        self._rx_cpdu_id: int = -1

        # ACK pendente
        self._pending_ack: Optional[bytes] = None

        self._delivered: list[bytes] = []

    def reset(self) -> None:
        """Reset contadores ao entrar/sair do estado EXPEDITED."""
        self._tx_queue.clear()
        self._cpdu_id = 0
        self._tx_frame_seq = 0
        self._rx_frame_seq = 0
        self._waiting_ack = False
        self._current_encoded = None
        self._current_dpdu = None
        self._retx_count = 0
        self._tx_segments.clear()
        self._rx_parts.clear()
        self._rx_cpdu_id = -1
        self._pending_ack = None
        self._delivered.clear()

    def submit_cpdu(self, payload: bytes) -> None:
        """Enfileira C_PDU para transmissão expedited."""
        if not payload:
            return
        self._tx_queue.append(bytes(payload))
        _log_exp(f"submit_cpdu len={len(payload)} queue_depth={len(self._tx_queue)}")

    def _segment_cpdu(self, payload: bytes) -> list[tuple[DPDU, bytes]]:
        """Segment C_PDU into EXPEDITED_DATA_ONLY D_PDUs (C.3.7)."""
        segments: list[tuple[DPDU, bytes]] = []
        addr = dpdu_set_address(
            destination=self.remote_node_address,
            source=self.local_node_address,
        )
        eot = dpdu_calc_eot_field(1)
        offset = 0
        total = len(payload)
        seq = self._tx_frame_seq
        while offset < total:
            chunk = payload[offset : offset + MAX_EXPEDITED_DATA_BYTES]
            pdu_start = offset == 0
            pdu_end = offset + len(chunk) >= total
            dpdu = build_expedited_data_only(
                EOW_EXPEDITED,
                eot,
                addr,
                chunk,
                tx_frame_seq=seq,
                cpdu_id=self._cpdu_id,
                pdu_start=pdu_start,
                pdu_end=pdu_end,
            )
            enc = encode_dpdu(dpdu)
            segments.append((dpdu, enc))
            seq = (seq + 1) % EXPEDITED_FSN_MOD
            offset += len(chunk)
        return segments

    def get_delivered_cpdus(self) -> list[bytes]:
        out = list(self._delivered)
        self._delivered.clear()
        return out

    def has_pending_tx(self) -> bool:
        if self._pending_ack is not None:
            return True
        if self._waiting_ack:
            return True
        if self._tx_segments:
            return True
        return bool(self._tx_queue)

    def process_tx(self, current_time_ms: int) -> list[bytes]:
        """Retorna lista de frames para enviar (burst)."""
        burst: list[bytes] = []
        frame = self._process_tx_single(current_time_ms)
        while frame is not None:
            burst.append(frame)
            frame = self._process_tx_single(current_time_ms)
        return burst

    def _process_tx_single(self, current_time_ms: int) -> Optional[bytes]:
        # ACK pendente tem prioridade
        if self._pending_ack is not None:
            enc = self._pending_ack
            self._pending_ack = None
            return enc

        # Retransmissão se timeout
        if self._waiting_ack and self._current_encoded is not None:
            if current_time_ms - self._tx_time_ms >= self.retx_timeout_ms:
                self._retx_count += 1
                if self._retx_count > self.max_retries:
                    _log_exp("max retries atingido, descartando C_PDU inteiro")
                    # Avançar seq passando os segmentos restantes
                    remaining = len(self._tx_segments)
                    self._tx_frame_seq = (
                        (self._tx_frame_seq + 1 + remaining) % EXPEDITED_FSN_MOD
                    )
                    self._cpdu_id = (self._cpdu_id + 1) % EXPEDITED_CPDU_ID_MOD
                    self._tx_segments.clear()
                    self._waiting_ack = False
                    self._current_encoded = None
                    self._current_dpdu = None
                    return None
                self._tx_time_ms = current_time_ms
                _log_exp(f"retransmit seq={self._tx_frame_seq} retx={self._retx_count}")
                return self._current_encoded
            return None

        # Se não aguardando ACK, preparar segmentos se necessário
        if not self._waiting_ack and not self._tx_segments and self._tx_queue:
            payload = self._tx_queue.pop(0)
            self._tx_segments = self._segment_cpdu(payload)
            _log_exp(
                f"C_PDU segmentada em {len(self._tx_segments)} segmento(s) "
                f"cpdu_id={self._cpdu_id}"
            )

        # Enviar próximo segmento se disponível
        if not self._waiting_ack and self._tx_segments:
            dpdu, enc = self._tx_segments.pop(0)
            self._current_encoded = enc
            self._current_dpdu = dpdu
            self._waiting_ack = True
            self._tx_time_ms = current_time_ms
            self._retx_count = 0
            _log_exp(
                f"TX expedited seq={dpdu.data.tx_frame_seq} cpdu_id={dpdu.data.cpdu_id} "
                f"pdu_start={dpdu.data.pdu_start} pdu_end={dpdu.data.pdu_end} "
                f"len={len(dpdu.user_data)}"
            )
            return enc

        return None

    def process_rx_dpdu(self, dpdu: DPDU) -> None:
        """Processa D_PDU recebido tipo 4 ou 5."""
        if dpdu.dpdu_type is DPDUType.EXPEDITED_DATA_ONLY:
            if dpdu.data is None:
                return
            # C.3.4§(6): Only D_PDUs with correct data CRC shall be acknowledged positively
            if dpdu.data_crc_ok is False:
                _log_exp("RX expedited data CRC ERROR, ignoring frame")
                return
            seq = dpdu.data.tx_frame_seq
            cpdu_id = dpdu.data.cpdu_id
            pdu_start = dpdu.data.pdu_start
            pdu_end = dpdu.data.pdu_end
            _log_exp(
                f"RX expedited data seq={seq} cpdu_id={cpdu_id} "
                f"pdu_start={pdu_start} pdu_end={pdu_end} len={len(dpdu.user_data)}"
            )

            # Reassembly com suporte a segmentação (C.3.7)
            skip_data = False
            if pdu_start:
                if self._rx_parts and cpdu_id != self._rx_cpdu_id:
                    _log_exp(
                        f"RX descartando C_PDU incompleto cpdu_id={self._rx_cpdu_id}"
                    )
                self._rx_parts = []
                self._rx_cpdu_id = cpdu_id
            elif cpdu_id != self._rx_cpdu_id or not self._rx_parts:
                _log_exp(
                    f"RX segmento órfão cpdu_id={cpdu_id} (esperado={self._rx_cpdu_id})"
                )
                skip_data = True

            if not skip_data:
                self._rx_parts.append(bytes(dpdu.user_data))
                if pdu_end:
                    payload = b"".join(self._rx_parts)
                    self._rx_parts = []
                    self._delivered.append(payload)
                    if self.delivery_callback:
                        self.delivery_callback(payload)

            # Enviar ACK para todo segmento recebido com CRC válido
            addr = dpdu_set_address(
                destination=dpdu.address.source,
                source=self.local_node_address,
            )
            eot = dpdu_calc_eot_field(1)
            ack_dpdu = build_expedited_ack_only(
                EOW_EXPEDITED, eot, addr, rx_lwe=seq
            )
            self._pending_ack = encode_dpdu(ack_dpdu)
            self._rx_frame_seq = (seq + 1) % EXPEDITED_FSN_MOD
            return

        if dpdu.dpdu_type is DPDUType.EXPEDITED_ACK_ONLY:
            if dpdu.ack is None:
                return
            rx_lwe = dpdu.ack.rx_lwe
            _log_exp(f"RX expedited ACK rx_lwe={rx_lwe}")
            if self._waiting_ack and rx_lwe == self._tx_frame_seq:
                self._waiting_ack = False
                self._current_encoded = None
                self._current_dpdu = None
                self._tx_frame_seq = (self._tx_frame_seq + 1) % EXPEDITED_FSN_MOD
                # cpdu_id avança apenas quando todos os segmentos foram ACKed
                if not self._tx_segments:
                    self._cpdu_id = (self._cpdu_id + 1) % EXPEDITED_CPDU_ID_MOD
                    _log_exp(
                        f"C_PDU completo, próximo seq={self._tx_frame_seq} "
                        f"cpdu_id={self._cpdu_id}"
                    )
                else:
                    _log_exp(
                        f"Segmento ACKed, próximo seq={self._tx_frame_seq} "
                        f"restam={len(self._tx_segments)} segmentos"
                    )
