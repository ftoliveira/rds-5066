"""
F.12 — IP Client (SAP 9) — MANDATORY (Edition 3).

Encapsula datagramas IP no U_PDU para transporte sobre sub-rede HF.
Suporta unicast (ARQ) e multicast (non-ARQ) com mapping DSCP/TOS -> priority.
"""

from __future__ import annotations

import logging
import struct

from .base_client import SubnetClient
from src.stypes import DeliveryMode, SAP_ID_IP_CLIENT

logger = logging.getLogger(__name__)


# DSCP class (3 MSBs of DSCP field) -> (priority, description)
# Based on Table 9, F.12.4.2
_DSCP_PRIORITY_MAP = {
    0b000: 0,   # Routine / Best Effort
    0b001: 2,   # Priority / AF1
    0b010: 4,   # Immediate / AF2
    0b011: 6,   # Flash / AF3
    0b100: 8,   # Flash Override / AF4
    0b101: 10,  # Critical / EF
    0b110: 12,  # Internetwork Control
    0b111: 14,  # Network Control
}


class QoSMode:
    """Modo de mapeamento QoS para IP Client (F.12.4)."""
    TOS = "tos"      # RFC 1349 TOS-based (Table F-8)
    DSCP = "dscp"    # DiffServ DSCP-based (Table 9) — PREFERRED


