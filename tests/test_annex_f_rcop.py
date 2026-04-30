"""Tests for rcop.py, bftp.py, cftp.py — RCOP/UDOP codecs, BFTP/FRAP/CFTP clients."""

import gzip
import struct

import pytest

from src.annex_f.rcop import (
    RcopPDU,
    encode_rcop_pdu,
    decode_rcop_pdu,
    RcopClient,
    UdopClient,
    RCOP_HEADER_SIZE,
    RCOP_MAX_APP_DATA,
    APP_ID_BFTP,
    APP_ID_FRAP,
    APP_ID_FRAPV2,
    _RcopReassemblyContext,
)
from src.annex_f.bftp import (
    BftpClient,
    FrapClient,
    FrapV2Client,
    _encode_bftp,
    _decode_bftp,
    _build_bftp_header,
)
from src.annex_f.cftp import (
    CftpClient,
    CftpMessage,
    CFTP_MSG_ACK,
    _encode_rcopv1,
    _decode_rcopv1,
    _encode_bftpv1,
    _decode_bftpv1,
    _encode_cftp_message,
    _decode_cftp_message,
    RcopV1PDU,
)
from src.stypes import SisUnidataIndication

from tests.annex_f_helpers import MockNode, deliver


# ===========================================================================
# RcopPDU codec
# ===========================================================================

class TestRcopPDU:
    def test_valid_construction(self):
        pdu = RcopPDU(0, 0, 0, 0, b"")
        assert pdu.connection_id == 0

    def test_boundary_values(self):
        pdu = RcopPDU(15, 255, 65535, 65535, b"data")
        assert pdu.connection_id == 15
        assert pdu.updu_id == 255

    def test_invalid_connection_id(self):
        with pytest.raises(ValueError):
            RcopPDU(16, 0, 0, 0, b"")

    def test_invalid_updu_id(self):
        with pytest.raises(ValueError):
            RcopPDU(0, 256, 0, 0, b"")

    def test_encode_decode_roundtrip(self):
        pdu = RcopPDU(5, 100, 300, 0x1002, b"hello")
        raw = encode_rcop_pdu(pdu)
        pdu2 = decode_rcop_pdu(raw)
        assert pdu2.connection_id == 5
        assert pdu2.updu_id == 100
        assert pdu2.segment_number == 300
        assert pdu2.app_id == 0x1002
        assert pdu2.app_data == b"hello"

    def test_decode_truncated(self):
        with pytest.raises(ValueError):
            decode_rcop_pdu(b"\x00\x01")

    def test_header_size(self):
        pdu = RcopPDU(0, 0, 0, 0, b"")
        raw = encode_rcop_pdu(pdu)
        assert len(raw) == RCOP_HEADER_SIZE


# ===========================================================================
# RCOP reassembly
# ===========================================================================

class TestRcopReassembly:
    def test_single_segment(self):
        ctx = _RcopReassemblyContext()
        pdu = RcopPDU(0, 1, 0, 0x1002, b"small")
        result = ctx.feed(99, 6, pdu)
        assert result == (0x1002, b"small")

    def test_multi_segment(self):
        ctx = _RcopReassemblyContext()
        big = b"A" * RCOP_MAX_APP_DATA
        # seg 0: full
        assert ctx.feed(99, 6, RcopPDU(0, 1, 0, 0x1002, big)) is None
        # seg 1: short (last)
        result = ctx.feed(99, 6, RcopPDU(0, 1, 1, 0x1002, b"tail"))
        assert result is not None
        app_id, data = result
        assert app_id == 0x1002
        assert data == big + b"tail"

    def test_missing_segment(self):
        ctx = _RcopReassemblyContext()
        big = b"A" * RCOP_MAX_APP_DATA
        ctx.feed(99, 6, RcopPDU(0, 1, 0, 0x1002, big))
        # Skip seg 1, send seg 2 (short)
        result = ctx.feed(99, 6, RcopPDU(0, 1, 2, 0x1002, b"x"))
        assert result is None


# ===========================================================================
# RcopClient (SAP 6)
# ===========================================================================

