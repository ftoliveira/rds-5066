"""ARQ Engine (selective repeat) for STANAG 5066 Phase 3.

Type 0 (DATA_ONLY), 1 (ACK_ONLY), 2 (DATA_ACK), 4 (EXPEDITED_DATA_ONLY), 5 (EXPEDITED_ACK_ONLY)
and type 3 (RESETWIN_RESYNC) for FULL RESET. Annex C.4.2 / C.4.4.

Sequence numbers are 8-bit (0-255) per the standard. All internal tracking
uses modular arithmetic to handle wrap-around correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Callable, Optional
import time

from src.dpdu_frame import (
    build_ack_only,
    build_data_only,
    build_resetwin_resync,
    decode_dpdu,
    dpdu_calc_eot_field,
    dpdu_set_address,
    encode_dpdu,
)
from src.stypes import DPDU, DPDUType, Address

MAX_ARQ_WINDOW = 128  # Annex C.4.2: max outstanding D_PDUs
ARQ_WINDOW_SIZE = MAX_ARQ_WINDOW
MAX_DATA_BYTES = 1023
RETX_TIMEOUT_MS = 2000
MAX_RETRIES = 5
RESET_RETRANSMIT_MS = 3000
EOW_ARQ = 0

SEQ_MOD = 256  # 8-bit sequence space

# Annex C Table C-4: repetition counts for type 3 / type 6 D_PDUs.
# Key = data_rate_bps, value = (short_interleave, long_interleave).
RESET_REPETITIONS: dict[int, tuple[int, int]] = {
    75: (5, 9),      # (short_interleave, long_interleave) repetitions
    150: (3, 5),
    300: (3, 5),
    600: (3, 3),
    1200: (1, 3),
    2400: (1, 1),
    3200: (1, 1),    # Edition 3 — implementation-specific per Amendment 1
    3600: (1, 1),    # Edition 3 — implementation-specific per Amendment 1
    4800: (1, 1),
    6400: (1, 1),    # Edition 3 — implementation-specific per Amendment 1
    8000: (1, 1),    # Edition 3 — implementation-specific per Amendment 1
    9600: (1, 1),
}


def repetition_count_for_rate(data_rate_bps: int, long_interleave: bool = False) -> int:
    """Return the number of repetitions for type 3/6 D_PDUs per Table C-4."""
    entry = RESET_REPETITIONS.get(data_rate_bps, (1, 1))
    return entry[1] if long_interleave else entry[0]


def _seq_add(seq: int, offset: int) -> int:
    """Add offset to an 8-bit sequence number."""
    return (seq + offset) % SEQ_MOD


def _seq_dist(from_seq: int, to_seq: int) -> int:
    """Forward distance from from_seq to to_seq in 8-bit modular space.

    _seq_dist(0, 5) == 5
    _seq_dist(250, 5) == 11  (wraps around)
    _seq_dist(5, 5) == 0
    """
    return (to_seq - from_seq) % SEQ_MOD


def _seq_in_window(seq: int, lwe: int, count: int) -> bool:
    """True if seq is in [lwe, lwe+count) modulo 256."""
    if count <= 0:
        return False
    return _seq_dist(lwe, seq) < count


def _log_arq(msg: str) -> None:
    """Debug helper for ARQ traces."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [ARQ] {msg}")


def _flow_arq(msg: str) -> None:
    """Mesma linha de tempo, prefixo FLOW para grep."""
    from src.flow_log import flow_tx
    flow_tx("ARQ", msg)


class ArqTxState(Enum):
    IDLE = "IDLE"
    SEGMENT = "SEGMENT"
    SEND = "SEND"
    WAIT_ACK = "WAIT_ACK"
    RETRANSMIT = "RETRANSMIT"


class AckStatus(IntEnum):
    """PENDING=pronto para 1ª tx, SENT_WAIT_ACK=enviado aguardando ACK (STANAG C.4.2)."""
    PENDING = 0
    ACKED = 1
    NACKED = 2
    SENT_WAIT_ACK = 3


class RxFrameStatus(IntEnum):
    EMPTY = 0
    RECEIVED = 1
    ERROR = 2


@dataclass(slots=True)
class _TxSlot:
    seq: int
    encoded: bytes
    status: int  # AckStatus
    tx_time_ms: int
    retx_count: int


