from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# Default EOW type used by Non-ARQ segments when none is specified by the caller.
# Per Annex C.3.10, Non-ARQ D_PDUs may carry any EOW type.
EOW_TYPE_NON_ARQ = 3


@dataclass
class NonArqSegment:
    """Segmento de uma C_PDU transportado em um D_PDU não-ARQ (tipos 7/8)."""

    cpdu_id: int
    first_byte_position: int
    cpdu_size: int
    eow_type: int
    payload: bytes


@dataclass
class _ReassemblyEntry:
    cpdu_size: int
    buffer: bytearray
    received_ranges: List[Tuple[int, int]]
    created_at: float


class NonArqReassembler:
    """Reassembly de C_PDUs a partir de segmentos não-ARQ.

    A janela de recepção é controlada por `window_seconds`, expurgando C_PDUs
    parciais após o timeout, conforme o `cpdu_reception_window`.
    """

    def __init__(self, window_seconds: float = 30.0) -> None:
        self._window_seconds = float(window_seconds)
        self._entries: Dict[int, _ReassemblyEntry] = {}

    def _get_entry(self, cpdu_id: int, cpdu_size: int, now: float) -> _ReassemblyEntry:
        entry = self._entries.get(cpdu_id)
        if entry is None:
            entry = _ReassemblyEntry(
                cpdu_size=cpdu_size,
                buffer=bytearray(cpdu_size),
                received_ranges=[],
                created_at=now,
            )
            self._entries[cpdu_id] = entry
        return entry

    @staticmethod
    def _merge_ranges(
        ranges: List[Tuple[int, int]],
        new_range: Tuple[int, int],
    ) -> List[Tuple[int, int]]:
        """Mantém ranges de bytes recebidos normalizados e mesclados."""
        if not ranges:
            return [new_range]

        start, end = new_range
        merged: List[Tuple[int, int]] = []
        inserted = False

        for s, e in ranges:
            if e < start:
                merged.append((s, e))
            elif end < s:
                if not inserted:
                    merged.append((start, end))
                    inserted = True
                merged.append((s, e))
            else:
                start = min(start, s)
                end = max(end, e)

        if not inserted:
            merged.append((start, end))

        merged.sort()
        return merged

    def accept_segment(
        self,
        segment: NonArqSegment,
        now: Optional[float] = None,
    ) -> Optional[bytes]:
        """Processa um segmento recebido.

        Retorna a C_PDU completa quando todos os bytes foram recebidos,
        caso contrário `None`. Segmentos com `eow_type` diferente de 3
        são aceitos, mas uma implementação conforme Annex C deve sempre
        gerar segmentos com `EOW_TYPE_NON_ARQ`.
        """
        if now is None:
            now = time.time()

        entry = self._get_entry(segment.cpdu_id, segment.cpdu_size, now)

        start = segment.first_byte_position
        end = start + len(segment.payload)
        if start < 0 or end > entry.cpdu_size:
            return None

        entry.buffer[start:end] = segment.payload
        entry.received_ranges = self._merge_ranges(entry.received_ranges, (start, end))

        if len(entry.received_ranges) == 1:
            r_start, r_end = entry.received_ranges[0]
            if r_start == 0 and r_end >= entry.cpdu_size:
                data = bytes(entry.buffer[: entry.cpdu_size])
                del self._entries[segment.cpdu_id]
                return data

        return None

    def purge_expired(self, now: Optional[float] = None) -> None:
        """Remove C_PDUs parciais que excederam a janela de recepção."""
        if now is None:
            now = time.time()

        to_delete = [
            cpdu_id
            for cpdu_id, entry in self._entries.items()
            if now - entry.created_at > self._window_seconds
        ]
        for cpdu_id in to_delete:
            del self._entries[cpdu_id]


