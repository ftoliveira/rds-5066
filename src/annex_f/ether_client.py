"""
F.11 — ETHER Client (SAP 8).

Fornece suporte multi-protocolo via campo Ethertype, encapsulando dados em
EC_FRAMEs transmitidos via S_UNIDATA_REQUEST.

EC_FRAME (F.11.2, Figura F-15):
  Bytes 0-1: Ethertype (big-endian, 2 bytes)
  Bytes 2+:  Data Field (payload do protocolo)

Ethertypes suportados (Tabela F-6):
  0x0800  IPv4
  0x86DD  IPv6
  0x876B  VJ Compressed IP (RFC 1144)
  0x0180  ROHC — experimental (RFC 3843)
  0x0806  ARP
  0x880B  PPP

Mapeamento de endereço ARP (F.11.5.4, Figura F-17):
  Pseudo-Ethernet 48 bits = 0x5066 + STANAG_5066_address (32 bits)

Requisitos de serviço (F.11.4):
  - MTU mínimo: 2048 bytes (EC_FRAME completo)
  - ARQ ou non-ARQ conforme protocolo encapsulado
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Callable

from src.stypes import DeliveryMode, SAP_ID_ETHER, SisUnidataIndication

from .base_client import SubnetClient

logger = logging.getLogger(__name__)

# ─── Ethertypes (Tabela F-6) ──────────────────────────────────────────────────

ETHERTYPE_IPV4 = 0x0800   # IPv4 (F.11.5.1)
ETHERTYPE_IPV6 = 0x86DD   # IPv6 (F.11.5.2)
ETHERTYPE_VJCOMP = 0x876B  # Van Jacobsen TCP/IP header compression (F.11.5.3)
ETHERTYPE_ROHC = 0x0180   # ROHC — valor experimental (F.11.5.3)
ETHERTYPE_ARP = 0x0806    # ARP (F.11.5.4)
ETHERTYPE_PPP = 0x880B    # PPP (F.11.5.5)

EC_FRAME_HEADER_SIZE = 2  # bytes Ethertype

# Prefixo de pseudo-Ethernet para mapeamento ARP (F.11.5.4)
ARP_PSEUDO_ETHER_PREFIX = 0x5066


@dataclass(slots=True)
class EtherFrame:
    """EC_FRAME: cabeçalho Ethertype + campo de dados (F.11.2)."""

    ethertype: int   # 16 bits big-endian
    data: bytes      # payload do protocolo encapsulado

    def __post_init__(self):
        if not (0 <= self.ethertype <= 0xFFFF):
            raise ValueError(f"Ethertype inválido: 0x{self.ethertype:04X}")


def encode_ec_frame(frame: EtherFrame) -> bytes:
    """Codifica EC_FRAME em bytes."""
    return struct.pack(">H", frame.ethertype) + frame.data


def decode_ec_frame(raw: bytes) -> EtherFrame:
    """Decodifica bytes em EtherFrame."""
    if len(raw) < EC_FRAME_HEADER_SIZE:
        raise ValueError(f"EC_FRAME truncado: {len(raw)} bytes")
    (ethertype,) = struct.unpack_from(">H", raw)
    return EtherFrame(ethertype, raw[EC_FRAME_HEADER_SIZE:])


# ─── Funções auxiliares ARP ───────────────────────────────────────────────────


def stanag_addr_to_pseudo_ether(stanag_addr: int) -> bytes:
    """Converte endereço STANAG 5066 em pseudo-Ethernet de 6 bytes (F.11.5.4)."""
    prefix = struct.pack(">H", ARP_PSEUDO_ETHER_PREFIX)  # 0x50 0x66
    addr_bytes = struct.pack(">I", stanag_addr & 0xFFFFFFFF)
    return prefix + addr_bytes


def pseudo_ether_to_stanag_addr(pseudo_ether: bytes) -> int:
    """Converte pseudo-Ethernet de 6 bytes em endereço STANAG 5066."""
    if len(pseudo_ether) < 6:
        raise ValueError(f"Pseudo-Ethernet deve ter 6 bytes, recebeu {len(pseudo_ether)}")
    (prefix,) = struct.unpack_from(">H", pseudo_ether)
    if prefix != ARP_PSEUDO_ETHER_PREFIX:
        raise ValueError(f"Prefixo inválido: 0x{prefix:04X} (esperado 0x5066)")
    (addr,) = struct.unpack_from(">I", pseudo_ether, 2)
    return addr


# ─── EtherClient ──────────────────────────────────────────────────────────────

# Assinatura do handler de protocolo: (src_addr, data_field)
ProtoHandler = Callable[[int, bytes], None]


class EtherClient(SubnetClient):
    """F.11 — ETHER Client, SAP 8.

    Suporte multi-protocolo via campo Ethertype. Handlers são registrados
    por Ethertype e chamados quando um EC_FRAME é recebido.

    Envio:
        client.send_frame(dest_addr, ETHERTYPE_IPV4, ipv4_bytes)
        client.send_frame(dest_addr, ETHERTYPE_ARP, arp_bytes, arq=False)

    Recepção:
        client.register_protocol(ETHERTYPE_IPV4, my_ipv4_handler)
        # handler(src_addr: int, data: bytes)
    """

    SAP_ID = SAP_ID_ETHER  # 8

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self._handlers: dict[int, ProtoHandler] = {}
        self.on_frame_received: Callable[[int, EtherFrame], None] | None = None

    # ── Registro de handlers ──────────────────────────────────────────────────

    def register_protocol(self, ethertype: int, handler: ProtoHandler):
        """Registra handler para um Ethertype específico.

        O handler é chamado com (src_addr: int, data: bytes).
        """
        self._handlers[ethertype] = handler
        logger.debug("EtherClient: registrado handler para Ethertype 0x%04X", ethertype)

    # ── Envio ─────────────────────────────────────────────────────────────────

    def send_frame(
        self,
        dest_addr: int,
        ethertype: int,
        data: bytes,
        arq: bool = True,
        priority: int = 5,
        ttl_seconds: float = 120.0,
    ) -> None:
        """Envia EC_FRAME com o Ethertype e dados fornecidos.

        Para IPv4/IPv6 unicast: arq=True (F.11.5.1, F.11.5.2).
        Para multicast / ARP broadcast: arq=False.
        Para PPP: arq=True, in_order=True (F.11.5.5).
        """
        frame = EtherFrame(ethertype, data)
        # F.11.2: first two bytes of EC_FRAME placed in first two bytes of U_PDU
        frame_bytes = encode_ec_frame(frame)
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=frame_bytes,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=arq),
        )
        logger.debug(
            "EtherClient → addr=%d Ethertype=0x%04X %d bytes (arq=%s)",
            dest_addr, ethertype, len(data), arq,
        )

    def send_ipv4(self, dest_addr: int, ipv4_datagram: bytes, **kw) -> None:
        """Atalho para IPv4-over-Ether (F.11.5.1, Ethertype=0x0800)."""
        self.send_frame(dest_addr, ETHERTYPE_IPV4, ipv4_datagram, arq=True, **kw)

    def send_ipv6(self, dest_addr: int, ipv6_datagram: bytes, **kw) -> None:
        """Atalho para IPv6-over-Ether (F.11.5.2, Ethertype=0x86DD)."""
        self.send_frame(dest_addr, ETHERTYPE_IPV6, ipv6_datagram, arq=True, **kw)

    def send_arp(self, dest_addr: int, arp_packet: bytes, **kw) -> None:
        """Atalho para ARP-over-Ether (F.11.5.4, Ethertype=0x0806, non-ARQ)."""
        self.send_frame(dest_addr, ETHERTYPE_ARP, arp_packet, arq=False, **kw)

    def send_ppp(self, dest_addr: int, ppp_frame: bytes, **kw) -> None:
        """Atalho para PPP-over-Ether (F.11.5.5, Ethertype=0x880B, ARQ, in-order)."""
        # F.11.5.5: PPP requires in-order delivery
        from src.stypes import DeliveryMode as _DM
        mode = _DM(arq_mode=True, in_order=True)
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=encode_ec_frame(EtherFrame(ETHERTYPE_PPP, ppp_frame)),
            mode=mode,
            **kw,
        )

    # ── Recepção ──────────────────────────────────────────────────────────────

    def _on_data_received(self, src_addr: int, data: bytes):
        """Decodifica EC_FRAME e despacha por Ethertype."""
        try:
            frame = decode_ec_frame(data)
        except ValueError as exc:
            logger.warning("EtherClient ← addr=%d: EC_FRAME inválido: %s", src_addr, exc)
            return

        logger.debug(
            "EtherClient ← addr=%d Ethertype=0x%04X %d bytes",
            src_addr, frame.ethertype, len(frame.data),
        )

        # catch-all opcional
        if self.on_frame_received:
            self.on_frame_received(src_addr, frame)

        # handler por Ethertype
        handler = self._handlers.get(frame.ethertype)
        if handler:
            handler(src_addr, frame.data)
        elif not self.on_frame_received:
            logger.warning(
                "EtherClient: nenhum handler para Ethertype=0x%04X de addr=%d",
                frame.ethertype, src_addr,
            )
