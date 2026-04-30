"""Tests for src/annex_f/updu.py — UPDU header codec, segmentation, reassembly."""

import pytest

from src.annex_f.updu import (
    UPDU_HEADER_SIZE,
    UPDUHeader,
    encode_updu,
    decode_updu,
    segment_updu,
    ReassemblyContext,
    DEFAULT_MTU,
    MAX_SEGMENT_DATA,
)


# ---------------------------------------------------------------------------
# UPDUHeader validation
# ---------------------------------------------------------------------------

class TestUPDUHeader:
    def test_valid_min_values(self):
        h = UPDUHeader(0, 0, 0)
        assert h.connection_id == 0

    def test_valid_max_values(self):
        h = UPDUHeader(15, 4095, 255)
        assert h.connection_id == 15
        assert h.updu_id == 4095
        assert h.segment_number == 255

    def test_invalid_connection_id(self):
        with pytest.raises(ValueError):
            UPDUHeader(16, 0, 0)

    def test_invalid_updu_id(self):
        with pytest.raises(ValueError):
            UPDUHeader(0, 4096, 0)

    def test_invalid_segment_number(self):
        with pytest.raises(ValueError):
            UPDUHeader(0, 0, 256)

    def test_negative_values(self):
        with pytest.raises(ValueError):
            UPDUHeader(-1, 0, 0)


# ---------------------------------------------------------------------------
# encode / decode roundtrip
# ---------------------------------------------------------------------------

class TestEncodeDecodeRoundtrip:
    def test_roundtrip_no_data(self):
        h = UPDUHeader(3, 1000, 5)
        raw = encode_updu(h, b"")
        h2, data = decode_updu(raw)
        assert h2.connection_id == 3
        assert h2.updu_id == 1000
        assert h2.segment_number == 5
        assert data == b""

    def test_roundtrip_with_data(self):
        h = UPDUHeader(15, 4095, 255)
        payload = b"\x01\x02\x03\xAA\xBB"
        raw = encode_updu(h, payload)
        h2, data = decode_updu(raw)
        assert h2.connection_id == 15
        assert h2.updu_id == 4095
        assert h2.segment_number == 255
        assert data == payload

    def test_header_size_is_4(self):
        raw = encode_updu(UPDUHeader(0, 0, 0), b"")
        assert len(raw) == UPDU_HEADER_SIZE

    def test_decode_too_short(self):
        with pytest.raises(ValueError, match="muito curto"):
            decode_updu(b"\x00\x01")


# ---------------------------------------------------------------------------
# segment_updu
# ---------------------------------------------------------------------------

class TestSegmentUpdu:
    def test_single_segment_small_data(self):
        segs = segment_updu(0, 1, b"hello")
        assert len(segs) == 1
        h, data = decode_updu(segs[0])
        assert data == b"hello"
        assert h.segment_number == 0

    def test_multiple_segments(self):
        big_data = bytes(range(256)) * 20  # 5120 bytes
        segs = segment_updu(0, 1, big_data)
        assert len(segs) >= 3
        # Verify sequential segment numbers
        for i, seg in enumerate(segs):
            h, _ = decode_updu(seg)
            assert h.segment_number == i

    def test_empty_data_one_segment(self):
        segs = segment_updu(0, 1, b"")
        assert len(segs) == 1
        h, data = decode_updu(segs[0])
        assert data == b""

    def test_small_mtu_raises(self):
        with pytest.raises(ValueError, match="muito pequeno"):
            segment_updu(0, 1, b"data", mtu=UPDU_HEADER_SIZE)

    @pytest.mark.parametrize("mtu", [5, 10, 100, 512])
    def test_all_segments_fit_mtu(self, mtu):
        data = bytes(200)
        segs = segment_updu(0, 1, data, mtu=mtu)
        for seg in segs:
            assert len(seg) <= mtu

    def test_reassembled_data_matches_original(self):
        data = bytes(range(256)) * 10  # 2560 bytes
        segs = segment_updu(3, 42, data)
        reassembled = b""
        for seg in segs:
            _, chunk = decode_updu(seg)
            reassembled += chunk
        assert reassembled == data


# ---------------------------------------------------------------------------
# ReassemblyContext
# ---------------------------------------------------------------------------

class TestReassemblyContext:
    def test_single_small_segment(self):
        ctx = ReassemblyContext()
        h = UPDUHeader(0, 1, 0)
        result = ctx.feed(99, h, b"hello")
        assert result == b"hello"

    def test_multi_segment_reassembly(self):
        ctx = ReassemblyContext(mtu=10)  # max_data = 6
        max_data = 10 - UPDU_HEADER_SIZE  # 6
        # seg 0: full
        assert ctx.feed(99, UPDUHeader(0, 1, 0), b"A" * max_data) is None
        # seg 1: full
        assert ctx.feed(99, UPDUHeader(0, 1, 1), b"B" * max_data) is None
        # seg 2: short (last)
        result = ctx.feed(99, UPDUHeader(0, 1, 2), b"C" * 3)
        assert result == b"A" * max_data + b"B" * max_data + b"C" * 3

    def test_missing_segment_returns_none(self):
        ctx = ReassemblyContext(mtu=10)
        max_data = 10 - UPDU_HEADER_SIZE
        ctx.feed(99, UPDUHeader(0, 1, 0), b"A" * max_data)
        # Skip seg 1, send seg 2 (short = last)
        result = ctx.feed(99, UPDUHeader(0, 1, 2), b"C")
        assert result is None  # gap at seg 1

    def test_clear_resets_all(self):
        ctx = ReassemblyContext(mtu=10)
        max_data = 10 - UPDU_HEADER_SIZE
        ctx.feed(99, UPDUHeader(0, 1, 0), b"A" * max_data)
        ctx.clear()
        # Now feed same key — treated as new
        result = ctx.feed(99, UPDUHeader(0, 1, 0), b"new")
        assert result == b"new"