class TestRcopClient:
    def test_send_arq_mode(self):
        node = MockNode()
        c = RcopClient(node)
        c.send(dest_addr=1, app_id=0x1002, data=b"hello")
        assert node.sent[0]["mode"].arq_mode is True
        assert node.sent[0]["mode"].node_delivery_confirm is True

    def test_send_returns_updu_id(self):
        node = MockNode()
        c = RcopClient(node)
        uid1 = c.send(dest_addr=1, app_id=0x1002, data=b"a")
        uid2 = c.send(dest_addr=1, app_id=0x1002, data=b"b")
        assert uid1 != uid2

    def test_send_segmentation(self):
        node = MockNode()
        c = RcopClient(node)
        big = b"X" * (RCOP_MAX_APP_DATA + 100)
        c.send(dest_addr=1, app_id=0x1002, data=big)
        assert len(node.sent) == 2  # 1 full + 1 short

    def test_receive_by_app_id(self):
        node = MockNode()
        c = RcopClient(node)
        received = []
        c.register_app_handler(0x1002, lambda *a: received.append(a))
        pdu = encode_rcop_pdu(RcopPDU(0, 1, 0, 0x1002, b"data"))
        ind = SisUnidataIndication(dest_sap=6, src_addr=99, src_sap=6,
                                    priority=0, updu=pdu)
        c.on_unidata_indication(ind)
        assert len(received) == 1
        assert received[0][2] == 0x1002  # app_id
        assert received[0][3] == b"data"  # app_data

    def test_receive_catch_all(self):
        node = MockNode()
        c = RcopClient(node)
        received = []
        c.on_received = lambda *a: received.append(a)
        pdu = encode_rcop_pdu(RcopPDU(0, 1, 0, 0x9999, b"xyz"))
        ind = SisUnidataIndication(dest_sap=6, src_addr=99, src_sap=6,
                                    priority=0, updu=pdu)
        c.on_unidata_indication(ind)
        assert len(received) == 1

    def test_sap_id_is_6(self):
        assert RcopClient.SAP_ID == 6


# ===========================================================================
# UdopClient (SAP 7)
# ===========================================================================

class TestUdopClient:
    def test_send_non_arq(self):
        node = MockNode()
        c = UdopClient(node)
        c.send(dest_addr=1, app_id=0x1002, data=b"hello")
        assert node.sent[0]["mode"].arq_mode is False

    def test_sap_id_is_7(self):
        assert UdopClient.SAP_ID == 7


# ===========================================================================
# BFTP codec
# ===========================================================================

class TestBftpCodec:
    def test_encode_decode_roundtrip(self):
        raw = _encode_bftp("test.txt", b"file data here")
        filename, file_data = _decode_bftp(raw)
        assert filename == "test.txt"
        assert file_data == b"file data here"

    def test_decode_truncated(self):
        with pytest.raises(ValueError):
            _decode_bftp(b"\x00")

    def test_long_filename(self):
        name = "a" * 255
        raw = _encode_bftp(name, b"data")
        fn, _ = _decode_bftp(raw)
        assert fn == name

    def test_filename_too_long(self):
        with pytest.raises(ValueError):
            _encode_bftp("a" * 256, b"data")


class TestBftpClient:
    def test_send_file(self):
        node = MockNode()
        c = BftpClient(node)
        uid = c.send_file(dest_addr=1, filename="test.bin", file_data=b"contents")
        assert isinstance(uid, int)
        assert len(node.sent) >= 1
        # Decode the RCOP PDU to verify APP_ID
        pdu = decode_rcop_pdu(node.sent[0]["updu"])
        assert pdu.app_id == APP_ID_BFTP

    def test_receive_file(self):
        node = MockNode()
        c = BftpClient(node)
        received = []
        c.on_file_received = lambda *a: received.append(a)
        # Build BFTP data wrapped in RCOP PDU
        bftp_data = _encode_bftp("file.txt", b"content")
        pdu = encode_rcop_pdu(RcopPDU(0, 1, 0, APP_ID_BFTP, bftp_data))
        ind = SisUnidataIndication(dest_sap=6, src_addr=99, src_sap=6,
                                    priority=0, updu=pdu)
        c.on_unidata_indication(ind)
        assert len(received) == 1
        assert received[0][2] == "file.txt"
        assert received[0][3] == b"content"


# ===========================================================================
# FRAP / FRAPv2
# ===========================================================================

class TestFrapClient:
    def test_ack_sends_empty_body(self):
        node = MockNode()
        c = FrapClient(node)
        c.ack(dest_addr=1, conn_id=3, updu_id=7)
        pdu = decode_rcop_pdu(node.sent[0]["updu"])
        assert pdu.app_id == APP_ID_FRAP
        assert pdu.app_data == b""

    def test_ack_restores_state(self):
        node = MockNode()
        c = FrapClient(node, connection_id=5)
        orig_uid = c._rcop_updu_id
        c.ack(dest_addr=1, conn_id=3, updu_id=7)
        assert c.connection_id == 5
        assert c._rcop_updu_id == orig_uid


class TestFrapV2Client:
    def test_ack_sends_bftp_header(self):
        node = MockNode()
        c = FrapV2Client(node)
        c.ack(dest_addr=1, filename="f.txt", file_size=1000,
              conn_id=2, updu_id=5)
        pdu = decode_rcop_pdu(node.sent[0]["updu"])
        assert pdu.app_id == APP_ID_FRAPV2
        expected_body = _build_bftp_header("f.txt", 1000)
        assert pdu.app_data == expected_body


# ===========================================================================
# CFTP codecs
# ===========================================================================