class IPClient(SubnetClient):
    """IP Client — Annex F.12, SAP 9 (MANDATORY)."""

    SAP_ID = SAP_ID_IP_CLIENT  # 9

    def __init__(self, node, address_table: dict[str, int] | None = None,
                 connection_id: int = 0,
                 qos_mode: str = QoSMode.DSCP):
        """
        Args:
            node: StanagNode instance.
            address_table: Mapping {ip_address_str: stanag_5066_address}.
                           Minimum 10 entries recommended (F.12 requirement).
            qos_mode: QoS mapping mode — 'dscp' (preferred) or 'tos' (RFC 1349).
        """
        super().__init__(node, connection_id)
        self._address_table: dict[str, int] = address_table or {}
        self._reverse_table: dict[int, str] = {v: k for k, v in self._address_table.items()}
        self._mtu = 2048
        self.qos_mode = qos_mode
        self.on_ip_received: callable | None = None

    def send_ip_datagram(self, datagram: bytes) -> bool:
        """Encapsula e envia datagrama IP via SIS.

        Returns True se enviado, False se falhou (addr não resolvido, etc).
        """
        if len(datagram) < 20:
            logger.warning("IP datagram too short: %d bytes", len(datagram))
            return False

        # Parse IP header
        version_ihl = datagram[0]
        version = (version_ihl >> 4) & 0x0F
        if version != 4:
            logger.warning("Only IPv4 supported, got version %d", version)
            return False

        tos = datagram[1]
        total_length = struct.unpack_from('!H', datagram, 2)[0]
        flags = (datagram[6] >> 5) & 0x07
        dont_fragment = bool(flags & 0x02)

        # Extract destination IP
        dst_ip = f"{datagram[16]}.{datagram[17]}.{datagram[18]}.{datagram[19]}"

        # Check if multicast (224.0.0.0/4)
        is_multicast = (datagram[16] & 0xF0) == 0xE0

        # Resolve destination STANAG address
        stanag_addr = self.resolve_address(dst_ip)
        if stanag_addr is None:
            logger.warning("Cannot resolve IP %s to STANAG address", dst_ip)
            return False

        # Path MTU check (RFC 1191)
        if dont_fragment and total_length > self._mtu:
            logger.warning("Datagram DF set and size %d > MTU %d, dropping",
                           total_length, self._mtu)
            return False

        # Map TOS/DSCP -> priority + delivery mode
        priority, mode = self._map_tos_to_delivery(tos, is_multicast)

        # F.12.2: first byte of IP datagram aligned with first byte of U_PDU
        if total_length <= self._mtu:
            self._send_data(
                dest_addr=stanag_addr,
                dest_sap=self.SAP_ID,
                data=datagram,
                priority=priority,
                ttl_seconds=120.0,
                mode=mode,
            )
        else:
            # IP fragmentation: split datagram into MTU-sized fragments
            fragments = self._fragment_ipv4(datagram, self._mtu)
            for frag in fragments:
                self._send_data(
                    dest_addr=stanag_addr,
                    dest_sap=self.SAP_ID,
                    data=frag,
                    priority=priority,
                    ttl_seconds=120.0,
                    mode=mode,
                )

        logger.debug("IP datagram to %s (STANAG %d), %d bytes, priority=%d, arq=%s",
                      dst_ip, stanag_addr, len(datagram), priority, mode.arq_mode)
        return True

    @staticmethod
    def _fragment_ipv4(datagram: bytes, mtu: int) -> list[bytes]:
        """Fragment an IPv4 datagram into pieces that fit in the MTU.

        Each fragment is a valid IPv4 datagram with updated flags/offset/length.
        Fragment offset is in units of 8 bytes.
        """
        ihl = (datagram[0] & 0x0F) * 4  # header length in bytes
        header = bytearray(datagram[:ihl])
        payload = datagram[ihl:]
        identification = struct.unpack_from('!H', datagram, 4)[0]
        orig_flags_offset = struct.unpack_from('!H', datagram, 6)[0]
        orig_offset = (orig_flags_offset & 0x1FFF) * 8  # byte offset

        max_payload = ((mtu - ihl) // 8) * 8  # must be multiple of 8
        fragments = []
        offset = 0

        while offset < len(payload):
            chunk = payload[offset:offset + max_payload]
            is_last = (offset + len(chunk)) >= len(payload)

            frag_header = bytearray(header)
            # Total length
            frag_len = ihl + len(chunk)
            struct.pack_into('!H', frag_header, 2, frag_len)
            # Flags + Fragment Offset
            frag_offset_units = (orig_offset + offset) // 8
            mf = 0 if is_last else 0x2000  # More Fragments
            frag_header[6] = (mf >> 8) | ((frag_offset_units >> 8) & 0x1F)
            frag_header[7] = frag_offset_units & 0xFF
            # Recalculate header checksum
            frag_header[10] = 0
            frag_header[11] = 0
            checksum = IPClient._ip_checksum(frag_header)
            struct.pack_into('!H', frag_header, 10, checksum)

            fragments.append(bytes(frag_header) + chunk)
            offset += len(chunk)

        return fragments

    @staticmethod
    def _ip_checksum(header: bytes) -> int:
        """Calculate IPv4 header checksum (RFC 791)."""
        if len(header) % 2:
            header = header + b'\x00'
        total = 0
        for i in range(0, len(header), 2):
            total += (header[i] << 8) + header[i + 1]
        total = (total >> 16) + (total & 0xFFFF)
        total += total >> 16
        return ~total & 0xFFFF

    def _on_data_received(self, src_addr: int, data: bytes):
        """Recebe datagrama IP da rede HF."""
        if len(data) < 20:
            logger.warning("Received short IP datagram (%d bytes) from addr=%d",
                           len(data), src_addr)
            return

        src_ip = self._reverse_table.get(src_addr, f"stanag:{src_addr}")
        logger.debug("IP datagram received from %s (%d bytes)", src_ip, len(data))

        if self.on_ip_received:
            self.on_ip_received(data, src_addr)

    def _map_tos_to_delivery(self, tos: int, is_multicast: bool) -> tuple[int, DeliveryMode]:
        """Map TOS/DSCP byte to (priority, DeliveryMode).

        Supports two modes per F.12.4:
          - DSCP (preferred): DiffServ Code Points, Table 9 (F.12.4.2)
          - TOS: RFC 1349 TOS precedence bits, Table F-8 (F.12.4.1)
        """
        if self.qos_mode == QoSMode.TOS:
            return self._map_tos_rfc1349(tos, is_multicast)
        return self._map_dscp(tos, is_multicast)

    def _map_dscp(self, tos: int, is_multicast: bool) -> tuple[int, DeliveryMode]:
        """DSCP-based QoS mapping (Table 9, F.12.4.2) — PREFERRED."""
        dscp = (tos >> 2) & 0x3F
        dscp_class = (dscp >> 3) & 0x07
        priority = _DSCP_PRIORITY_MAP.get(dscp_class, 0)
        arq = not is_multicast
        mode = DeliveryMode(arq_mode=arq)
        return priority, mode

    @staticmethod
    def _map_tos_rfc1349(tos: int, is_multicast: bool) -> tuple[int, DeliveryMode]:
        """RFC 1349 TOS-based QoS mapping (Table F-8, F.12.4.1).

        TOS byte bits [4:1]: Delay, Throughput, Reliability, Cost
        Priority derived from IP Precedence bits [7:5] per Table F-7.
        """
        # IP Precedence (bits 7:5) -> priority per Table F-7
        precedence = (tos >> 5) & 0x07
        priority = _DSCP_PRIORITY_MAP.get(precedence, 0)

        if is_multicast:
            # Multicast: always non-ARQ; reliability bit toggles repeats
            reliability = (tos >> 2) & 0x01
            # reliability=1 → non-ARQ with repeats > 1 (handled by SIS)
            return priority, DeliveryMode(arq_mode=False)

        # Unicast: TOS flags determine ARQ/non-ARQ per Table F-8
        delay = (tos >> 4) & 0x01       # Minimize delay
        throughput = (tos >> 3) & 0x01   # Maximize throughput
        reliability = (tos >> 2) & 0x01  # Maximize reliability
        cost = (tos >> 1) & 0x01         # Minimize cost

        if delay and not throughput and not reliability and not cost:
            # "Minimize delay" → non-ARQ
            return priority, DeliveryMode(arq_mode=False)
        if not delay and not throughput and not reliability and cost:
            # "Minimize cost" → non-ARQ
            return priority, DeliveryMode(arq_mode=False)
        # "Maximize throughput" or "Maximize reliability" or default → ARQ
        return priority, DeliveryMode(arq_mode=True)

    def resolve_address(self, ip_addr: str) -> int | None:
        """Lookup IP -> STANAG 5066 address."""
        return self._address_table.get(ip_addr)

    def resolve_stanag_to_ip(self, stanag_addr: int) -> str | None:
        """Reverse lookup STANAG -> IP address."""
        return self._reverse_table.get(stanag_addr)

    def add_address_mapping(self, ip_addr: str, stanag_addr: int):
        """Adiciona entrada na tabela estática de enderecos."""
        self._address_table[ip_addr] = stanag_addr
        self._reverse_table[stanag_addr] = ip_addr

    def remove_address_mapping(self, ip_addr: str):
        """Remove entrada da tabela."""
        stanag_addr = self._address_table.pop(ip_addr, None)
        if stanag_addr is not None:
            self._reverse_table.pop(stanag_addr, None)

    @property
    def mtu(self) -> int:
        return self._mtu

    @mtu.setter
    def mtu(self, value: int):
        self._mtu = value
