"""SIS — Subnetwork Interface Sublayer (STANAG 5066 Annex A).

Gerencia SAPs (0-15), filas de prioridade, sessões de enlace (soft/hard),
TTL/TTD e roteamento de U_PDUs. Encapsula Phase3Node internamente.

Codificação S_PDU conforme Annex A Figuras A-3, A-4.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from src.stypes import (
    CPDU,
    CasLinkState,
    CPDUType,
    DeliveryMode,
    DPDUType,
    LinkType,
    ServiceType,
    SisBindRejectReason,
    SisDataDeliveryFailReason,
    SisHardLinkRejectReason,
    SisHardLinkTerminateReason,
    SisHardLinkType,
    SisLinkSessionState,
    SisRejectReason,
    SisUnidataIndication,
    SPDU,
    SPDU_TYPE_DATA,
    SPDU_TYPE_DATA_DELIVERY_CONFIRM,
    SPDU_TYPE_DATA_DELIVERY_FAIL,
    SPDU_TYPE_HARD_LINK_ESTABLISH_CONFIRM,
    SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED,
    SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST,
    SPDU_TYPE_HARD_LINK_TERMINATE,
    SPDU_TYPE_HARD_LINK_TERMINATE_CONFIRM,
    TxMode,
)

# ---------------------------------------------------------------------------
# S_PDU codec — Annex A Fig. A-4 (bit-map conforme CCITT V.42)
# ---------------------------------------------------------------------------


def _julian_day_mod16(t: float) -> int:
    """Dia Juliano (001-365) mod 16 para TTD (Annex A)."""
    dt = datetime.utcfromtimestamp(t)
    start = datetime(dt.year, 1, 1)
    day = (dt - start).days + 1
    return day % 16


def encode_spdu_data(spdu: SPDU) -> bytes:
    """Codifica DATA S_PDU (tipo 0) conforme Annex A Fig. A-4(a)."""
    priority = min(15, max(0, spdu.priority))
    src_sap = spdu.src_sap & 0x0F
    dest_sap = spdu.dest_sap & 0x0F
    delivery_confirm = 1 if (spdu.node_delivery_confirm_required or
                              spdu.client_delivery_confirm_required) else 0

    ttd_val = spdu.ttd if spdu.ttd else 0.0
    valid_ttd = 1 if ttd_val > 0 else 0
    julian_mod16 = _julian_day_mod16(ttd_val) if valid_ttd else 0
    # Annex A A.3.1.1: GMT = seconds since midnight / 2, mapped into 16 bits
    gmt_high16 = int((ttd_val % 86400) / 2) & 0xFFFF if valid_ttd else 0

    # Byte 0: TYPE (4 bits) = 0 | PRIORITY (4 bits)
    b0 = (SPDU_TYPE_DATA << 4) | priority
    # Byte 1: SOURCE SAP ID (4 bits) | DEST SAP ID (4 bits) — Annex A Fig A-4
    b1 = (src_sap << 4) | dest_sap
    # Byte 2: DLVRY_CONF(1) | VALID_TTD(1) | reserved(2) | Julian mod 16 (4 bits)
    b2 = (delivery_confirm << 7) | (valid_ttd << 6) | (julian_mod16 & 0x0F)

    out = bytearray([b0, b1, b2])
    if valid_ttd:
        out.extend(struct.pack("!H", gmt_high16))
    out.extend(spdu.updu)
    return bytes(out)


def decode_spdu_data(data: bytes) -> tuple[SPDU, int]:
    """Decodifica DATA S_PDU (tipo 0). Retorna (SPDU, bytes consumidos)."""
    if len(data) < 3:
        raise ValueError("DATA S_PDU muito curto")
    b0, b1, b2 = data[0], data[1], data[2]
    spdu_type = (b0 >> 4) & 0x0F
    if spdu_type != SPDU_TYPE_DATA:
        raise ValueError(f"Esperado tipo DATA (0), got {spdu_type}")

    priority = b0 & 0x0F
    src_sap = (b1 >> 4) & 0x0F
    dest_sap = b1 & 0x0F
    client_confirm = (b2 >> 7) & 1
    valid_ttd = (b2 >> 6) & 1
    julian_mod16 = b2 & 0x0F

    consumed = 3
    ttd = 0.0
    if valid_ttd and len(data) >= 5:
        gmt_high16 = struct.unpack("!H", data[3:5])[0]
        consumed = 5
        # Annex A A.3.1.1: GMT seconds since midnight = gmt_high16 * 2
        ttd_gmt_seconds = gmt_high16 * 2
        ttd = float(ttd_gmt_seconds)  # seconds since midnight (caller resolves full date)

    updu = data[consumed:]
    return (
        SPDU(
            version=1,
            src_sap=src_sap,
            dest_sap=dest_sap,
            priority=priority,
            ttd=ttd,
            tx_mode=TxMode.ARQ,
            client_delivery_confirm_required=bool(client_confirm),
            updu=updu,
        ),
        consumed + len(updu),
    )


def encode_spdu_hard_link_request(
    link_type: int,
    link_priority: int,
    requesting_sap: int,
    remote_sap: int,
) -> bytes:
    """Codifica S_PDU tipo 3 (HARD LINK ESTABLISHMENT REQUEST).

    Annex A Fig. A-35 (2 bytes):
      Byte 0: TYPE[7:4]=3 | LINK_TYPE[3:2] | LINK_PRIORITY[1:0]
      Byte 1: REQUESTING_SAP[7:4] | REMOTE_SAP[3:0]
    """
    b0 = (SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST << 4) | ((link_type & 0x03) << 2) | (link_priority & 0x03)
    b1 = ((requesting_sap & 0x0F) << 4) | (remote_sap & 0x0F)
    return bytes([b0, b1])


def decode_spdu_hard_link_request(data: bytes) -> tuple[int, int, int, int]:
    """Decodifica S_PDU tipo 3. Retorna (link_type, link_priority, requesting_sap, remote_sap).

    Annex A Fig. A-35 (2 bytes):
      Byte 0: TYPE[7:4] | LINK_TYPE[3:2] | LINK_PRIORITY[1:0]
      Byte 1: REQUESTING_SAP[7:4] | REMOTE_SAP[3:0]
    """
    if len(data) < 2:
        raise ValueError("S_PDU tipo 3 muito curto")
    link_type = (data[0] >> 2) & 0x03
    link_priority = data[0] & 0x03
    requesting_sap = (data[1] >> 4) & 0x0F
    remote_sap = data[1] & 0x0F
    return link_type, link_priority, requesting_sap, remote_sap


def encode_spdu_hard_link_confirm() -> bytes:
    """Codifica S_PDU tipo 4 (HARD LINK ESTABLISHMENT CONFIRM)."""
    return bytes([SPDU_TYPE_HARD_LINK_ESTABLISH_CONFIRM << 4])


def encode_spdu_hard_link_rejected(reason: int) -> bytes:
    """Codifica S_PDU tipo 5 (HARD LINK ESTABLISHMENT REJECTED).

    Annex A Fig. A-37 (1 byte): TYPE[7:4]=5 | REASON[3:0]
    """
    return bytes([(SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED << 4) | (reason & 0x0F)])


def decode_spdu_hard_link_rejected(data: bytes) -> int:
    """Decodifica S_PDU tipo 5. Retorna reason.

    Annex A Fig. A-37 (1 byte): TYPE[7:4] | REASON[3:0]
    """
    if len(data) < 1:
        return 0
    return data[0] & 0x0F


def encode_spdu_hard_link_terminate(reason: int) -> bytes:
    """Codifica S_PDU tipo 6 (HARD LINK TERMINATE).

    Annex A Fig. A-38 (1 byte): TYPE[7:4]=6 | REASON[3:0]
    """
    return bytes([(SPDU_TYPE_HARD_LINK_TERMINATE << 4) | (reason & 0x0F)])


def decode_spdu_hard_link_terminate(data: bytes) -> int:
    """Decodifica S_PDU tipo 6. Retorna reason.

    Annex A Fig. A-38 (1 byte): TYPE[7:4] | REASON[3:0]
    """
    if len(data) < 1:
        return 0
    return data[0] & 0x0F


def encode_spdu_hard_link_terminate_confirm() -> bytes:
    """Codifica S_PDU tipo 7 (HARD LINK TERMINATE CONFIRM)."""
    return bytes([SPDU_TYPE_HARD_LINK_TERMINATE_CONFIRM << 4])


def encode_spdu_data_delivery_confirm(src_sap: int, dest_sap: int, updu_partial: bytes) -> bytes:
    """Codifica S_PDU tipo 1 (DATA DELIVERY CONFIRM)."""
    return bytes([
        SPDU_TYPE_DATA_DELIVERY_CONFIRM << 4,
        (src_sap & 0x0F) << 4 | (dest_sap & 0x0F),
    ]) + updu_partial


def encode_spdu_data_delivery_fail(
    src_sap: int,
    dest_sap: int,
    reason: int,
    updu_partial: bytes,
) -> bytes:
    """Codifica S_PDU tipo 2 (DATA DELIVERY FAIL)."""
    return bytes([
        SPDU_TYPE_DATA_DELIVERY_FAIL << 4,
        (src_sap & 0x0F) << 4 | (dest_sap & 0x0F),
        reason & 0x0F,
    ]) + updu_partial


def decode_spdu_data_delivery_confirm(data: bytes) -> tuple[int, int, bytes]:
    """Decodifica S_PDU tipo 1. Retorna (src_sap, dest_sap, updu_partial)."""
    if len(data) < 2:
        raise ValueError("S_PDU tipo 1 muito curto")
    src_sap = (data[1] >> 4) & 0x0F
    dest_sap = data[1] & 0x0F
    return src_sap, dest_sap, data[2:]


def decode_spdu_data_delivery_fail(data: bytes) -> tuple[int, int, int, bytes]:
    """Decodifica S_PDU tipo 2. Retorna (src_sap, dest_sap, reason, updu_partial)."""
    if len(data) < 3:
        raise ValueError("S_PDU tipo 2 muito curto")
    src_sap = (data[1] >> 4) & 0x0F
    dest_sap = data[1] & 0x0F
    reason = data[2] & 0x0F
    return src_sap, dest_sap, reason, data[3:]


def spdu_type(data: bytes) -> int:
    """Retorna o tipo do S_PDU (primeiro nibble do primeiro byte)."""
    if not data:
        raise ValueError("S_PDU vazio")
    return (data[0] >> 4) & 0x0F


def encode_spdu(spdu: SPDU) -> bytes:
    """Codifica S_PDU DATA (tipo 0) em bytes conforme Annex A."""
    return encode_spdu_data(spdu)


def decode_spdu(data: bytes) -> SPDU:
    """Decodifica bytes em S_PDU. Detecta tipo e delega."""
    if not data:
        raise ValueError("S_PDU vazio")
    t = spdu_type(data)
    if t == SPDU_TYPE_DATA:
        spdu, _ = decode_spdu_data(data)
        return spdu
    if t == SPDU_TYPE_DATA_DELIVERY_CONFIRM:
        src_sap, dest_sap, updu_partial = decode_spdu_data_delivery_confirm(data)
        return SPDU(
            src_sap=src_sap,
            dest_sap=dest_sap,
            updu=updu_partial,
        )
    if t == SPDU_TYPE_DATA_DELIVERY_FAIL:
        src_sap, dest_sap, reason, updu_partial = decode_spdu_data_delivery_fail(data)
        return SPDU(
            src_sap=src_sap,
            dest_sap=dest_sap,
            updu=updu_partial,
        )
    raise ValueError(f"S_PDU tipo {t} não suportado para decode_spdu genérico")


# ---------------------------------------------------------------------------
# Classes internas
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SapContext:
    sap_id: int
    rank: int = 0
    service: ServiceType = field(default_factory=ServiceType)
    bound: bool = True


@dataclass(slots=True)
class _TxEntry:
    spdu: SPDU
    dest_addr: int
    src_sap: int
    enqueued_at_ms: int
    delivery_mode: DeliveryMode = field(default_factory=DeliveryMode)


@dataclass(slots=True)
class _PendingHardLinkIndication:
    """Requisição tipo 2 aguardando hard_link_accept/reject do cliente."""
    src_addr: int = 0
    remote_sap: int = 0
    link_priority: int = 0
    link_type: int = 0
    requesting_sap: int = 0


@dataclass(slots=True)
class _LinkSession:
    state: SisLinkSessionState = SisLinkSessionState.IDLE
    link_type: LinkType = LinkType.SOFT
    remote_addr: int = 0
    remote_sap: int = 0
    last_activity_ms: int = 0
    hard_link_owner: int = -1  # sap_id do dono do hard link
    hard_link_owner_rank: int = 0  # rank do cliente dono do hard link
    link_priority: int = 0
    sis_hard_link_type: int = 0  # SisHardLinkType 0/1/2
    awaiting_hard_link_response: bool = False  # caller aguardando tipo 4/5
    awaiting_terminate_confirm: bool = False  # aguardando tipo 7
    hard_link_response_timeout_ms: int = 0  # timestamp when establish times out
    terminate_confirm_timeout_ms: int = 0  # timestamp when terminate times out
    pending_indication: Optional[_PendingHardLinkIndication] = None
    is_calling: bool = False  # True if we initiated the hard link


@dataclass(slots=True)
class _SisCallbacks:
    unidata_indication: Optional[Callable] = None
    request_confirm: Optional[Callable] = None
    request_rejected: Optional[Callable] = None
    bind_rejected: Optional[Callable] = None
    hard_link_established: Optional[Callable] = None
    hard_link_indication: Optional[Callable] = None
    hard_link_rejected: Optional[Callable] = None
    hard_link_terminated: Optional[Callable] = None


# ---------------------------------------------------------------------------
# SIS — alias deprecated, use StanagNode
# ---------------------------------------------------------------------------


def __getattr__(name):
    if name == "SIS":
        from src.stanag_node import StanagNode
        return StanagNode
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Ensure `from src.sis import SIS` still works at class level for isinstance checks.
# The module-level __getattr__ above handles attribute access; for subclass aliasing
# we define a thin wrapper that is resolved lazily.
import sys as _sys

class _SISMeta(type):
    """Metaclass that lazily resolves the base class to StanagNode."""
    def __instancecheck__(cls, instance):
        from src.stanag_node import StanagNode
        return isinstance(instance, StanagNode)
    def __subclasscheck__(cls, subclass):
        from src.stanag_node import StanagNode
        return issubclass(subclass, StanagNode)


class SIS(metaclass=_SISMeta):
    """Deprecated: use StanagNode."""
    def __new__(cls, *args, **kwargs):
        from src.stanag_node import StanagNode
        return StanagNode(*args, **kwargs)