class TestCftpCodecs:
    def test_rcopv1_roundtrip(self):
        pdu = RcopV1PDU(3, 10, 500, b"test data")
        raw = _encode_rcopv1(pdu)
        pdu2 = _decode_rcopv1(raw)
        assert pdu2.connection_id == 3
        assert pdu2.updu_id == 10
        assert pdu2.segment_number == 500
        assert pdu2.data == b"test data"

    def test_bftpv1_roundtrip(self):
        raw = _encode_bftpv1("msg001", b"compressed_data")
        fn, data = _decode_bftpv1(raw)
        assert fn == "msg001"
        assert data == b"compressed_data"

    def test_bftpv1_missing_sync(self):
        with pytest.raises(ValueError, match="sincroniza"):
            _decode_bftpv1(b"\x00\x00\x03abc\x00\x00\x00\x03xyz")

    def test_cftp_message_roundtrip(self):
        msg = CftpMessage("msgid123", ["a@a.com", "b@b.com"],
                           b"Subject: test\r\n\r\nBody")
        raw = _encode_cftp_message(msg)
        msg2 = _decode_cftp_message(raw)
        assert msg2.message_id == "msgid123"
        assert msg2.recipients == ["a@a.com", "b@b.com"]
        assert msg2.message == b"Subject: test\r\n\r\nBody"


# ===========================================================================
# CftpClient (SAP 12)
# ===========================================================================

class TestCftpClient:
    def test_send_mail(self):
        node = MockNode()
        c = CftpClient(node)
        uid = c.send_mail(dest_addr=1, message_id="msg001",
                           recipients=["a@a.com"],
                           smtp_message=b"Subject: Hi\r\n\r\nHello")
        assert isinstance(uid, int)
        assert len(node.sent) >= 1
        assert node.sent[0]["sap_id"] == 12

    def test_receive_and_deliver(self):
        node = MockNode()
        c = CftpClient(node, auto_ack=False)
        received = []
        c.on_mail_received = lambda addr, msg: received.append((addr, msg))

        # Build multi-layer payload
        msg = CftpMessage("test001", ["user@test.com"], b"From: x\r\n\r\nBody")
        raw_cftp = _encode_cftp_message(msg)
        compressed = gzip.compress(raw_cftp)
        bftpv1 = _encode_bftpv1("test001", compressed)
        rcopv1 = _encode_rcopv1(RcopV1PDU(0, 1, 0, bftpv1))

        ind = SisUnidataIndication(dest_sap=12, src_addr=99, src_sap=12,
                                    priority=0, updu=rcopv1)
        c.on_unidata_indication(ind)
        assert len(received) == 1
        assert received[0][1].message_id == "test001"

    def test_auto_ack(self):
        node = MockNode()
        c = CftpClient(node, auto_ack=True)

        msg = CftpMessage("test002", ["u@t.com"], b"body")
        raw_cftp = _encode_cftp_message(msg)
        compressed = gzip.compress(raw_cftp)
        bftpv1 = _encode_bftpv1("test002", compressed)
        rcopv1 = _encode_rcopv1(RcopV1PDU(0, 1, 0, bftpv1))

        ind = SisUnidataIndication(dest_sap=12, src_addr=99, src_sap=12,
                                    priority=0, updu=rcopv1)
        c.on_unidata_indication(ind)
        # Should have sent an ACK
        assert len(node.sent) >= 1
        # Verify ACK contains CFTP_MSG_ACK bytes
        ack_raw = node.sent[-1]["updu"]
        ack_pdu = _decode_rcopv1(ack_raw)
        assert ack_pdu.data == CFTP_MSG_ACK

    def test_auto_ack_disabled(self):
        node = MockNode()
        c = CftpClient(node, auto_ack=False)

        msg = CftpMessage("test003", ["u@t.com"], b"body")
        raw_cftp = _encode_cftp_message(msg)
        compressed = gzip.compress(raw_cftp)
        bftpv1 = _encode_bftpv1("test003", compressed)
        rcopv1 = _encode_rcopv1(RcopV1PDU(0, 1, 0, bftpv1))

        ind = SisUnidataIndication(dest_sap=12, src_addr=99, src_sap=12,
                                    priority=0, updu=rcopv1)
        c.on_unidata_indication(ind)
        assert len(node.sent) == 0

    def test_ack_received_callback(self):
        node = MockNode()
        c = CftpClient(node)
        acks = []
        c.on_ack_received = lambda addr: acks.append(addr)

        ack_pdu = _encode_rcopv1(RcopV1PDU(0, 1, 0, CFTP_MSG_ACK))
        ind = SisUnidataIndication(dest_sap=12, src_addr=42, src_sap=12,
                                    priority=0, updu=ack_pdu)
        c.on_unidata_indication(ind)
        assert acks == [42]

    def test_sap_id_is_12(self):
        assert CftpClient.SAP_ID == 12
