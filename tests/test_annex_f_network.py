"""Tests for ether_client.py and ip_client.py — network clients."""

import struct

import pytest

from src.annex_f.ether_client import (
    EtherClient,
    EtherFrame,
    encode_ec_frame,
    decode_ec_frame,
    stanag_addr_to_pseudo_ether,
    pseudo_ether_to_stanag_addr,
    ETHERTYPE_IPV4,
    ETHERTYPE_IPV6,
    ETHERTYPE_ARP,
    ETHERTYPE_PPP,
)
from src.annex_f.ip_client import IPClient, QoSMode, _DSCP_PRIORITY_MAP
from src.stypes import DeliveryMode

from tests.annex_f_helpers import MockNode, deliver


# ---------------------------------------------------------------------------
# Helper: build minimal valid IPv4 datagram
# ---------------------------------------------------------------------------

def _make_ipv4(dst_bytes, payload=b"", tos=0, flags=0, src_bytes=None):
    """Build minimal 20-byte IPv4 header + payload.

    Args:
        dst_bytes: 4-byte destination IP (e.g. b'\\xC0\\xA8\\x01\\x01' = 192.168.1.1)
        payload: payload bytes
        tos: TOS byte
        flags: 3-bit flags (bit 1=DF, bit 2=MF) in high bits of byte 6
        src_bytes: 4-byte source IP (default 10.0.0.1)
    """
    if src_bytes is None:
        src_bytes = bytes([10, 0, 0, 1])
    ihl = 5  # 20 bytes, no options
    version_ihl = (4 << 4) | ihl
    total_length = 20 + len(payload)
    identification = 0x1234
    flags_offset = (flags << 13) & 0xFFFF
    ttl = 64
    protocol = 6  # TCP
    header = struct.pack(
        ">BBHHHBBH4s4s",
        version_ihl, tos, total_length,
        identification, flags_offset,
        ttl, protocol, 0,  # checksum=0 placeholder
        src_bytes, dst_bytes,
    )
    # Calculate checksum
    checksum = IPClient._ip_checksum(header)
    header = header[:10] + struct.pack(">H", checksum) + header[12:]
    return header + payload


# ===========================================================================
# EC_FRAME codec
# ===========================================================================

class TestEcFrame:
    def test_encode_decode_roundtrip(self):
        frame = EtherFrame(0x0800, b"payload")
        raw = encode_ec_frame(frame)
        f2 = decode_ec_frame(raw)
        assert f2.ethertype == 0x0800
        assert f2.data == b"payload"

    def test_decode_truncated(self):
        with pytest.raises(ValueError):
            decode_ec_frame(b"\x08")

    def test_invalid_ethertype(self):
        with pytest.raises(ValueError):
            EtherFrame(0x10000, b"")


# ===========================================================================
# ARP pseudo-Ethernet mapping
# ===========================================================================

class TestPseudoEther:
    def test_stanag_to_pseudo(self):
        result = stanag_addr_to_pseudo_ether(1)
        assert result == b"\x50\x66\x00\x00\x00\x01"

    def test_pseudo_to_stanag(self):
        result = pseudo_ether_to_stanag_addr(b"\x50\x66\x00\x00\x00\x01")
        assert result == 1

    def test_roundtrip(self):
        for addr in [0, 1, 255, 65535, 0x0FFFFFFF]:
            pseudo = stanag_addr_to_pseudo_ether(addr)
            assert pseudo_ether_to_stanag_addr(pseudo) == addr

    def test_invalid_prefix(self):
        with pytest.raises(ValueError):
            pseudo_ether_to_stanag_addr(b"\xAA\xBB\x00\x00\x00\x01")

    def test_short_input(self):
        with pytest.raises(ValueError):
            pseudo_ether_to_stanag_addr(b"\x50\x66")


# ===========================================================================
# EtherClient (SAP 8)
# ===========================================================================

