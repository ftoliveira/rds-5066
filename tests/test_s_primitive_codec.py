"""Tests for S_PRIMITIVE codec — STANAG 5066 v3 Annex A wire format."""

import struct
import pytest

from src.s_primitive_codec import (
    PREAMBLE, VERSION, HEADER_SIZE,
    encode_s_primitive, decode_s_primitive, decode_primitive_auto,
    encode_address, decode_address,
    encode_delivery_mode, decode_delivery_mode,
    encode_service_type, decode_service_type,
    _pack_hard_link_byte, _unpack_hard_link_byte,
    encode_bind_request, decode_bind_request,
    encode_bind_accepted, decode_bind_accepted,
    encode_bind_rejected, decode_bind_rejected,
    encode_unbind_request, decode_unbind_request,
    encode_unbind_indication, decode_unbind_indication,
    encode_hard_link_establish, decode_hard_link_establish,
    encode_hard_link_terminate, decode_hard_link_terminate,
    encode_hard_link_established, decode_hard_link_established,
    encode_hard_link_rejected, decode_hard_link_rejected,
    encode_hard_link_terminated, decode_hard_link_terminated,
    encode_hard_link_indication, decode_hard_link_indication,
    encode_hard_link_accept, decode_hard_link_accept,
    encode_hard_link_reject, decode_hard_link_reject,
    encode_subnet_availability, decode_subnet_availability,
    encode_unidata_request, decode_unidata_request,
    encode_unidata_indication, decode_unidata_indication,
    encode_unidata_request_confirm, decode_unidata_request_confirm,
    encode_unidata_request_rejected, decode_unidata_request_rejected,
    encode_expedited_unidata_request, decode_expedited_unidata_request,
    encode_expedited_unidata_indication, decode_expedited_unidata_indication,
    encode_expedited_unidata_request_confirm, decode_expedited_unidata_request_confirm,
    encode_expedited_unidata_request_rejected, decode_expedited_unidata_request_rejected,
    encode_keep_alive, encode_data_flow_on, encode_data_flow_off,
    encode_management_msg_request, decode_management_msg_request,
)
from src.stypes import SPrimitiveType


# -----------------------------------------------------------------------
# Generic wrapper
# -----------------------------------------------------------------------

class TestGenericWrapper:
    def test_encode_has_preamble_version_size(self):
        raw = encode_s_primitive(1, b'\xAA\xBB')
        assert raw[:2] == PREAMBLE
        assert raw[2] == VERSION
        size = struct.unpack_from('<H', raw, 3)[0]
        assert size == 3  # 1 (type) + 2 (payload)

    def test_roundtrip(self):
        raw = encode_s_primitive(42, b'\x01\x02\x03')
        ptype, payload, consumed = decode_s_primitive(raw)
        assert ptype == 42
        assert payload == b'\x01\x02\x03'
        assert consumed == len(raw)

    def test_preamble_not_found(self):
        with pytest.raises(ValueError, match="Preamble"):
            decode_s_primitive(b'\x00\x00\x00')

    def test_incomplete_header(self):
        with pytest.raises(ValueError, match="Incomplete"):
            decode_s_primitive(PREAMBLE + b'\x00')

    def test_incomplete_data(self):
        with pytest.raises(ValueError, match="Incomplete"):
            decode_s_primitive(PREAMBLE + b'\x00' + struct.pack('<H', 100) + b'\x01')


# -----------------------------------------------------------------------
# Address encoding A.2.2.28.1
# -----------------------------------------------------------------------

class TestAddress:
    def test_roundtrip(self):
        for addr, sz, grp in [(0, 1, False), (0x0FFFFFFF, 7, True), (0x1234, 4, False)]:
            encoded = encode_address(addr, size=sz, group=grp)
            assert len(encoded) == 4
            a, s, g = decode_address(encoded)
            assert a == addr
            assert s == sz
            assert g == grp

    def test_default_size(self):
        encoded = encode_address(0x42)
        a, s, g = decode_address(encoded)
        assert a == 0x42
        assert s == 7
        assert g is False