class NonArqSegmenter:
    """Segmentação simples de C_PDUs em segmentos não-ARQ."""

    def __init__(self, max_payload: int) -> None:
        if max_payload <= 0:
            raise ValueError("max_payload must be > 0")
        self._max_payload = int(max_payload)
        self._next_cpdu_id = 0

    def next_cpdu_id(self) -> int:
        cpdu_id = self._next_cpdu_id
        self._next_cpdu_id = (self._next_cpdu_id + 1) & 0x0FFF
        return cpdu_id

    def build_segments(self, cpdu: bytes, cpdu_id: Optional[int] = None) -> List[NonArqSegment]:
        """Cria uma lista de segmentos para a C_PDU fornecida."""
        if cpdu_id is None:
            cpdu_id = self.next_cpdu_id()

        size = len(cpdu)
        segments: List[NonArqSegment] = []
        offset = 0

        while offset < size:
            chunk = cpdu[offset : offset + self._max_payload]
            segments.append(
                NonArqSegment(
                    cpdu_id=cpdu_id,
                    first_byte_position=offset,
                    cpdu_size=size,
                    eow_type=0,
                    payload=chunk,
                )
            )
            offset += len(chunk)

        return segments


class NonArqEndpoint:
    """Endpoint lógico Non-ARQ, com fila TX e reassembly RX.

    Este endpoint é independente do formato de D_PDU. A camada abaixo
    deve mapear `NonArqSegment` para os campos de cabeçalho do tipo 7/8
    em `dpdu_frame.py` (cpdu_id, first_byte_position, cpdu_size, EOW=3).
    """

    def __init__(
        self,
        max_payload: int,
        cpdu_window_seconds: float = 30.0,
    ) -> None:
        self._segmenter = NonArqSegmenter(max_payload)
        self._reassembler = NonArqReassembler(cpdu_window_seconds)
        self._tx_queue: List[NonArqSegment] = []

    def enqueue_cpdu(self, cpdu: bytes, cpdu_id: Optional[int] = None) -> int:
        """Enfileira uma C_PDU para transmissão, retornando o cpdu_id."""
        segments = self._segmenter.build_segments(cpdu, cpdu_id=cpdu_id)
        if not segments:
            cpdu_id = self._segmenter.next_cpdu_id()
        else:
            cpdu_id = segments[0].cpdu_id
            self._tx_queue.extend(segments)
        return cpdu_id

    def has_pending_segment(self) -> bool:
        return bool(self._tx_queue)

    def pop_next_segment(self) -> Optional[NonArqSegment]:
        if not self._tx_queue:
            return None
        return self._tx_queue.pop(0)

    def process_rx_segment(
        self,
        segment: NonArqSegment,
        now: Optional[float] = None,
    ) -> Optional[bytes]:
        """Entrega um segmento recebido ao reassembly.

        Retorna a C_PDU completa quando disponível, ou `None` se ainda
        estiver incompleta.
        """
        return self._reassembler.accept_segment(segment, now=now)

    def tick(self, now: Optional[float] = None) -> None:
        """Avança a lógica de tempo, expurgando C_PDUs expiradas."""
        self._reassembler.purge_expired(now=now)

"""Phase 2 non-ARQ engine for STANAG 5066."""

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from src.dpdu_frame import (
    build_expedited_non_arq,
    build_non_arq,
    decode_dpdu,
    dpdu_calc_eot_field,
    encode_dpdu,
)
from src.modem_if import ModemInterface
from src.flow_log import flow_tx, payload_hint
from src.stypes import (
    DPDU, DPDUType, NonArqDelivery, NonArqDeliveryKind, NonArqDeliveryMode,
)


DEFAULT_NON_ARQ_SEGMENT_BYTES = 200
DEFAULT_CPDU_RECEPTION_WINDOW_HS = 4


@dataclass(slots=True)
class _TxRequest:
    dpdu_type: DPDUType
    destination: int
    payload: bytes
    cpdu_id: int
    cpdu_reception_window_hs: int
    group_address: bool = False


@dataclass(slots=True)
class _RxAssembly:
    dpdu_type: DPDUType
    source: int
    destination: int
    cpdu_id: int
    cpdu_size: int
    expires_at_ms: int
    buffer: bytearray
    received: list[bool]
    received_count: int = 0


