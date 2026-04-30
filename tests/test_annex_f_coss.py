"""Tests for src/annex_f/coss.py — COSS encoding modes, DPI2E, flush buffer."""

import time

import pytest

from src.annex_f.coss import (
    CossMode,
    CharacterEncoder,
    CossClient,
    _FlushBuffer,
)

from tests.annex_f_helpers import MockNode, deliver


# ===========================================================================
# CharacterEncoder — OCTET
# ===========================================================================

class TestCharacterEncoderOctet:
    def test_encode_passthrough(self):
        data = b"\xFF\x00\x7F\x80"
        assert CharacterEncoder.encode(data, CossMode.OCTET) == data

    def test_decode_passthrough(self):
        data = b"\xFF\x00\x7F\x80"
        assert CharacterEncoder.decode(data, CossMode.OCTET) == data


# ===========================================================================
# CharacterEncoder — ITA5
# ===========================================================================

class TestCharacterEncoderITA5:
    def test_encode_masks_msb(self):
        result = CharacterEncoder.encode(b"\xFF\x80\x41", CossMode.ITA5)
        assert result == b"\x7F\x00\x41"

    def test_decode_masks_msb(self):
        result = CharacterEncoder.decode(b"\xFF\x80\x41", CossMode.ITA5)
        assert result == b"\x7F\x00\x41"


# ===========================================================================
# CharacterEncoder — LPI2E
# ===========================================================================

class TestCharacterEncoderLPI2E:
    def test_encode_masks_5bit(self):
        # 0xFF -> 0x1F, 0x1F -> 0x1F, 0x20 -> 0x00
        result = CharacterEncoder.encode(b"\xFF\x1F\x20", CossMode.LPI2E)
        assert result == b"\x1F\x1F\x00"

    def test_decode_masks_5bit(self):
        result = CharacterEncoder.decode(b"\xFF\x1F\x20", CossMode.LPI2E)
        assert result == b"\x1F\x1F\x00"


# ===========================================================================
# CharacterEncoder — SIX_BIT
# ===========================================================================

class TestCharacterEncoderSixBit:
    def test_encode_masks_6bit(self):
        result = CharacterEncoder.encode(b"\xFF\x3F\x40", CossMode.SIX_BIT)
        assert result == b"\x3F\x3F\x00"

    def test_decode_masks_6bit(self):
        result = CharacterEncoder.decode(b"\xFF\x3F\x40", CossMode.SIX_BIT)
        assert result == b"\x3F\x3F\x00"


# ===========================================================================
# CharacterEncoder — DPI2E
# ===========================================================================

class TestDPI2E:
    def test_encode_3_chars_2_bytes(self):
        """3 five-bit chars → 2 bytes (one 3-into-2 pair)."""
        chars = bytes([0x01, 0x02, 0x03])
        result = CharacterEncoder.encode(chars, CossMode.DPI2E)
        assert len(result) == 2

    def test_encode_6_chars_4_bytes(self):
        """6 five-bit chars → 4 bytes (two 3-into-2 pairs)."""
        chars = bytes([1, 2, 3, 4, 5, 6])
        result = CharacterEncoder.encode(chars, CossMode.DPI2E)
        assert len(result) == 4

    def test_encode_remainder_1_loose(self):
        """4 chars = 1 triplet (2 bytes) + 1 loose (1 byte) = 3 bytes."""
        chars = bytes([1, 2, 3, 4])
        result = CharacterEncoder.encode(chars, CossMode.DPI2E)
        assert len(result) == 3

    def test_encode_remainder_2_pair(self):
        """5 chars = 1 triplet (2 bytes) + 1 pair (2 bytes) = 4 bytes."""
        chars = bytes([1, 2, 3, 4, 5])
        result = CharacterEncoder.encode(chars, CossMode.DPI2E)
        assert len(result) == 4

    def test_decode_single_byte(self):
        """L=1 → single loose-packed char."""
        encoded = bytes([0x0A])  # 5-bit value = 10
        result = CharacterEncoder.decode(encoded, CossMode.DPI2E)
        assert result == bytes([0x0A])

    def test_decode_empty(self):
        result = CharacterEncoder.decode(b"", CossMode.DPI2E)
        assert result == b""

    @pytest.mark.parametrize("n", range(1, 13))
    def test_roundtrip_various_lengths(self, n):
        """Encode then decode should return original chars (masked to 5 bits)."""
        chars = bytes(i & 0x1F for i in range(n))
        encoded = CharacterEncoder.encode(chars, CossMode.DPI2E)
        decoded = CharacterEncoder.decode(encoded, CossMode.DPI2E)
        assert decoded == chars

    def test_encode_masks_to_5_bits(self):
        """Input chars have high bits → encode masks them."""
        chars = bytes([0xFF, 0xE0, 0x1F])  # should become [0x1F, 0x00, 0x1F]
        encoded = CharacterEncoder.encode(chars, CossMode.DPI2E)
        decoded = CharacterEncoder.decode(encoded, CossMode.DPI2E)
        assert decoded == bytes([0x1F, 0x00, 0x1F])