# -----------------------------------------------------------------------
# Delivery Mode A.2.2.28.2
# -----------------------------------------------------------------------

class TestDeliveryMode:
    def test_roundtrip(self):
        encoded = encode_delivery_mode(tx_mode=2, delivery_confirm=1,
                                       delivery_order=True, ext=True)
        assert len(encoded) == 1
        d = decode_delivery_mode(encoded)
        assert d['tx_mode'] == 2
        assert d['delivery_confirm'] == 1
        assert d['delivery_order'] is True
        assert d['extension'] is True

    def test_zeros(self):
        d = decode_delivery_mode(encode_delivery_mode(0))
        assert d == {'tx_mode': 0, 'delivery_confirm': 0,
                     'delivery_order': False, 'extension': False}


# -----------------------------------------------------------------------
# Service Type (Fig A-3, 2 bytes)
# -----------------------------------------------------------------------

class TestServiceType:
    def test_roundtrip(self):
        for tm, dc, do_, ext, mr in [
            (0, 0, False, False, 0),
            (2, 1, True, True, 15),
            (3, 3, True, True, 8),
        ]:
            encoded = encode_service_type(tm, dc, do_, ext, mr)
            assert len(encoded) == 2
            d = decode_service_type(encoded)
            assert d['transmission_mode'] == tm
            assert d['delivery_confirmation'] == dc
            assert d['delivery_order'] == do_
            assert d['extended'] == ext
            assert d['min_retransmissions'] == mr

    def test_bit_layout(self):
        # transmission_mode=2 -> bits 15:14 = 10
        # delivery_confirmation=1 -> bits 13:12 = 01
        # delivery_order=True -> bit 11 = 1
        # extended=False -> bit 10 = 0
        # min_retransmissions=5 -> bits 9:6 = 0101
        encoded = encode_service_type(2, 1, True, False, 5)
        val = struct.unpack('>H', encoded)[0]
        assert (val >> 14) & 0x03 == 2
        assert (val >> 12) & 0x03 == 1
        assert (val >> 11) & 0x01 == 1
        assert (val >> 10) & 0x01 == 0
        assert (val >> 6) & 0x0F == 5


# -----------------------------------------------------------------------
# Hard link packed byte
# -----------------------------------------------------------------------

class TestHardLinkPackedByte:
    @pytest.mark.parametrize("lt,lp,sap", [
        (0, 0, 0), (3, 3, 15), (2, 1, 7), (1, 2, 10),
    ])
    def test_roundtrip(self, lt, lp, sap):
        packed = _pack_hard_link_byte(lt, lp, sap)
        assert 0 <= packed <= 0xFF
        a, b, c = _unpack_hard_link_byte(packed)
        assert (a, b, c) == (lt, lp, sap)

    def test_bit_layout(self):
        # link_type=2 (bits 7:6 = 10), priority=1 (bits 5:4 = 01), sap=5 (bits 3:0 = 0101)
        val = _pack_hard_link_byte(2, 1, 5)
        assert val == 0b10_01_0101


# -----------------------------------------------------------------------
# S_BIND_REQUEST (#1) — SAP_ID|RANK packed in one byte, SERVICE_TYPE 2 bytes
# -----------------------------------------------------------------------