class TestEtherClient:
    def test_send_frame_arq(self):
        node = MockNode()
        c = EtherClient(node)
        c.send_frame(dest_addr=1, ethertype=0x0800, data=b"ip_data")
        assert node.sent[0]["mode"].arq_mode is True
        # Verify EC_FRAME format in updu
        updu = node.sent[0]["updu"]
        frame = decode_ec_frame(updu)
        assert frame.ethertype == 0x0800
        assert frame.data == b"ip_data"

    def test_send_ipv4(self):
        node = MockNode()
        c = EtherClient(node)
        c.send_ipv4(dest_addr=1, ipv4_datagram=b"ipv4data")
        frame = decode_ec_frame(node.sent[0]["updu"])
        assert frame.ethertype == ETHERTYPE_IPV4

    def test_send_ipv6(self):
        node = MockNode()
        c = EtherClient(node)
        c.send_ipv6(dest_addr=1, ipv6_datagram=b"ipv6data")
        frame = decode_ec_frame(node.sent[0]["updu"])
        assert frame.ethertype == ETHERTYPE_IPV6

    def test_send_arp_non_arq(self):
        node = MockNode()
        c = EtherClient(node)
        c.send_arp(dest_addr=1, arp_packet=b"arp")
        assert node.sent[0]["mode"].arq_mode is False

    def test_send_ppp_in_order(self):
        node = MockNode()
        c = EtherClient(node)
        c.send_ppp(dest_addr=1, ppp_frame=b"ppp")
        mode = node.sent[0]["mode"]
        assert mode.arq_mode is True
        assert mode.in_order is True
        frame = decode_ec_frame(node.sent[0]["updu"])
        assert frame.ethertype == ETHERTYPE_PPP

    def test_receive_dispatch_by_ethertype(self):
        node = MockNode()
        c = EtherClient(node)
        received = []
        c.register_protocol(0x0800, lambda addr, data: received.append((addr, data)))
        ec_frame = encode_ec_frame(EtherFrame(0x0800, b"ip_payload"))
        deliver(c, src_addr=42, data=ec_frame)
        assert received == [(42, b"ip_payload")]

    def test_receive_catch_all(self):
        node = MockNode()
        c = EtherClient(node)
        received = []
        c.on_frame_received = lambda addr, frame: received.append((addr, frame))
        ec_frame = encode_ec_frame(EtherFrame(0x0800, b"data"))
        deliver(c, src_addr=10, data=ec_frame)
        assert len(received) == 1
        assert received[0][1].ethertype == 0x0800

    def test_sap_id_is_8(self):
        assert EtherClient.SAP_ID == 8


# ===========================================================================
# IP checksum
# ===========================================================================

class TestIPChecksum:
    def test_known_checksum(self):
        """Verify checksum of a known valid IPv4 header is 0."""
        datagram = _make_ipv4(bytes([192, 168, 1, 1]))
        header = datagram[:20]
        assert IPClient._ip_checksum(header) == 0

    def test_checksum_nonzero_for_corrupted(self):
        datagram = _make_ipv4(bytes([192, 168, 1, 1]))
        header = bytearray(datagram[:20])
        header[5] ^= 0xFF  # corrupt a byte
        assert IPClient._ip_checksum(header) != 0


# ===========================================================================
# IPClient DSCP mapping
# ===========================================================================

class TestIPClientDSCP:
    @pytest.mark.parametrize("dscp_class,expected_priority", [
        (0b000, 0), (0b001, 2), (0b010, 4), (0b011, 6),
        (0b100, 8), (0b101, 10), (0b110, 12), (0b111, 14),
    ])
    def test_dscp_class_to_priority(self, dscp_class, expected_priority):
        node = MockNode()
        c = IPClient(node, qos_mode=QoSMode.DSCP)
        # DSCP class is bits [7:5] of TOS byte (when DSCP bits [5:3] of the 6-bit DSCP)
        # TOS byte: DSCP[5:0] | ECN[1:0], DSCP class = DSCP[5:3]
        tos = (dscp_class << 5)  # class in top 3 bits of DSCP, which is top bits of TOS
        priority, mode = c._map_dscp(tos, is_multicast=False)
        assert priority == expected_priority

    def test_multicast_non_arq(self):
        node = MockNode()
        c = IPClient(node, qos_mode=QoSMode.DSCP)
        _, mode = c._map_dscp(0x00, is_multicast=True)
        assert mode.arq_mode is False