# ===========================================================================
# _FlushBuffer
# ===========================================================================

class TestFlushBuffer:
    def test_threshold_flush(self):
        flushed = []
        buf = _FlushBuffer(threshold=4, on_flush=lambda d: flushed.append(d))
        buf.feed(b"ABCD")  # exactly threshold
        assert len(flushed) == 1
        assert flushed[0] == b"ABCD"

    def test_crlf_flush(self):
        flushed = []
        buf = _FlushBuffer(threshold=100, flush_on_crlf=True,
                            on_flush=lambda d: flushed.append(d))
        buf.feed(b"abc\r\n")
        assert len(flushed) == 1
        assert flushed[0] == b"abc\r\n"

    def test_no_crlf_flush_when_disabled(self):
        flushed = []
        buf = _FlushBuffer(threshold=100, flush_on_crlf=False,
                            on_flush=lambda d: flushed.append(d))
        buf.feed(b"abc\r\n")
        assert len(flushed) == 0

    def test_timeout_flush(self):
        flushed = []
        buf = _FlushBuffer(threshold=100, flush_timeout_s=0.5,
                            on_flush=lambda d: flushed.append(d))
        buf.feed(b"x")
        # Simulate time passing by setting _last_rx in the past
        buf._last_rx = time.monotonic() - 1.0
        buf.tick()
        assert len(flushed) == 1
        assert flushed[0] == b"x"

    def test_flush_now(self):
        flushed = []
        buf = _FlushBuffer(threshold=100,
                            on_flush=lambda d: flushed.append(d))
        buf.feed(b"abc")  # under threshold
        assert len(flushed) == 0
        buf.flush_now()
        assert len(flushed) == 1

    def test_flush_now_empty_noop(self):
        flushed = []
        buf = _FlushBuffer(on_flush=lambda d: flushed.append(d))
        buf.flush_now()
        assert len(flushed) == 0


# ===========================================================================
# CossClient (SAP 1)
# ===========================================================================

class TestCossClient:
    def test_feed_bytes_and_flush_sends(self):
        node = MockNode()
        c = CossClient(node, dest_addr=42, mode=CossMode.OCTET,
                         flush_threshold=5)
        c.feed_bytes(b"ABCDE")
        assert len(node.sent) == 1
        assert node.sent[0]["updu"] == b"ABCDE"
        assert node.sent[0]["dest_addr"] == 42

    def test_manual_flush(self):
        node = MockNode()
        c = CossClient(node, dest_addr=1, mode=CossMode.OCTET,
                         flush_threshold=100)
        c.feed_bytes(b"short")
        assert len(node.sent) == 0
        c.flush()
        assert len(node.sent) == 1

    def test_receive_decodes_and_delivers(self):
        node = MockNode()
        c = CossClient(node, mode=CossMode.ITA5)
        received = []
        c.on_serial_output = lambda addr, data: received.append((addr, data))
        # ITA5 encoded: MSB masked
        deliver(c, src_addr=10, data=b"\xC1\xC2")  # 0x41, 0x42 after mask
        assert len(received) == 1
        assert received[0][0] == 10
        assert received[0][1] == b"\x41\x42"

    def test_arq_mode(self):
        node = MockNode()
        c = CossClient(node, dest_addr=1, arq=True, flush_threshold=1)
        c.feed_bytes(b"x")
        assert node.sent[0]["mode"].arq_mode is True

    def test_non_arq_mode(self):
        node = MockNode()
        c = CossClient(node, dest_addr=1, arq=False, flush_threshold=1)
        c.feed_bytes(b"x")
        assert node.sent[0]["mode"].arq_mode is False

    def test_sap_id_is_1(self):
        assert CossClient.SAP_ID == 1

    def test_encoding_applied_on_send(self):
        """ITA5 mode should mask MSB on outgoing data."""
        node = MockNode()
        c = CossClient(node, dest_addr=1, mode=CossMode.ITA5,
                         flush_threshold=3)
        c.feed_bytes(b"\xFF\x80\x41")
        assert node.sent[0]["updu"] == b"\x7F\x00\x41"