class TestBindRequest:
    def test_roundtrip(self):
        raw = encode_bind_request(sap_id=9, rank=7, transmission_mode=1,
                                  delivery_confirmation=2, delivery_order=True,
                                  extended=True, min_retransmissions=3)
        ptype, payload, _ = decode_s_primitive(raw)
        assert ptype == SPrimitiveType.S_BIND_REQUEST
        d = decode_bind_request(payload)
        assert d['sap_id'] == 9
        assert d['rank'] == 7
        st = d['service_type']
        assert st['transmission_mode'] == 1
        assert st['delivery_confirmation'] == 2
        assert st['delivery_order'] is True
        assert st['extended'] is True
        assert st['min_retransmissions'] == 3

    def test_payload_is_3_bytes(self):
        """Spec: 1 byte packed SAP_ID|RANK + 2 bytes SERVICE_TYPE = 3 bytes."""
        raw = encode_bind_request(sap_id=5, rank=3)
        _, payload, _ = decode_s_primitive(raw)
        assert len(payload) == 3

    def test_nibble_packing(self):
        raw = encode_bind_request(sap_id=0x0A, rank=0x05)
        _, payload, _ = decode_s_primitive(raw)
        assert payload[0] == 0xA5  # upper nibble=A, lower nibble=5

    def test_auto_dispatch(self):
        raw = encode_bind_request(sap_id=3, rank=1)
        ptype, d, _ = decode_primitive_auto(raw)
        assert ptype == SPrimitiveType.S_BIND_REQUEST
        assert d['sap_id'] == 3


# -----------------------------------------------------------------------
# S_BIND_ACCEPTED (#2) — SAP_ID in upper nibble, MTU 2 bytes LE
# -----------------------------------------------------------------------

class TestBindAccepted:
    def test_roundtrip(self):
        raw = encode_bind_accepted(sap_id=12, mtu=4096)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_bind_accepted(payload)
        assert d['sap_id'] == 12
        assert d['mtu'] == 4096

    def test_payload_is_3_bytes(self):
        raw = encode_bind_accepted(sap_id=1, mtu=2048)
        _, payload, _ = decode_s_primitive(raw)
        assert len(payload) == 3

    def test_sap_upper_nibble(self):
        raw = encode_bind_accepted(sap_id=0x0F, mtu=0)
        _, payload, _ = decode_s_primitive(raw)
        assert (payload[0] >> 4) & 0x0F == 0x0F
        assert payload[0] & 0x0F == 0  # lower nibble NOT_USED


# -----------------------------------------------------------------------
# S_HARD_LINK_ESTABLISH (#3) — packed byte + 4-byte address
# -----------------------------------------------------------------------

class TestHardLinkEstablish:
    def test_roundtrip(self):
        raw = encode_hard_link_establish(link_type=2, link_priority=1,
                                         remote_sap=5, remote_node=0x1234)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_establish(payload)
        assert d['link_type'] == 2
        assert d['link_priority'] == 1
        assert d['remote_sap'] == 5
        assert d['remote_node'] == 0x1234

    def test_no_requesting_sap(self):
        """requesting_sap field removed from S_PRIMITIVE per spec."""
        raw = encode_hard_link_establish(2, 1, 5, 0x100)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_establish(payload)
        assert 'requesting_sap' not in d

    def test_payload_size(self):
        """1 packed byte + 4 address = 5 bytes."""
        raw = encode_hard_link_establish(0, 0, 0, 0)
        _, payload, _ = decode_s_primitive(raw)
        assert len(payload) == 5


# -----------------------------------------------------------------------
# S_HARD_LINK_TERMINATE (#4) — only remote_node_address
# -----------------------------------------------------------------------

class TestHardLinkTerminate:
    def test_roundtrip(self):
        raw = encode_hard_link_terminate(remote_node=0xABCD)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_terminate(payload)
        assert d['remote_node'] == 0xABCD

    def test_only_address(self):
        """Spec: only REMOTE_NODE_ADDRESS, no remote_sap/local_sap/reason."""
        raw = encode_hard_link_terminate(0x100)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_terminate(payload)
        assert set(d.keys()) == {'remote_node'}

    def test_payload_size(self):
        """4 bytes address only."""
        raw = encode_hard_link_terminate(0)
        _, payload, _ = decode_s_primitive(raw)
        assert len(payload) == 4


# -----------------------------------------------------------------------
# S_HARD_LINK_ESTABLISHED (#5) — remote_node_status + packed + address
# -----------------------------------------------------------------------