# ===========================================================================
# IPClient TOS mapping
# ===========================================================================

class TestIPClientTOS:
    def test_minimize_delay_non_arq(self):
        # delay=1, throughput=0, reliability=0, cost=0
        tos = 0x10  # bit 4
        priority, mode = IPClient._map_tos_rfc1349(tos, is_multicast=False)
        assert mode.arq_mode is False

    def test_minimize_cost_non_arq(self):
        # delay=0, throughput=0, reliability=0, cost=1
        tos = 0x02  # bit 1
        priority, mode = IPClient._map_tos_rfc1349(tos, is_multicast=False)
        assert mode.arq_mode is False

    def test_maximize_throughput_arq(self):
        # throughput=1
        tos = 0x08  # bit 3
        priority, mode = IPClient._map_tos_rfc1349(tos, is_multicast=False)
        assert mode.arq_mode is True

    def test_maximize_reliability_arq(self):
        # reliability=1
        tos = 0x04  # bit 2
        priority, mode = IPClient._map_tos_rfc1349(tos, is_multicast=False)
        assert mode.arq_mode is True

    def test_default_all_zero_arq(self):
        priority, mode = IPClient._map_tos_rfc1349(0x00, is_multicast=False)
        assert mode.arq_mode is True

    def test_multicast_always_non_arq(self):
        priority, mode = IPClient._map_tos_rfc1349(0x00, is_multicast=True)
        assert mode.arq_mode is False


# ===========================================================================
# IPClient send / fragmentation
# ===========================================================================

class TestIPClientSend:
    def test_send_datagram_resolved(self):
        node = MockNode()
        c = IPClient(node, address_table={"192.168.1.1": 100})
        datagram = _make_ipv4(bytes([192, 168, 1, 1]), b"payload")
        result = c.send_ip_datagram(datagram)
        assert result is True
        assert len(node.sent) == 1
        assert node.sent[0]["dest_addr"] == 100

    def test_unresolved_address(self):
        node = MockNode()
        c = IPClient(node)
        datagram = _make_ipv4(bytes([10, 0, 0, 1]))
        result = c.send_ip_datagram(datagram)
        assert result is False
        assert len(node.sent) == 0

    def test_df_exceeds_mtu(self):
        node = MockNode()
        c = IPClient(node, address_table={"192.168.1.1": 100})
        c.mtu = 30  # very small
        # flags=2 means DF bit set
        datagram = _make_ipv4(bytes([192, 168, 1, 1]), b"A" * 50, flags=2)
        result = c.send_ip_datagram(datagram)
        assert result is False

    def test_fragmentation(self):
        node = MockNode()
        c = IPClient(node, address_table={"192.168.1.1": 100})
        c.mtu = 40  # Force fragmentation (20 header + 20 payload max)
        payload = b"A" * 100
        datagram = _make_ipv4(bytes([192, 168, 1, 1]), payload)
        result = c.send_ip_datagram(datagram)
        assert result is True
        assert len(node.sent) > 1  # Multiple fragments
        # Each fragment should fit in MTU
        for s in node.sent:
            assert len(s["updu"]) <= c.mtu

    def test_short_datagram_rejected(self):
        node = MockNode()
        c = IPClient(node)
        assert c.send_ip_datagram(b"\x00" * 10) is False

    def test_non_ipv4_rejected(self):
        node = MockNode()
        c = IPClient(node)
        # Version 6 in header
        header = b"\x60" + b"\x00" * 19
        assert c.send_ip_datagram(header) is False

    def test_address_mapping(self):
        node = MockNode()
        c = IPClient(node)
        c.add_address_mapping("1.2.3.4", 42)
        assert c.resolve_address("1.2.3.4") == 42
        assert c.resolve_stanag_to_ip(42) == "1.2.3.4"
        c.remove_address_mapping("1.2.3.4")
        assert c.resolve_address("1.2.3.4") is None