@dataclass(slots=True)
class _RxSlot:
    seq: int = -1
    data: bytes = b""
    status: int = 0  # RxFrameStatus
    pdu_start: bool = False
    pdu_end: bool = False


def _make_address(destination: int, source: int) -> Address:
    return dpdu_set_address(destination=destination, source=source)


def _segment_cpdu(
    payload: bytes,
    local_node: int,
    remote_node: int,
    next_seq: int,
    deliver_in_order: bool,
    eot: int = 0,
    tx_uwe_seq: int | None = None,
    tx_lwe_seq: int | None = None,
) -> list[tuple[DPDU, bytes]]:
    """Segment C_PDU into DATA_ONLY DPDUs. Returns list of (DPDU, encoded_bytes).

    C.3.3 §11-12: as flags TX_UWE/TX_LWE são setadas exatamente no segmento
    cujo ``tx_frame_seq`` coincide com o TX_UWE/TX_LWE atual; demais segmentos
    transportam as flags zeradas. ``tx_uwe_seq``/``tx_lwe_seq`` informam quais
    são esses seqs (None desliga a flag para o batch).
    """
    if not payload:
        return []
    segments: list[tuple[DPDU, bytes]] = []
    addr = _make_address(destination=remote_node, source=local_node)
    offset = 0
    total = len(payload)
    seq = next_seq & 0xFF
    while offset < total:
        start = offset
        chunk = payload[offset : offset + MAX_DATA_BYTES]
        pdu_start = start == 0
        pdu_end = start + len(chunk) >= total
        offset += len(chunk)
        dpdu = build_data_only(
            EOW_ARQ,
            eot,
            addr,
            chunk,
            seq,
            pdu_start=pdu_start,
            pdu_end=pdu_end,
            deliver_in_order=deliver_in_order,
            tx_uwe=(tx_uwe_seq is not None and seq == tx_uwe_seq),
            tx_lwe=(tx_lwe_seq is not None and seq == tx_lwe_seq),
        )
        encoded = encode_dpdu(dpdu)
        _log_arq(
            f"TX segment local={local_node} remote={remote_node} "
            f"seq={dpdu.data.tx_frame_seq} offset={start} len={len(chunk)} "
            f"pdu_start={dpdu.data.pdu_start} pdu_end={dpdu.data.pdu_end}"
        )
        segments.append((dpdu, encoded))
        seq = _seq_add(seq, 1)
    return segments


def _build_selective_ack_bitmap(
    rx_lwe: int,
    rx_window: list[_RxSlot],
    window_size: int,
) -> bytes:
    """Build selective ACK bitmap per Annex C.3.4§(13-14).

    Bitmap is truncated after the byte containing the RX UWE bit.
    Padding bits in the last byte are set to 0.
    """
    # Determine the RX UWE: highest offset with a received or pending frame
    rx_uwe_offset = 0
    for i in range(1, window_size + 1):
        seq = _seq_add(rx_lwe, i)
        idx = seq % window_size
        if idx < len(rx_window):
            slot = rx_window[idx]
            if slot.seq == seq and slot.status in (RxFrameStatus.RECEIVED, RxFrameStatus.ERROR):
                rx_uwe_offset = i

    if rx_uwe_offset == 0:
        return b""

    bitmap = bytearray()
    bit = 0
    current_byte = 0
    for i in range(1, rx_uwe_offset + 1):
        seq = _seq_add(rx_lwe, i)
        idx = seq % window_size
        if idx < len(rx_window):
            slot = rx_window[idx]
            if slot.seq == seq and slot.status == RxFrameStatus.RECEIVED:
                current_byte |= 1 << bit
        bit += 1
        if bit >= 8:
            bitmap.append(current_byte)
            current_byte = 0
            bit = 0
    if bit > 0:
        bitmap.append(current_byte)
    return bytes(bitmap)


def _parse_selective_ack(
    rx_lwe_peer: int,
    sel_acks: bytes,
    tx_lwe: int,
    tx_count: int,
    window_size: int,
) -> dict[int, bool]:
    """Parse selective ACK bitmap: returns dict seq -> acked (True/False).

    Uses 8-bit modular arithmetic for all sequence comparisons.
    """
    result: dict[int, bool] = {}
    for i, b in enumerate(sel_acks):
        for bit in range(8):
            seq = _seq_add(rx_lwe_peer, 1 + i * 8 + bit)
            if not _seq_in_window(seq, tx_lwe, tx_count):
                if _seq_dist(tx_lwe, seq) >= tx_count:
                    return result
                continue
            idx = seq % window_size
            result[seq] = bool(b & (1 << bit))
    return result