class TestHardLinkEstablished:
    def test_roundtrip(self):
        raw = encode_hard_link_established(
            remote_node_status=1, link_type=2, link_priority=3,
            remote_sap=4, remote_node=0x5678)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_established(payload)
        assert d['remote_node_status'] == 1
        assert d['link_type'] == 2
        assert d['link_priority'] == 3
        assert d['remote_sap'] == 4
        assert d['remote_node'] == 0x5678

    def test_has_remote_node_status(self):
        raw = encode_hard_link_established(0, 0, 0, 0, 0)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_established(payload)
        assert 'remote_node_status' in d

    def test_payload_size(self):
        """1 status + 1 packed + 4 address = 6 bytes."""
        raw = encode_hard_link_established(0, 0, 0, 0, 0)
        _, payload, _ = decode_s_primitive(raw)
        assert len(payload) == 6


# -----------------------------------------------------------------------
# S_HARD_LINK_REJECTED (#6) — reason + packed + address
# -----------------------------------------------------------------------

class TestHardLinkRejected:
    def test_roundtrip(self):
        raw = encode_hard_link_rejected(reason=3, link_type=1, link_priority=2,
                                        remote_sap=6, remote_node=0x9ABC)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_rejected(payload)
        assert d['reason'] == 3
        assert d['link_type'] == 1
        assert d['link_priority'] == 2
        assert d['remote_sap'] == 6
        assert d['remote_node'] == 0x9ABC

    def test_payload_size(self):
        """1 reason + 1 packed + 4 address = 6 bytes."""
        raw = encode_hard_link_rejected(0, 0, 0, 0, 0)
        _, payload, _ = decode_s_primitive(raw)
        assert len(payload) == 6


# -----------------------------------------------------------------------
# S_HARD_LINK_TERMINATED (#7) — same layout as rejected
# -----------------------------------------------------------------------

class TestHardLinkTerminated:
    def test_roundtrip(self):
        raw = encode_hard_link_terminated(reason=2, link_type=1, link_priority=0,
                                          remote_sap=3, remote_node=0xDEAD)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_terminated(payload)
        assert d['reason'] == 2
        assert d['link_type'] == 1
        assert d['link_priority'] == 0
        assert d['remote_sap'] == 3
        assert d['remote_node'] == 0xDEAD


# -----------------------------------------------------------------------
# S_HARD_LINK_INDICATION (#8) — remote_node_status + packed + address
# -----------------------------------------------------------------------

class TestHardLinkIndication:
    def test_roundtrip(self):
        raw = encode_hard_link_indication(
            remote_node_status=2, link_type=1, link_priority=0,
            remote_sap=3, remote_node=0x400)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_indication(payload)
        assert d['remote_node_status'] == 2
        assert d['link_type'] == 1
        assert d['remote_sap'] == 3
        assert d['remote_node'] == 0x400

    def test_has_remote_node_status(self):
        raw = encode_hard_link_indication(0, 0, 0, 0, 0)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_indication(payload)
        assert 'remote_node_status' in d


# -----------------------------------------------------------------------
# S_HARD_LINK_ACCEPT (#9) — packed + address
# -----------------------------------------------------------------------

class TestHardLinkAccept:
    def test_roundtrip(self):
        raw = encode_hard_link_accept(link_type=2, link_priority=1,
                                      remote_sap=7, remote_node=0xBEEF)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_accept(payload)
        assert d['link_type'] == 2
        assert d['link_priority'] == 1
        assert d['remote_sap'] == 7
        assert d['remote_node'] == 0xBEEF

    def test_no_requesting_sap(self):
        raw = encode_hard_link_accept(0, 0, 0, 0)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_accept(payload)
        assert 'requesting_sap' not in d

    def test_payload_size(self):
        """1 packed + 4 address = 5 bytes."""
        raw = encode_hard_link_accept(0, 0, 0, 0)
        _, payload, _ = decode_s_primitive(raw)
        assert len(payload) == 5


# -----------------------------------------------------------------------
# S_HARD_LINK_REJECT (#10) — reason + packed + address
# -----------------------------------------------------------------------

