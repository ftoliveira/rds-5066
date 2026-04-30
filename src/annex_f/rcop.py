"""
F.8 — RCOP (Reliable Connection-Oriented Protocol, SAP 6) +
F.9 — UDOP (Unreliable Datagram-Oriented Protocol, SAP 7).

Formato RCOP PDU (F.8.1, Figura F-9):
  Byte 0: CONNECTION_ID_NUMBER[7:4] | RESERVED[3:0]
  Byte 1: U_PDU_ID_NUMBER (8 bits)
  Bytes 2-3: U_PDU_SEGMENT_NUMBER (16 bits big-endian)
  Bytes 4-5: APPLICATION_IDENTIFIER (16 bits big-endian)
  Bytes 6+: APP_DATA[]

UDOP usa exatamente o mesmo formato (F.9.1), com SAP_ID=7 e modo non-ARQ.

Remontagem (F.8.3): identificador único = (src_addr, src_sap, conn_id, updu_id).
Último segmento detectado por len(app_data) < RCOP_MAX_APP_DATA.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Callable

from src.stypes import (
    DeliveryMode,
    SAP_ID_RCOP,
    SAP_ID_UDOP,
    SisUnidataIndication,
)

from .base_client import SubnetClient
from .updu import DEFAULT_MTU

logger = logging.getLogger(__name__)

# ─── APP_ID assignments (Table F-5) ───────────────────────────────────────────
APP_ID_BFTP = 0x1002        # Basic File Transfer Protocol
APP_ID_FRAP = 0x100B        # File-Receipt Acknowledgement Protocol
APP_ID_FRAPV2 = 0x100C      # File-Receipt Acknowledgement Protocol v2
APP_ID_TMMHS_TMI1 = 0x2000  # T-MMHS TMI-1 (LMTA-to-LMTA)
APP_ID_TMMHS_TMI2 = 0x2001  # T-MMHS TMI-2 (LMTA-to-LUA)
APP_ID_TMMHS_TMI3 = 0x2002  # T-MMHS TMI-3 (LMS-to-LUA)
APP_ID_TMMHS_TMI4 = 0x2003  # T-MMHS TMI-4 (LUA-to-LUA)
APP_ID_TMMHS_TMI5 = 0x2004  # T-MMHS TMI-5 (ACP-127 AU)

RCOP_HEADER_SIZE = 6                              # bytes
RCOP_MAX_APP_DATA = DEFAULT_MTU - RCOP_HEADER_SIZE  # 2042 bytes por segmento


@dataclass(slots=True)
class RcopPDU:
    """RCOP/UDOP Protocol Data Unit (F.8.1, Figura F-9)."""

    connection_id: int   # 0-15 (4 bits)
    updu_id: int         # 0-255 (8 bits, U_PDU_ID_NUMBER)
    segment_number: int  # 0-65535 (16 bits)
    app_id: int          # 0-65535 (APPLICATION_IDENTIFIER, 16 bits)
    app_data: bytes      # APP_DATA[] (comprimento variável)

    def __post_init__(self):
        if not (0 <= self.connection_id <= 15):
            raise ValueError(f"connection_id fora de 0-15: {self.connection_id}")
        if not (0 <= self.updu_id <= 255):
            raise ValueError(f"updu_id fora de 0-255: {self.updu_id}")
        if not (0 <= self.segment_number <= 65535):
            raise ValueError(f"segment_number fora de 0-65535: {self.segment_number}")
        if not (0 <= self.app_id <= 65535):
            raise ValueError(f"app_id fora de 0-65535: {self.app_id}")


def encode_rcop_pdu(pdu: RcopPDU) -> bytes:
    """Codifica RcopPDU em bytes (wire format, big-endian)."""
    byte0 = (pdu.connection_id & 0x0F) << 4  # bits reservados = 0
    header = struct.pack(">BBHH", byte0, pdu.updu_id, pdu.segment_number, pdu.app_id)
    return header + pdu.app_data


def decode_rcop_pdu(raw: bytes) -> RcopPDU:
    """Decodifica bytes em RcopPDU. Levanta ValueError se truncado."""
    if len(raw) < RCOP_HEADER_SIZE:
        raise ValueError(
            f"RCOP PDU truncado: {len(raw)} bytes (mínimo {RCOP_HEADER_SIZE})"
        )
    byte0, updu_id, segment_number, app_id = struct.unpack_from(">BBHH", raw)
    connection_id = (byte0 >> 4) & 0x0F
    app_data = raw[RCOP_HEADER_SIZE:]
    return RcopPDU(connection_id, updu_id, segment_number, app_id, app_data)


# Assinatura do handler de aplicação: (src_addr, src_sap, app_id, app_data)
AppHandler = Callable[[int, int, int, bytes], None]


class _RcopReassemblyContext:
    """Acumula segmentos RCOP/UDOP por (src_addr, src_sap, conn_id, updu_id).

    Detecta completude quando len(app_data) < RCOP_MAX_APP_DATA (último segmento).
    Verificação de completude: todos os segmentos 0..max_seg devem estar presentes.
    Conforme F.8.3, o identificador único é (src_addr, src_sap, conn_id, updu_id).
    """

    def __init__(self):
        # {(src_addr, src_sap, conn_id, updu_id): {seg_num: (app_id, app_data)}}
        self._buffers: dict[tuple, dict[int, tuple[int, bytes]]] = {}

    def feed(
        self, src_addr: int, src_sap: int, pdu: RcopPDU
    ) -> tuple[int, bytes] | None:
        """Alimenta um segmento. Retorna (app_id, dados_completos) ou None."""
        key = (src_addr, src_sap, pdu.connection_id, pdu.updu_id)

        # Segmento único (não segmentado): seg_num=0 e dados menores que MTU
        if pdu.segment_number == 0 and len(pdu.app_data) < RCOP_MAX_APP_DATA:
            self._buffers.pop(key, None)
            return (pdu.app_id, pdu.app_data)

        buf = self._buffers.setdefault(key, {})
        buf[pdu.segment_number] = (pdu.app_id, pdu.app_data)

        # Verifica completude: último segmento tem dados < RCOP_MAX_APP_DATA
        if len(pdu.app_data) >= RCOP_MAX_APP_DATA:
            return None  # não é o último

        max_seg = max(buf)
        if len(buf) != max_seg + 1:
            return None  # faltam segmentos intermediários

        # Remonta em ordem; app_id vem do primeiro segmento
        first_app_id = buf[0][0]
        reassembled = b"".join(buf[i][1] for i in range(max_seg + 1))
        del self._buffers[key]
        return (first_app_id, reassembled)

    def clear(self):
        self._buffers.clear()


class RcopClient(SubnetClient):
    """F.8 — RCOP Client, SAP 6 (ARQ, confiável, orientado a conexão).

    Envio:
        client.send(dest_addr, app_id, data)

    Recepção por app_id:
        client.register_app_handler(APP_ID_BFTP, my_callback)

    Recepção catch-all:
        client.on_received = my_callback
        # assinatura: (src_addr, src_sap, app_id, app_data)
    """

    SAP_ID = SAP_ID_RCOP  # 6

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self._rcop_updu_id: int = 0
        self._rcop_reassembly = _RcopReassemblyContext()
        self._handlers: dict[int, AppHandler] = {}
        self.on_received: AppHandler | None = None  # catch-all

    # ── Envio ─────────────────────────────────────────────────────────────────

    def _alloc_rcop_id(self) -> int:
        uid = self._rcop_updu_id
        self._rcop_updu_id = (self._rcop_updu_id + 1) & 0xFF
        return uid

    def _build_segments(
        self, conn_id: int, updu_id: int, app_id: int, data: bytes
    ) -> list[bytes]:
        """Segmenta app_data em PDUs RCOP."""
        if not data:
            return [encode_rcop_pdu(RcopPDU(conn_id, updu_id, 0, app_id, b""))]

        segs = []
        seg_num = 0
        for offset in range(0, len(data), RCOP_MAX_APP_DATA):
            chunk = data[offset : offset + RCOP_MAX_APP_DATA]
            segs.append(encode_rcop_pdu(RcopPDU(conn_id, updu_id, seg_num, app_id, chunk)))
            seg_num += 1
            if seg_num > 65535:
                raise ValueError("Dados requerem mais de 65535 segmentos RCOP")
        return segs

    def send(
        self,
        dest_addr: int,
        app_id: int,
        data: bytes,
        conn_id: int | None = None,
        priority: int = 5,
        ttl_seconds: float = 120.0,
    ) -> int:
        """Envia dados via RCOP (ARQ). Retorna updu_id alocado."""
        if conn_id is None:
            conn_id = self.connection_id
        updu_id = self._alloc_rcop_id()
        segments = self._build_segments(conn_id, updu_id, app_id, data)

        for seg_bytes in segments:
            self.node.unidata_request(
                sap_id=self.SAP_ID,
                dest_addr=dest_addr,
                dest_sap=self.SAP_ID,
                priority=priority,
                ttl_seconds=ttl_seconds,
                mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
                updu=seg_bytes,
            )

        logger.debug(
            "RCOP SAP=%d → addr=%d app_id=0x%04X updu_id=%d "
            "(%d seg(s), %d bytes)",
            self.SAP_ID, dest_addr, app_id, updu_id, len(segments), len(data),
        )
        return updu_id

    # ── Recepção ──────────────────────────────────────────────────────────────

    def register_app_handler(self, app_id: int, handler: AppHandler):
        """Registra callback para app_id específico."""
        self._handlers[app_id] = handler

    def on_unidata_indication(self, indication: SisUnidataIndication):
        """Override: parseia RCOP PDU diretamente (sem UPDU wrapper da classe base)."""
        try:
            pdu = decode_rcop_pdu(indication.updu)
        except ValueError as exc:
            logger.warning("RCOP PDU inválido de addr=%d: %s", indication.src_addr, exc)
            return

        result = self._rcop_reassembly.feed(
            indication.src_addr, indication.src_sap, pdu
        )
        if result is None:
            return  # aguardando segmentos restantes

        app_id, app_data = result
        logger.debug(
            "RCOP ← addr=%d app_id=0x%04X %d bytes",
            indication.src_addr, app_id, len(app_data),
        )
        handler = self._handlers.get(app_id) or self.on_received
        if handler:
            handler(indication.src_addr, indication.src_sap, app_id, app_data)
        else:
            logger.warning(
                "RCOP: nenhum handler para app_id=0x%04X de addr=%d",
                app_id, indication.src_addr,
            )


class UdopClient(RcopClient):
    """F.9 — UDOP Client, SAP 7 (non-ARQ, não confiável, orientado a datagramas).

    Usa o mesmo formato de PDU que RCOP (F.9.1).
    Modo padrão: non-ARQ, sem confirmação de entrega.
    Suporta endereços multicast (broadcast) da sub-rede.
    """

    SAP_ID = SAP_ID_UDOP  # 7

    def send(
        self,
        dest_addr: int,
        app_id: int,
        data: bytes,
        conn_id: int | None = None,
        priority: int = 5,
        ttl_seconds: float = 120.0,
    ) -> int:
        """Envia dados via UDOP (non-ARQ). Retorna updu_id alocado."""
        if conn_id is None:
            conn_id = self.connection_id
        updu_id = self._alloc_rcop_id()
        segments = self._build_segments(conn_id, updu_id, app_id, data)

        for seg_bytes in segments:
            self.node.unidata_request(
                sap_id=self.SAP_ID,
                dest_addr=dest_addr,
                dest_sap=self.SAP_ID,
                priority=priority,
                ttl_seconds=ttl_seconds,
                mode=DeliveryMode(arq_mode=False),
                updu=seg_bytes,
            )

        logger.debug(
            "UDOP SAP=%d → addr=%d app_id=0x%04X updu_id=%d "
            "(%d seg(s), %d bytes)",
            self.SAP_ID, dest_addr, app_id, updu_id, len(segments), len(data),
        )
        return updu_id