class NonArqEngine:
    """Type 7/8 sender/receiver with simple reassembly on a simulated modem."""

    def __init__(
        self,
        local_node_address: int,
        modem: ModemInterface,
        *,
        max_user_data_bytes: int = DEFAULT_NON_ARQ_SEGMENT_BYTES,
        default_cpdu_reception_window_hs: int = DEFAULT_CPDU_RECEPTION_WINDOW_HS,
        half_duplex: bool = True,
        delivery_handler: Callable[[NonArqDelivery], None] | None = None,
        delivery_mode: NonArqDeliveryMode = NonArqDeliveryMode.DELIVER_W_ERRORS,
    ) -> None:
        self.local_node_address = local_node_address
        self.modem = modem
        self.max_user_data_bytes = max_user_data_bytes
        self.default_cpdu_reception_window_hs = default_cpdu_reception_window_hs
        self.half_duplex = half_duplex
        self.delivery_handler = delivery_handler
        # C.3.13 §10-11: ERROR_FREE descarta fragmentos parciais expirados;
        # DELIVER_W_ERRORS entrega-os marcados com complete=False.
        self.delivery_mode = delivery_mode

        self._tx_queue_expedited: deque[_TxRequest] = deque()
        self._tx_queue_normal: deque[_TxRequest] = deque()
        self._active_segments: deque[bytes] = deque()
        self._next_cpdu_id_type7 = 0
        self._next_cpdu_id_type8 = 0
        self._rx_assemblies: dict[tuple[int, int], _RxAssembly] = {}

        self.deliveries: list[NonArqDelivery] = []
        self.sent_dpdus: list[DPDU] = []
        self.remote_tx_until_ms = 0
        self.local_tx_until_ms = 0
        self.deferred_tx_count = 0
        self.collision_count = 0

    def set_delivery_handler(self, handler: Callable[[NonArqDelivery], None] | None) -> None:
        self.delivery_handler = handler

    def queue_cpdu(
        self,
        dpdu_type: DPDUType,
        destination: int,
        payload: bytes,
        *,
        cpdu_id: int | None = None,
        cpdu_reception_window_hs: int | None = None,
        group_address: bool = False,
    ) -> int:
        if dpdu_type not in {DPDUType.NON_ARQ, DPDUType.EXPEDITED_NON_ARQ}:
            raise ValueError("Non-ARQ engine only queues type 7 or 8 D_PDUs")

        if cpdu_id is not None:
            assigned_cpdu_id = cpdu_id & 0x0FFF
        elif int(dpdu_type) == int(DPDUType.EXPEDITED_NON_ARQ):
            assigned_cpdu_id = self._next_cpdu_id_type8
            self._next_cpdu_id_type8 = (self._next_cpdu_id_type8 + 1) & 0x0FFF
        else:
            assigned_cpdu_id = self._next_cpdu_id_type7
            self._next_cpdu_id_type7 = (self._next_cpdu_id_type7 + 1) & 0x0FFF

        hint = payload_hint(payload)
        flow_tx(
            "NonARQ",
            f"node={self.local_node_address} QUEUE tipo={dpdu_type.name} dest={destination} "
            f"cpdu_id={assigned_cpdu_id} payload_len={len(payload)} {hint} (segmentacao na proxima TX)",
        )
        req = _TxRequest(
            dpdu_type=dpdu_type,
            destination=destination,
            payload=payload,
            cpdu_id=assigned_cpdu_id,
            cpdu_reception_window_hs=(
                self.default_cpdu_reception_window_hs
                if cpdu_reception_window_hs is None
                else cpdu_reception_window_hs
            ),
            group_address=group_address,
        )
        if int(dpdu_type) == int(DPDUType.EXPEDITED_NON_ARQ):
            self._tx_queue_expedited.append(req)
        else:
            self._tx_queue_normal.append(req)
        return assigned_cpdu_id

    def _pop_next_request(self) -> _TxRequest | None:
        """Retorna próxima requisição: expedited tem prioridade sobre normal."""
        if self._tx_queue_expedited:
            return self._tx_queue_expedited.popleft()
        if self._tx_queue_normal:
            return self._tx_queue_normal.popleft()
        return None

    def process_tx(self, current_time_ms: int) -> list[DPDU]:
        sent_now: list[DPDU] = []
        has_pending = self._tx_queue_expedited or self._tx_queue_normal
        if current_time_ms < self.remote_tx_until_ms:
            if self._active_segments or has_pending:
                self.deferred_tx_count += 1
            return sent_now

        if not self._active_segments and has_pending:
            request = self._pop_next_request()
            if request is None:
                return sent_now
            segs = self._build_segments(request)
            self._active_segments.extend(segs)
            flow_tx(
                "NonARQ",
                f"node={self.local_node_address} SEGMENTOU cpdu_id={request.cpdu_id} dest={request.destination} "
                f"n_segmentos={len(segs)} burst_mode EOT time-based",
            )

        if self._active_segments:
            # Modo burst (Annex C.3): todos os D_PDUs do CPDU em uma única transmissão
            burst_frames = list(self._active_segments)
            self._active_segments.clear()

            self.modem.modem_tx_burst(burst_frames)

            # local_tx_until_ms: EOT do primeiro D_PDU cobre todo o burst
            first_decoded = decode_dpdu(burst_frames[0])
            if first_decoded.eot:
                self.local_tx_until_ms = current_time_ms + first_decoded.eot * 500

            for raw in burst_frames:
                decoded = decode_dpdu(raw)
                self.sent_dpdus.append(decoded)
                sent_now.append(decoded)
        return sent_now

    def process_rx(self, current_time_ms: int) -> list[NonArqDelivery]:
        deliveries_now: list[NonArqDelivery] = []
        while True:
            frame = self.modem.modem_rx_read_frame()
            if frame is None:
                break
            dpdu = decode_dpdu(frame)
            if dpdu.eot:
                self.remote_tx_until_ms = max(self.remote_tx_until_ms, current_time_ms + dpdu.eot * 500)

            if dpdu.dpdu_type in {DPDUType.NON_ARQ, DPDUType.EXPEDITED_NON_ARQ}:
                if dpdu.address.destination != self.local_node_address:
                    continue
                deliveries_now.extend(self._process_non_arq_dpdu(dpdu, current_time_ms))

        deliveries_now.extend(self._expire_partial_reassemblies(current_time_ms))
        return deliveries_now

    def tick(self, current_time_ms: int) -> tuple[list[NonArqDelivery], list[DPDU]]:
        deliveries = self.process_rx(current_time_ms)
        sent = self.process_tx(current_time_ms)
        return deliveries, sent

    # Overhead fixo por D_PDU: sync (2 B) + header (~36 B) + CRC header (2 B) ≈ 40 B
    # Ref: STANAG 5066 Annex C.3.1.3, Annex H (tamanho recomendado ~200 B dados)
    _DPDU_OVERHEAD_BYTES: int = 40

    def _build_segments(self, request: _TxRequest) -> list[bytes]:
        """Constrói D_PDUs com EOT monotonicamente decrescente (modo burst, Annex C.3).

        Cada EOT reflete o tempo restante no 'transmission interval' a partir do
        início daquele D_PDU, em meios-segundos (Annex C.3.1.3).
        """
        segments: list[bytes] = []
        total = len(request.payload)
        segment_count = max(1, (total + self.max_user_data_bytes - 1) // self.max_user_data_bytes)

        data_rate_bps = self.modem.config.data_rate_bps

        # Pré-calcular tempo de transmissão de cada D_PDU (em ms)
        chunk_sizes: list[int] = []
        dpdu_times_ms: list[int] = []
        for index in range(segment_count):
            start = index * self.max_user_data_bytes
            end = min(start + self.max_user_data_bytes, total)
            data_len = end - start
            chunk_sizes.append(data_len)
            frame_bytes = self._DPDU_OVERHEAD_BYTES + data_len
            dpdu_times_ms.append(frame_bytes * 8 * 1000 // data_rate_bps)

        total_burst_ms = sum(dpdu_times_ms)

        for index in range(segment_count):
            start = index * self.max_user_data_bytes
            chunk = request.payload[start : start + chunk_sizes[index]]

            # EOT = tempo restante no burst a partir do início deste D_PDU (meios-s)
            elapsed_ms = sum(dpdu_times_ms[:index])
            remaining_ms = total_burst_ms - elapsed_ms
            eot = dpdu_calc_eot_field((remaining_ms + 499) // 500) if self.half_duplex else 0

            kwargs = dict(
                eow=0x000,
                eot=eot,
                address=self._make_address(request.destination),
                data=chunk,
                cpdu_reception_window=request.cpdu_reception_window_hs,
                first_byte_position=start,
                cpdu_size=total,
                group_address=request.group_address,
                cpdu_id=request.cpdu_id,
            )
            if request.dpdu_type is DPDUType.EXPEDITED_NON_ARQ:
                dpdu = build_expedited_non_arq(**kwargs)
            else:
                dpdu = build_non_arq(**kwargs)
            segments.append(encode_dpdu(dpdu))
        return segments

    def _make_address(self, destination: int):
        from src.dpdu_frame import dpdu_set_address

        return dpdu_set_address(destination=destination, source=self.local_node_address)

    def _process_non_arq_dpdu(self, dpdu: DPDU, current_time_ms: int) -> list[NonArqDelivery]:
        header = dpdu.non_arq
        assert header is not None

        key = (dpdu.address.source, header.cpdu_id)
        assembly = self._rx_assemblies.get(key)
        if assembly is None:
            assembly = _RxAssembly(
                dpdu_type=dpdu.dpdu_type,
                source=dpdu.address.source,
                destination=dpdu.address.destination,
                cpdu_id=header.cpdu_id,
                cpdu_size=header.cpdu_size,
                expires_at_ms=current_time_ms + header.cpdu_reception_window * 500,
                buffer=bytearray(header.cpdu_size),
                received=[False] * header.cpdu_size,
            )
            self._rx_assemblies[key] = assembly
        else:
            assembly.expires_at_ms = max(
                assembly.expires_at_ms,
                current_time_ms + header.cpdu_reception_window * 500,
            )

        start = header.first_byte_position
        end = min(start + len(dpdu.user_data), assembly.cpdu_size)
        chunk = dpdu.user_data[: max(0, end - start)]
        for offset, value in enumerate(chunk):
            absolute = start + offset
            if absolute >= assembly.cpdu_size:
                break
            if not assembly.received[absolute]:
                assembly.received[absolute] = True
                assembly.received_count += 1
            assembly.buffer[absolute] = value

        if assembly.received_count >= assembly.cpdu_size:
            delivery = NonArqDelivery(
                dpdu_type=assembly.dpdu_type,
                source=assembly.source,
                destination=assembly.destination,
                cpdu_id=assembly.cpdu_id,
                payload=bytes(assembly.buffer),
                complete=True,
                error=False,
                kind=NonArqDeliveryKind.COMPLETE,
                cpdu_size=assembly.cpdu_size,
            )
            del self._rx_assemblies[key]
            self._emit_delivery(delivery)
            return [delivery]
        return []

    def _expire_partial_reassemblies(self, current_time_ms: int) -> list[NonArqDelivery]:
        expired: list[NonArqDelivery] = []
        expired_keys = [
            key for key, assembly in self._rx_assemblies.items() if current_time_ms >= assembly.expires_at_ms
        ]
        for key in expired_keys:
            assembly = self._rx_assemblies.pop(key)
            # C.3.13 §10-11: em modo ERROR_FREE descartamos silenciosamente as
            # remontagens incompletas. Em DELIVER_W_ERRORS entregamos o
            # fragmento parcial com ``complete=False``.
            if self.delivery_mode == NonArqDeliveryMode.ERROR_FREE:
                continue
            partial_payload = bytes(
                assembly.buffer[idx]
                for idx, present in enumerate(assembly.received)
                if present
            )
            delivery = NonArqDelivery(
                dpdu_type=assembly.dpdu_type,
                source=assembly.source,
                destination=assembly.destination,
                cpdu_id=assembly.cpdu_id,
                payload=partial_payload,
                complete=False,
                error=True,
                kind=NonArqDeliveryKind.PARTIAL,
                cpdu_size=assembly.cpdu_size,
            )
            self._emit_delivery(delivery)
            expired.append(delivery)
        return expired

    def _emit_delivery(self, delivery: NonArqDelivery) -> None:
        self.deliveries.append(delivery)
        if self.delivery_handler is not None:
            self.delivery_handler(delivery)