class TestHardLinkReject:
    def test_roundtrip(self):
        raw = encode_hard_link_reject(reason=5, link_type=0, link_priority=3,
                                      remote_sap=11, remote_node=0xCAFE)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_hard_link_reject(payload)
        assert d['reason'] == 5
        assert d['link_type'] == 0
        assert d['link_priority'] == 3
        assert d['remote_sap'] == 11
        assert d['remote_node'] == 0xCAFE


# -----------------------------------------------------------------------
# S_UNIDATA_REQUEST (#11) — priority|dest_sap packed, src_addr added
# -----------------------------------------------------------------------

class TestUnidataRequest:
    def test_roundtrip(self):
        dm = encode_delivery_mode(tx_mode=1, delivery_confirm=1)[0]
        raw = encode_unidata_request(
            priority=7, dest_sap=3, dest_addr=0x100,
            delivery_mode_byte=dm, ttl=300, updu=b'hello', src_addr=0x200)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_request(payload)
        assert d['priority'] == 7
        assert d['dest_sap'] == 3
        assert d['dest_addr'] == 0x100
        assert d['src_addr'] == 0x200
        assert d['ttl'] == 300
        assert d['updu'] == b'hello'

    def test_priority_dest_sap_packed(self):
        dm = encode_delivery_mode(0)[0]
        raw = encode_unidata_request(priority=0x0A, dest_sap=0x05,
                                     dest_addr=0, delivery_mode_byte=dm,
                                     ttl=0, updu=b'')
        _, payload, _ = decode_s_primitive(raw)
        assert payload[0] == 0xA5  # upper=priority, lower=dest_sap

    def test_has_src_addr(self):
        dm = encode_delivery_mode(0)[0]
        raw = encode_unidata_request(priority=0, dest_sap=0, dest_addr=0,
                                     delivery_mode_byte=dm, ttl=0, updu=b'',
                                     src_addr=0x999)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_request(payload)
        assert d['src_addr'] == 0x999

    def test_empty_updu(self):
        dm = encode_delivery_mode(0)[0]
        raw = encode_unidata_request(priority=0, dest_sap=0, dest_addr=0,
                                     delivery_mode_byte=dm, ttl=0, updu=b'')
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_request(payload)
        assert d['updu'] == b''


# -----------------------------------------------------------------------
# S_UNIDATA_INDICATION (#12) — conditional error fields
# -----------------------------------------------------------------------

class TestUnidataIndication:
    def test_roundtrip_without_errors(self):
        raw = encode_unidata_indication(
            priority=3, dest_sap=2, dest_addr=0x100,
            tx_mode=0, src_sap=4, src_addr=0x200, updu=b'data')
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_indication(payload)
        assert d['priority'] == 3
        assert d['dest_sap'] == 2
        assert d['src_sap'] == 4
        assert d['updu'] == b'data'
        assert 'blocks_in_error' not in d

    def test_roundtrip_with_errors(self):
        raw = encode_unidata_indication(
            priority=1, dest_sap=0, dest_addr=0, tx_mode=1,
            src_sap=0, src_addr=0, updu=b'x',
            blocks_in_error=[10, 20, 30], non_received_blocks=[40, 50])
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_indication(payload)
        assert d['blocks_in_error'] == [10, 20, 30]
        assert d['non_received_blocks'] == [40, 50]

    def test_empty_error_lists(self):
        raw = encode_unidata_indication(
            priority=0, dest_sap=0, dest_addr=0, tx_mode=0,
            src_sap=0, src_addr=0, updu=b'',
            blocks_in_error=[], non_received_blocks=[])
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_indication(payload)
        assert d['blocks_in_error'] == []
        assert d['non_received_blocks'] == []

    def test_priority_dest_sap_packed(self):
        raw = encode_unidata_indication(
            priority=0x0C, dest_sap=0x03, dest_addr=0,
            tx_mode=0, src_sap=0, src_addr=0, updu=b'')
        _, payload, _ = decode_s_primitive(raw)
        assert payload[0] == 0xC3