class ArqEngine:
    """Selective repeat ARQ engine. TX and RX windows, selective ACK, FULL RESET.

    All sequence numbers are 8-bit (0-255) with modular arithmetic per STANAG 5066.
    """

    def __init__(
        self,
        local_node_address: int,
        remote_node_address: int,
        *,
        window_size: int = ARQ_WINDOW_SIZE,
        retx_timeout_ms: int = RETX_TIMEOUT_MS,
        max_retries: int = MAX_RETRIES,
        reset_retransmit_ms: int = RESET_RETRANSMIT_MS,
        delivery_callback: Optional[Callable[[bytes], None]] = None,
        link_failed_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.local_node_address = local_node_address
        self.remote_node_address = remote_node_address
        self.window_size = min(window_size, MAX_ARQ_WINDOW)
        self.retx_timeout_ms = retx_timeout_ms
        self.max_retries = max_retries
        self.reset_retransmit_ms = reset_retransmit_ms
        self.delivery_callback = delivery_callback
        self.link_failed_callback = link_failed_callback
        self.data_rate_bps: int = 1200
        self.long_interleave: bool = False

        self._tx_state = ArqTxState.IDLE
        self._tx_queue: list[tuple[bytes, bool]] = []  # (payload, deliver_in_order)
        self._current_segments: list[tuple[DPDU, bytes]] = []
        self._tx_lwe: int = 0       # 8-bit: lower window edge
        self._tx_uwe: int = 255     # 8-bit: upper window edge (255 = nenhum frame enviado)
        self._tx_count: int = 0     # number of frames in current TX batch
        self._next_seq: int = 0     # 8-bit: next sequence to assign
        self._last_reported_lwe: int = 0  # último tx_lwe reportado em DATA
        self._tx_window: dict[int, _TxSlot] = {}
        self._pending_ack_dpdu: Optional[DPDU] = None
        self._pending_ack_encoded: Optional[bytes] = None
        self._ack_dirty: bool = False   # True = rx_lwe mudou, ACK deve ser (re)construído

        self._rx_lwe: int = 0       # 8-bit: RX lower window edge
        self._deliver_lwe: int = 0  # 8-bit: Next sequence to deliver
        self._rx_window: list[_RxSlot] = [_RxSlot() for _ in range(self.window_size)]
        self._delivered: list[bytes] = []

        self._reset_state: Optional[str] = None
        self._reset_frame_id = 0
        self._reset_sent_at_ms: int = 0
        self._reset_reps_remaining: int = 0
        self._reset_cached_frame: Optional[bytes] = None

    def _reset_windows(self) -> None:
        """Zero TX/RX windows but preserve pending tx_queue (Annex C.4.4).

        Used when the initiator receives RESET_ACK: per the standard, windows
        must be zeroed, but C_PDUs queued for transmission after the reset
        was initiated should be preserved.
        """
        self._tx_state = ArqTxState.IDLE
        self._current_segments.clear()
        self._tx_lwe = 0
        self._tx_uwe = 255
        self._tx_count = 0
        self._next_seq = 0
        self._last_reported_lwe = 0
        self._tx_window.clear()
        self._pending_ack_dpdu = None
        self._pending_ack_encoded = None
        self._ack_dirty = False
        self._reset_reps_remaining = 0
        self._reset_cached_frame: Optional[bytes] = None
        self._rx_lwe = 0
        self._deliver_lwe = 0
        for slot in self._rx_window:
            slot.data = b""
            slot.status = RxFrameStatus.EMPTY
            slot.pdu_start = False
            slot.pdu_end = False
        self._delivered.clear()
        self._reset_state = None

    def reset_full(self) -> None:
        """Zero TX/RX windows, clear all queues and return to IDLE (Annex C.4.4)."""
        self._tx_queue.clear()
        self._reset_windows()

    def submit_cpdu(self, payload: bytes, deliver_in_order: bool = True) -> None:
        """Enqueue C_PDU for ARQ transmission."""
        if not payload:
            return
        if self._reset_state == "pending":
            _log_arq(
                f"submit_cpdu deferred (RESET pending): len={len(payload)}"
            )
        _log_arq(f"submit_cpdu len={len(payload)} deliver_in_order={deliver_in_order}")
        _flow_arq(f"C_PDU enfileirada no motor ARQ (segmentacao na maquina TX)")
        self._tx_queue.append((bytes(payload), deliver_in_order))

    def _iter_tx_window(self) -> list[int]:
        """Return list of 8-bit sequence numbers in current TX window [lwe, lwe+count)."""
        return [_seq_add(self._tx_lwe, i) for i in range(self._tx_count)]

    @property
    def reset_pending(self) -> bool:
        """True while a FULL RESET handshake is in progress (awaiting ACK)."""
        return self._reset_state == "pending"

    def has_pending_tx(self) -> bool:
        """True if there is a DPDU to send (data or ACK or RESET)."""
        if self._pending_ack_encoded:
            return True
        if self._reset_state == "pending":
            return True
        if self._tx_state in (ArqTxState.SEND, ArqTxState.RETRANSMIT):
            return True
        if self._tx_state == ArqTxState.WAIT_ACK:
            for seq in self._iter_tx_window():
                idx = seq % self.window_size
                slot = self._tx_window.get(idx)
                if slot and slot.status in (AckStatus.SENT_WAIT_ACK, AckStatus.NACKED):
                    return True
        for seq in self._iter_tx_window():
            idx = seq % self.window_size
            slot = self._tx_window.get(idx)
            if slot and slot.status in (AckStatus.PENDING, AckStatus.SENT_WAIT_ACK, AckStatus.NACKED):
                return True
        return bool(self._tx_queue) or bool(self._current_segments)

    def get_delivered_cpdus(self) -> list[bytes]:
        """Return and clear list of delivered C_PDU payloads."""
        out = list(self._delivered)
        self._delivered.clear()
        return out

    def process_tx(self, current_time_ms: int) -> list[bytes]:
        """Advance TX state machine; return list of encoded DPDUs to send (burst).

        Per Annex C.4.2, all pending frames should be sent in the same TX
        opportunity.  Returns an empty list when nothing to send.

        ACK acumulado: se _ack_dirty for True (um ou mais D-PDUs foram recebidos
        desde o último process_tx), constrói o ACK aqui com o rx_lwe final,
        garantindo que um único ACK cobre todos os frames do burst recebido.
        """
        if self._ack_dirty:
            self._build_ack()
            self._ack_dirty = False

        burst: list[bytes] = []
        single = self._process_tx_single(current_time_ms)
        while single is not None:
            burst.append(single)
            single = self._process_tx_single(current_time_ms)
        return burst

    def _process_tx_single(self, current_time_ms: int) -> Optional[bytes]:
        """Internal: return one encoded DPDU or None."""
        eot = dpdu_calc_eot_field(1)

        if self._pending_ack_encoded:
            enc = self._pending_ack_encoded
            self._pending_ack_dpdu = None
            self._pending_ack_encoded = None
            return enc

        if self._reset_state == "pending":
            # Repetições pendentes de um burst anterior
            if self._reset_reps_remaining > 0 and self._reset_cached_frame is not None:
                self._reset_reps_remaining -= 1
                return self._reset_cached_frame

            if current_time_ms - self._reset_sent_at_ms >= self.reset_retransmit_ms:
                addr = _make_address(
                    destination=self.remote_node_address,
                    source=self.local_node_address,
                )
                dpdu = build_resetwin_resync(
                    EOW_ARQ, eot, addr,
                    full_reset_cmd=True,
                    reset_ack=False,
                    reset_frame_id=self._reset_frame_id,
                )
                self._reset_sent_at_ms = current_time_ms
                reps = repetition_count_for_rate(self.data_rate_bps, self.long_interleave)
                enc = encode_dpdu(dpdu)
                if reps > 1:
                    self._reset_reps_remaining = reps - 1
                    self._reset_cached_frame = enc
                return enc
            return None

        if self._tx_state == ArqTxState.IDLE:
            if not self._tx_queue:
                return None
            self._tx_state = ArqTxState.SEGMENT

        if self._tx_state == ArqTxState.SEGMENT:
            payload, deliver_in_order = self._tx_queue[0]
            # C.3.3 §11-12: flag TX_LWE setada apenas no D_PDU cujo seq ==
            # TX_LWE; flag TX_UWE no D_PDU cujo seq == TX_UWE (último alocado).
            n_segments = (len(payload) + MAX_DATA_BYTES - 1) // MAX_DATA_BYTES
            new_uwe = (
                _seq_add(self._next_seq, n_segments - 1) if n_segments > 0 else None
            )
            self._current_segments = _segment_cpdu(
                payload,
                self.local_node_address,
                self.remote_node_address,
                self._next_seq,
                deliver_in_order,
                eot=eot,
                tx_uwe_seq=new_uwe,
                tx_lwe_seq=self._tx_lwe,
            )
            self._last_reported_lwe = self._tx_lwe
            for _dpdu, enc in self._current_segments:
                seq = _dpdu.data.tx_frame_seq if _dpdu.data else 0
                idx = seq % self.window_size
                self._tx_window[idx] = _TxSlot(
                    seq=seq,
                    encoded=enc,
                    status=AckStatus.PENDING,
                    tx_time_ms=current_time_ms,
                    retx_count=0,
                )
            self._tx_count = len(self._current_segments)
            # Atualizar TX UWE: último seq atribuído
            if self._tx_count > 0:
                self._tx_uwe = _seq_add(self._next_seq, self._tx_count - 1)
            _log_arq(
                f"TX window populated: frames {self._tx_count} "
                f"seq=[{self._next_seq}..{_seq_add(self._next_seq, self._tx_count - 1)}]"
            )
            self._tx_state = ArqTxState.SEND

        if self._tx_state == ArqTxState.SEND:
            # SEND: apenas frames PENDING (1ª transmissão). Após enviar -> SENT_WAIT_ACK.
            for seq in self._iter_tx_window():
                idx = seq % self.window_size
                slot = self._tx_window.get(idx)
                if slot and slot.status == AckStatus.PENDING:
                    slot.status = AckStatus.SENT_WAIT_ACK
                    slot.tx_time_ms = current_time_ms
                    _log_arq(
                        f"TX send frame seq={slot.seq} (1ª tx) retx_count={slot.retx_count}"
                    )
                    return slot.encoded
            self._tx_state = ArqTxState.WAIT_ACK
            return None

        if self._tx_state == ArqTxState.RETRANSMIT:
            # RETRANSMIT: apenas NACKED (timeout ou bitmap). Após enviar -> SENT_WAIT_ACK.
            for seq in self._iter_tx_window():
                idx = seq % self.window_size
                slot = self._tx_window.get(idx)
                if slot and slot.status == AckStatus.NACKED:
                    slot.retx_count += 1
                    if slot.retx_count > self.max_retries:
                        if self.link_failed_callback:
                            self.link_failed_callback()
                        self.reset_full()
                        return None
                    slot.status = AckStatus.SENT_WAIT_ACK
                    slot.tx_time_ms = current_time_ms
                    _log_arq(
                        f"TX retransmit frame seq={slot.seq} retx_count={slot.retx_count}"
                    )
                    return slot.encoded
            self._tx_state = ArqTxState.WAIT_ACK
            return None

        if self._tx_state == ArqTxState.WAIT_ACK:
            has_sent_wait = False
            has_nacked = False
            for seq in self._iter_tx_window():
                idx = seq % self.window_size
                slot = self._tx_window.get(idx)
                if slot:
                    if slot.status == AckStatus.SENT_WAIT_ACK:
                        has_sent_wait = True
                        if current_time_ms - slot.tx_time_ms >= self.retx_timeout_ms:
                            slot.status = AckStatus.NACKED
                            has_nacked = True
                    elif slot.status == AckStatus.NACKED:
                        has_nacked = True
                        
            if has_nacked and not has_sent_wait:
                self._tx_state = ArqTxState.RETRANSMIT
                return self._process_tx_single(current_time_ms)
                
            # Verificar se todos os frames do batch atual foram acked.
            # _tx_count pode ter chegado a 0 pelo avanço do rx_lwe no ACK,
            # ou todos os slots individuais podem estar ACKED.
            all_acked = self._tx_count == 0 or all(
                self._tx_window.get(seq % self.window_size) and
                self._tx_window[seq % self.window_size].status == AckStatus.ACKED
                for seq in self._iter_tx_window()
            )
            if all_acked:
                # Limpa slots acked da janela
                for seq in self._iter_tx_window():
                    self._tx_window.pop(seq % self.window_size, None)
                self._tx_lwe = _seq_add(self._tx_lwe, self._tx_count)
                self._next_seq = self._tx_lwe
                self._tx_count = 0
                self._tx_queue.pop(0)
                self._current_segments.clear()
                self._tx_state = ArqTxState.IDLE
                return self._process_tx_single(current_time_ms)

        return None

    def _deliver_rx_cpdu(self, start_seq: int, end_seq: int) -> None:
        """Reassemble and deliver C_PDU from RX window [start_seq, end_seq] (8-bit modular)."""
        count = _seq_dist(start_seq, end_seq) + 1
        parts: list[bytes] = []
        for i in range(count):
            seq = _seq_add(start_seq, i)
            idx = seq % self.window_size
            if idx >= len(self._rx_window):
                return
            slot = self._rx_window[idx]
            if slot.seq != seq or slot.status != RxFrameStatus.RECEIVED:
                return
            parts.append(slot.data)
        payload = b"".join(parts)
        _log_arq(
            f"RX deliver C_PDU seq=[{start_seq}..{end_seq}] len={len(payload)}"
        )
        from src.flow_log import flow_rx
        flow_rx("ARQ", f"C_PDU remontada len={len(payload)} -> callback/CAS (DATA)")
        self._delivered.append(payload)
        if self.delivery_callback:
            self.delivery_callback(payload)
        for i in range(count):
            seq = _seq_add(start_seq, i)
            idx = seq % self.window_size
            self._rx_window[idx].status = RxFrameStatus.EMPTY
            self._rx_window[idx].data = b""
            self._rx_window[idx].seq = -1
        self._deliver_lwe = _seq_add(end_seq, 1)

    def _try_deliver(self) -> None:
        """If we have a complete C_PDU ending at some seq, deliver it."""
        while True:
            count = _seq_dist(self._deliver_lwe, self._rx_lwe)
            if count == 0:
                break
            delivered_any = False
            for i in range(count):
                seq = _seq_add(self._deliver_lwe, i)
                idx = seq % self.window_size
                if idx >= len(self._rx_window):
                    continue
                slot = self._rx_window[idx]
                if slot.seq != seq or slot.status != RxFrameStatus.RECEIVED or not slot.pdu_end:
                    continue
                # Encontrou pdu_end em seq; procurar pdu_start para trás
                start_seq = seq
                valid_cpdu = False
                for _ in range(self.window_size):
                    sidx = start_seq % self.window_size
                    if sidx >= len(self._rx_window):
                        break
                    s_slot = self._rx_window[sidx]
                    if s_slot.seq == start_seq and s_slot.status == RxFrameStatus.RECEIVED:
                        if s_slot.pdu_start:
                            valid_cpdu = True
                            break
                        start_seq = _seq_add(start_seq, SEQ_MOD - 1)  # start_seq - 1 mod 256
                    else:
                        break
                if valid_cpdu:
                    self._deliver_rx_cpdu(start_seq, seq)
                    delivered_any = True
                    break
            if not delivered_any:
                break

    def _build_ack(self, eot: int = 0) -> None:
        """Build ACK-ONLY DPDU with current rx_lwe and selective ACK bitmap."""
        bitmap = _build_selective_ack_bitmap(
            self._rx_lwe, self._rx_window, self.window_size
        )
        addr = _make_address(
            destination=self.remote_node_address,
            source=self.local_node_address,
        )
        dpdu = build_ack_only(EOW_ARQ, eot, addr, self._rx_lwe & 0xFF, bitmap)
        self._pending_ack_dpdu = dpdu
        self._pending_ack_encoded = encode_dpdu(dpdu)
        _log_arq(
            f"RX build ACK rx_lwe={self._rx_lwe} bitmap_len={len(bitmap)} "
            f"bytes={bitmap.hex()}"
        )

    def process_rx_dpdu(self, dpdu: DPDU) -> None:
        """Process received DATA or ACK DPDU."""
        if dpdu.dpdu_type in (DPDUType.DATA_ONLY, DPDUType.DATA_ACK, DPDUType.EXPEDITED_DATA_ONLY):
            if dpdu.data is None:
                return
            seq = dpdu.data.tx_frame_seq
            
            if not _seq_in_window(seq, self._rx_lwe, self.window_size):
                # Fora da janela: provavelmente repetido antigo ou ruido no seq
                # Constrói ACK usando o LWE atual para que o peer atualize o estado dele.
                _log_arq(f"RX frame seq={seq} FORA DA JANELA rx_lwe={self._rx_lwe}")
                self._ack_dirty = True
                return

            idx = seq % self.window_size
            if idx >= len(self._rx_window):
                return
            # C.3.4 §7: D_PDUs com DROP_PDU = 1 devem ser ACKed positivamente
            # mesmo quando o CRC do payload está corrompido — o emissor já
            # decidiu descartar o segmento, portanto não há razão para o
            # receptor pedir retransmissão.
            drop_pdu_flag = bool(dpdu.data and dpdu.data.drop_pdu)
            if dpdu.data_crc_ok is False and not drop_pdu_flag:
                _log_arq(f"RX frame seq={seq} CRC ERROR, marcando como erro")
                self._rx_window[idx].seq = seq
                self._rx_window[idx].status = RxFrameStatus.ERROR
                self._ack_dirty = True
                return
            self._rx_window[idx].seq = seq
            # Quando DROP_PDU=1 e o CRC falhou, o payload está suspeito e não
            # deve ser entregue ao reassembler — armazenamos vazio.
            if dpdu.data_crc_ok is False and drop_pdu_flag:
                self._rx_window[idx].data = b""
                _log_arq(
                    f"RX frame seq={seq} DROP_PDU=1 c/ CRC erro: ACK positivo,"
                    f" payload descartado (C.3.4 §7)"
                )
            else:
                self._rx_window[idx].data = bytes(dpdu.user_data)
            self._rx_window[idx].status = RxFrameStatus.RECEIVED
            self._rx_window[idx].pdu_start = dpdu.data.pdu_start
            self._rx_window[idx].pdu_end = dpdu.data.pdu_end
            _log_arq(
                f"RX frame seq={seq} len={len(dpdu.user_data)} "
                f"pdu_start={dpdu.data.pdu_start} pdu_end={dpdu.data.pdu_end}"
            )

            # Advance _rx_lwe through consecutive received frames
            while True:
                lwe_idx = self._rx_lwe % self.window_size
                slot = self._rx_window[lwe_idx]
                if slot.seq == self._rx_lwe and slot.status == RxFrameStatus.RECEIVED:
                    self._rx_lwe = _seq_add(self._rx_lwe, 1)
                else:
                    break

            self._try_deliver()
            self._ack_dirty = True   # ACK será construído em process_tx() com rx_lwe final
            return

        if dpdu.dpdu_type in (DPDUType.ACK_ONLY, DPDUType.EXPEDITED_ACK_ONLY):
            if dpdu.ack is None:
                return
            rx_lwe_peer = dpdu.ack.rx_lwe
            sel_acks = dpdu.ack.sel_acks or b""
            _log_arq(
                f"RX ACK rx_lwe_peer={rx_lwe_peer} sel_acks_len={len(sel_acks)} "
                f"bytes={sel_acks.hex() if sel_acks else ''}"
            )
            # Processar selective ACK bitmap
            acked = _parse_selective_ack(
                rx_lwe_peer, sel_acks,
                self._tx_lwe, self._tx_count, self.window_size,
            )
            for seq, is_acked in acked.items():
                if not _seq_in_window(seq, self._tx_lwe, self._tx_count):
                    continue
                idx = seq % self.window_size
                slot = self._tx_window.get(idx)
                if slot and slot.seq == seq:
                    if is_acked:
                        slot.status = AckStatus.ACKED
                    elif slot.status != AckStatus.PENDING:
                        slot.status = AckStatus.NACKED

            # Avançar tx_lwe com base no rx_lwe do peer.
            # rx_lwe_peer é o LWE do receptor = próximo seq esperado.
            # Frames com seq < rx_lwe_peer já foram entregues em ordem.
            advance = _seq_dist(self._tx_lwe, rx_lwe_peer)
            if advance > 0 and advance <= self._tx_count:
                for i in range(advance):
                    seq = _seq_add(self._tx_lwe, i)
                    idx = seq % self.window_size
                    slot = self._tx_window.get(idx)
                    if slot and slot.seq == seq:
                        slot.status = AckStatus.ACKED
                self._tx_lwe = _seq_add(self._tx_lwe, advance)
                self._tx_count -= advance
            return

    def process_rx_reset(self, dpdu: DPDU) -> Optional[bytes]:
        """Process type 3 RESET/WIN-RESYNC (Annex C.3.6, C.4.4).

        Handles all four flags:
        - FULL_RESET_CMND: full reset of TX+RX windows
        - RESET_TX_WIN_RQST: peer requests we reset our TX window
        - RESET_RX_WIN_CMND: reset RX window to new_rx_lwe
        - RESET_ACK: acknowledgement of a previous reset
        Returns encoded RESET_ACK if we are responder, else None.
        """
        if dpdu.dpdu_type is not DPDUType.RESETWIN_RESYNC or dpdu.reset is None:
            return None
        r = dpdu.reset
        eot = dpdu_calc_eot_field(1)
        addr = _make_address(
            destination=self.remote_node_address,
            source=self.local_node_address,
        )

        # GAP-09: Handle RESET_RX_WIN_CMND — advance RX LWE to new_rx_lwe
        if r.reset_rx_win_cmd and not r.full_reset_cmd:
            new_lwe = r.new_rx_lwe & 0xFF
            advance = _seq_dist(self._rx_lwe, new_lwe)
            if advance > 0 and advance <= self.window_size:
                for i in range(advance):
                    seq = _seq_add(self._rx_lwe, i)
                    idx = seq % self.window_size
                    self._rx_window[idx].status = RxFrameStatus.EMPTY
                    self._rx_window[idx].data = b""
                    self._rx_window[idx].seq = -1
                self._rx_lwe = new_lwe
                if _seq_dist(self._deliver_lwe, new_lwe) > 0:
                    self._deliver_lwe = new_lwe
            ack_dpdu = build_resetwin_resync(
                EOW_ARQ, eot, addr,
                full_reset_cmd=False,
                reset_ack=True,
                new_rx_lwe=new_lwe,
                reset_frame_id=r.reset_frame_id,
            )
            return encode_dpdu(ack_dpdu)

        # GAP-09: Handle RESET_TX_WIN_RQST — reset our TX window
        if r.reset_tx_win_req and not r.full_reset_cmd:
            self._tx_state = ArqTxState.IDLE
            self._current_segments.clear()
            for seq in self._iter_tx_window():
                self._tx_window.pop(seq % self.window_size, None)
            self._tx_lwe = 0
            self._tx_count = 0
            self._next_seq = 0
            ack_dpdu = build_resetwin_resync(
                EOW_ARQ, eot, addr,
                full_reset_cmd=False,
                reset_ack=True,
                reset_frame_id=r.reset_frame_id,
            )
            return encode_dpdu(ack_dpdu)

        if r.full_reset_cmd and not r.reset_ack:
            self.reset_full()
            ack_dpdu = build_resetwin_resync(
                EOW_ARQ, eot, addr,
                full_reset_cmd=False,
                reset_ack=True,
                reset_frame_id=r.reset_frame_id,
            )
            return encode_dpdu(ack_dpdu)
        if r.reset_ack:
            # Initiator recebeu ACK: validate frame_id before clearing
            if self._reset_state == "pending" and r.reset_frame_id == self._reset_frame_id:
                _log_arq(
                    f"RESET_ACK recebido com frame_id={r.reset_frame_id} (esperado {self._reset_frame_id}) -> zerar janelas"
                )
                self._reset_windows()
            elif self._reset_state == "pending":
                _log_arq(
                    f"RESET_ACK recebido com frame_id={r.reset_frame_id} != esperado {self._reset_frame_id} -> ignorado"
                )
        return None

    def start_full_reset(self, current_time_ms: int) -> Optional[bytes]:
        """Initiator: start FULL RESET; returns first type 3 DPDU to send."""
        self._reset_frame_id = (self._reset_frame_id + 1) & 0xFF
        self._reset_state = "pending"
        self._reset_sent_at_ms = current_time_ms
        eot = dpdu_calc_eot_field(1)
        addr = _make_address(
            destination=self.remote_node_address,
            source=self.local_node_address,
        )
        dpdu = build_resetwin_resync(
            EOW_ARQ, eot, addr,
            full_reset_cmd=True,
            reset_ack=False,
            reset_frame_id=self._reset_frame_id,
        )
        return encode_dpdu(dpdu)