# -----------------------------------------------------------------------
# S_UNIDATA_REQUEST_CONFIRM/REJECTED (#13) — src_addr added
# -----------------------------------------------------------------------

class TestUnidataRequestConfirm:
    def test_roundtrip(self):
        raw = encode_unidata_request_confirm(
            dest_sap=5, dest_addr=0x100, src_sap=3,
            updu_frag=b'\x01\x02', src_addr=0x200)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_request_confirm(payload)
        assert d['dest_sap'] == 5
        assert d['dest_addr'] == 0x100
        assert d['src_sap'] == 3
        assert d['src_addr'] == 0x200
        assert d['updu_frag'] == b'\x01\x02'


class TestUnidataRequestRejected:
    def test_roundtrip(self):
        raw = encode_unidata_request_rejected(
            dest_sap=5, dest_addr=0x100, src_sap=3, reason=4,
            updu_frag=b'\xAB', src_addr=0x300)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_unidata_request_rejected(payload)
        assert d['dest_sap'] == 5
        assert d['dest_addr'] == 0x100
        assert d['src_sap'] == 3
        assert d['src_addr'] == 0x300
        assert d['reason'] == 4
        assert d['updu_frag'] == b'\xAB'


# -----------------------------------------------------------------------
# S_EXPEDITED_UNIDATA_REQUEST (#14) — no PRIORITY field
# -----------------------------------------------------------------------

class TestExpeditedUnidataRequest:
    def test_no_priority(self):
        dm = encode_delivery_mode(tx_mode=2)[0]
        raw = encode_expedited_unidata_request(
            dest_sap=5, dest_addr=0x100,
            delivery_mode_byte=dm, ttl=60, updu=b'exp', src_addr=0x300)
        _, payload, _ = decode_s_primitive(raw)
        d = decode_expedited_unidata_request(payload)
        assert 'priority' not in d
        assert d['dest_sap'] == 5
        assert d['dest_addr'] == 0x100
        assert d['src_addr'] == 0x300
        assert d['updu'] == b'exp'

    def test_dest_sap_upper_nibble(self):
        dm = encode_delivery_mode(0)[0]
        raw = encode_expedited_unidata_request(
            dest_sap=0x0A, dest_addr=0, delivery_mode_byte=dm,
            ttl=0, updu=b'')
        _, payload, _ = decode_s_primitive(raw)
        assert (payload[0] >> 4) & 0x0F == 0x0A
        assert payload[0] & 0x0F == 0  # NOT_USED lower nibble


# -----------------------------------------------------------------------
# S_EXPEDITED_UNIDATA_INDICATION — same layout as unidata_indication
# -----------------------------------------------------------------------

class TestExpeditedUnidataIndication:
    def test_roundtrip(self):
        raw = encode_expedited_unidata_indication(
            priority=5, dest_sap=1, dest_addr=0x100,
            tx_mode=2, src_sap=3, src_addr=0x200, updu=b'fast')
        _, payload, _ = decode_s_primitive(raw)
        d = decode_expedited_unidata_indication(payload)
        assert d['priority'] == 5
        assert d['dest_sap'] == 1
        assert d['src_sap'] == 3
        assert d['updu'] == b'fast'


# -----------------------------------------------------------------------
# Expedited confirm/rejected — same as unidata_request_confirm/rejected
# -----------------------------------------------------------------------

class TestExpeditedConfirmRejected:
    def test_confirm_roundtrip(self):
        raw = encode_expedited_unidata_request_confirm(
            dest_sap=2, dest_addr=0x50, src_sap=1, updu_frag=b'\xFF')
        _, payload, _ = decode_s_primitive(raw)
        d = decode_expedited_unidata_request_confirm(payload)
        assert d['dest_sap'] == 2
        assert d['updu_frag'] == b'\xFF'

    def test_rejected_roundtrip(self):
        raw = encode_expedited_unidata_request_rejected(
            dest_sap=2, dest_addr=0x50, src_sap=1, reason=3, updu_frag=b'\xEE')
        _, payload, _ = decode_s_primitive(raw)
        d = decode_expedited_unidata_request_rejected(payload)
        assert d['reason'] == 3


# -----------------------------------------------------------------------
# Simple primitives (no structural change but verify they still work)
# -----------------------------------------------------------------------

class TestSimplePrimitives:
    def test_bind_rejected(self):
        raw = encode_bind_rejected(reason=3)
        pt, d, _ = decode_primitive_auto(raw)
        assert pt == SPrimitiveType.S_BIND_REJECTED
        assert d['reason'] == 3

    def test_unbind_request(self):
        raw = encode_unbind_request()
        pt, d, _ = decode_primitive_auto(raw)
        assert pt == SPrimitiveType.S_UNBIND_REQUEST
        assert d == {}

    def test_unbind_indication(self):
        raw = encode_unbind_indication(reason=4)
        pt, d, _ = decode_primitive_auto(raw)
        assert d['reason'] == 4

    def test_subnet_availability(self):
        raw = encode_subnet_availability(status=1, reason=2)
        pt, d, _ = decode_primitive_auto(raw)
        assert d['status'] == 1 and d['reason'] == 2

    def test_keep_alive(self):
        raw = encode_keep_alive()
        pt, d, _ = decode_primitive_auto(raw)
        assert pt == SPrimitiveType.S_KEEP_ALIVE and d == {}

    def test_data_flow_on_off(self):
        for fn, expected_type in [(encode_data_flow_on, SPrimitiveType.S_DATA_FLOW_ON),
                                  (encode_data_flow_off, SPrimitiveType.S_DATA_FLOW_OFF)]:
            raw = fn()
            pt, d, _ = decode_primitive_auto(raw)
            assert pt == expected_type and d == {}

    def test_management_msg(self):
        raw = encode_management_msg_request(b'\x01\x02\x03')
        pt, d, _ = decode_primitive_auto(raw)
        assert d['message'] == b'\x01\x02\x03'


# -----------------------------------------------------------------------
# Full auto-dispatch for every type
# -----------------------------------------------------------------------

class TestAutoDispatch:
    def test_all_types_dispatch(self):
        """Encode every primitive type, decode via auto-dispatch, verify no crash."""
        dm = encode_delivery_mode(0)[0]
        primitives = [
            encode_bind_request(1, 0),
            encode_unbind_request(),
            encode_bind_accepted(1),
            encode_bind_rejected(0),
            encode_unbind_indication(0),
            encode_hard_link_establish(0, 0, 0, 0),
            encode_hard_link_terminate(0),
            encode_hard_link_established(0, 0, 0, 0, 0),
            encode_hard_link_rejected(0, 0, 0, 0, 0),
            encode_hard_link_terminated(0, 0, 0, 0, 0),
            encode_hard_link_indication(0, 0, 0, 0, 0),
            encode_hard_link_accept(0, 0, 0, 0),
            encode_hard_link_reject(0, 0, 0, 0, 0),
            encode_subnet_availability(0),
            encode_data_flow_on(),
            encode_data_flow_off(),
            encode_keep_alive(),
            encode_management_msg_request(b''),
            encode_unidata_request(0, 0, 0, dm, 0, b''),
            encode_unidata_indication(0, 0, 0, 0, 0, 0, b''),
            encode_unidata_request_confirm(0, 0, 0, b''),
            encode_unidata_request_rejected(0, 0, 0, 0, b''),
            encode_expedited_unidata_request(0, 0, dm, 0, b''),
            encode_expedited_unidata_request_confirm(0, 0, 0, b''),
            encode_expedited_unidata_request_rejected(0, 0, 0, 0, b''),
            encode_expedited_unidata_indication(0, 0, 0, 0, 0, 0, b''),
        ]
        for raw in primitives:
            ptype, d, consumed = decode_primitive_auto(raw)
            assert isinstance(d, dict)
            assert consumed == len(raw)
