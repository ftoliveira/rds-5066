"""Tests for DTS correction plan — STANAG 5066 Annex C conformance fixes.

Covers issues #1-#13, #15 from the correction plan.
"""

from __future__ import annotations

import pytest

from src.eow import (
    DRC_RATE_TO_BPS,
    DRCDataRate,
    build_eow_drc,
    parse_eow,
)
from src.non_arq import NonArqEngine, NonArqSegmenter
from src.dpdu_frame import (
    build_management,
    build_non_arq,
    build_expedited_non_arq,
    build_warning,
    decode_dpdu,
    dpdu_set_address,
    encode_dpdu,
)
from src.dts_state import (
    DTSState,
    DTSStateMachine,
    WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED,
    WARNING_REASON_INVALID_DPDU_FOR_STATE,
)
from src.management import ManagementEngine
from src.expedited_arq import ExpeditedArqEngine
from src.arq import (
    ArqEngine,
    RxFrameStatus,
    _build_selective_ack_bitmap,
    _RxSlot,
    _seq_add,
)
from src.stypes import DPDU, DPDUType


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _addr(dest=1, src=2):
    return dpdu_set_address(destination=dest, source=src)


class _FakeModemConfig:
    data_rate_bps = 1200


class _FakeModem:
    """Minimal modem stub for NonArqEngine."""

    config = _FakeModemConfig()

    def __init__(self):
        self._tx: list[bytes] = []
        self._rx: list[bytes] = []

    def modem_tx_burst(self, frames: list[bytes]) -> int:
        self._tx.extend(frames)
        return len(frames)

    def modem_tx_dpdu(self, buf: bytes, length=None) -> int:
        self._tx.append(buf)
        return len(buf)

    def modem_rx_read_frame(self):
        return self._rx.pop(0) if self._rx else None


# =======================================================================
# Issue #1 — DRC Data Rate Codes (eow.py)
# =======================================================================

class TestDRCDataRateCodes:
    """Table C-5: 12 data rates including 3600 bps at code 7."""

    EXPECTED = {
        0: 75, 1: 150, 2: 300, 3: 600, 4: 1200,
        5: 2400, 6: 3200, 7: 3600, 8: 4800, 9: 6400, 10: 8000, 11: 9600,
    }

    def test_rate_table_has_12_entries(self):
        assert len(DRC_RATE_TO_BPS) == 12

    def test_rate_table_values(self):
        assert DRC_RATE_TO_BPS == self.EXPECTED

    def test_enum_3600_exists(self):
        assert DRCDataRate.BPS_3600 == 7

    def test_enum_shifted_values(self):
        assert DRCDataRate.BPS_4800 == 8
        assert DRCDataRate.BPS_6400 == 9
        assert DRCDataRate.BPS_8000 == 10
        assert DRCDataRate.BPS_9600 == 11

    @pytest.mark.parametrize("code,bps", list(EXPECTED.items()))
    def test_drc_roundtrip(self, code, bps):
        eow = build_eow_drc(code)
        parsed = parse_eow(eow)
        assert parsed.drc_request is not None
        assert parsed.drc_request.data_rate_code == code
        assert parsed.drc_request.data_rate_bps == bps


# =======================================================================
# Issue #2 — Non-ARQ ID space separation (non_arq.py)
# =======================================================================

class TestNonArqIdSpaces:
    """C.3.10§(6): Type 7 and Type 8 use separate C_PDU ID number spaces."""

    def test_separate_counters(self):
        eng = NonArqEngine(1, _FakeModem(), max_user_data_bytes=100)
        id7a = eng.queue_cpdu(DPDUType.NON_ARQ, 2, b"a")
        id7b = eng.queue_cpdu(DPDUType.NON_ARQ, 2, b"b")
        id8a = eng.queue_cpdu(DPDUType.EXPEDITED_NON_ARQ, 2, b"c")
        id8b = eng.queue_cpdu(DPDUType.EXPEDITED_NON_ARQ, 2, b"d")
        assert id7a == 0 and id7b == 1
        assert id8a == 0 and id8b == 1

    def test_explicit_cpdu_id_overrides(self):
        eng = NonArqEngine(1, _FakeModem(), max_user_data_bytes=100)
        assert eng.queue_cpdu(DPDUType.NON_ARQ, 2, b"a", cpdu_id=99) == 99
        # Internal counter should not have been consumed
        assert eng.queue_cpdu(DPDUType.NON_ARQ, 2, b"b") == 0

    def test_interleaved_types_independent(self):
        eng = NonArqEngine(1, _FakeModem(), max_user_data_bytes=100)
        assert eng.queue_cpdu(DPDUType.NON_ARQ, 2, b"a") == 0
        assert eng.queue_cpdu(DPDUType.EXPEDITED_NON_ARQ, 2, b"b") == 0
        assert eng.queue_cpdu(DPDUType.NON_ARQ, 2, b"c") == 1
        assert eng.queue_cpdu(DPDUType.EXPEDITED_NON_ARQ, 2, b"d") == 1


# =======================================================================
# Issue #3 — EOW type validation removed for Non-ARQ (dpdu_frame.py)
# =======================================================================

class TestNonArqEowFreedom:
    """Non-ARQ D_PDUs can carry any EOW type (no restriction to type 3)."""

    def test_eow_zero_accepted(self):
        dpdu = build_non_arq(0x000, 0, _addr(), b"data", cpdu_size=4)
        raw = encode_dpdu(dpdu)
        dec = decode_dpdu(raw)
        assert dec.eow == 0x000

    def test_eow_drc_request_accepted(self):
        eow = build_eow_drc(DRCDataRate.BPS_2400)
        dpdu = build_non_arq(eow, 0, _addr(), b"data", cpdu_size=4)
        raw = encode_dpdu(dpdu)
        dec = decode_dpdu(raw)
        assert dec.eow == eow

    def test_expedited_non_arq_eow_freedom(self):
        dpdu = build_expedited_non_arq(0x041, 0, _addr(), b"data", cpdu_size=4)
        raw = encode_dpdu(dpdu)
        dec = decode_dpdu(raw)
        assert dec.eow == 0x041

    def test_phase1_segmenter_default_eow(self):
        seg = NonArqSegmenter(max_payload=100)
        segments = seg.build_segments(b"hello")
        assert segments[0].eow_type == 0


# =======================================================================
# Issue #4 — on_link_made() → IDLE_CONNECTED (dts_state.py)
# =======================================================================

class TestOnLinkMadeTransition:
    """Table C-10: D_CONNECTION_MADE in IDLE(UNCONNECTED) → IDLE(CONNECTED)."""

    def test_link_made_goes_to_idle_connected(self):
        sm = DTSStateMachine()
        assert sm.state == DTSState.IDLE_UNCONNECTED
        sm.on_link_made()
        assert sm.state == DTSState.IDLE_CONNECTED

    def test_enter_data_from_idle_connected(self):
        sm = DTSStateMachine()
        sm.on_link_made()
        sm.enter_data()
        assert sm.state == DTSState.DATA_CONNECTED

    def test_enter_data_noop_from_data_connected(self):
        sm = DTSStateMachine()
        sm.on_link_made()
        sm.enter_data()
        sm.enter_data()  # should be idempotent
        assert sm.state == DTSState.DATA_CONNECTED

    def test_enter_data_noop_from_unconnected(self):
        sm = DTSStateMachine()
        sm.enter_data()  # should be noop in IDLE_UNCONNECTED
        assert sm.state == DTSState.IDLE_UNCONNECTED


# =======================================================================
# Issue #5 — UNCONNECTED states reject connection D_PDUs (dts_state.py)
# =======================================================================

class TestUnconnectedStateRestrictions:
    """Tables C-11, C-13, C-15: Types 0-5 generate WARNING reason=1."""

    CONNECTION_TYPES = [
        DPDUType.DATA_ONLY,
        DPDUType.ACK_ONLY,
        DPDUType.DATA_ACK,
        DPDUType.RESETWIN_RESYNC,
        DPDUType.EXPEDITED_DATA_ONLY,
        DPDUType.EXPEDITED_ACK_ONLY,
    ]

    ALWAYS_ALLOWED = [
        DPDUType.NON_ARQ,
        DPDUType.EXPEDITED_NON_ARQ,
        DPDUType.WARNING,
        DPDUType.MANAGEMENT,
    ]

    UNCONNECTED_STATES = [
        DTSState.IDLE_UNCONNECTED,
        DTSState.DATA_UNCONNECTED,
        DTSState.EXPEDITED_UNCONNECTED,
        DTSState.MANAGEMENT_UNCONNECTED,
    ]

    @pytest.mark.parametrize("dpdu_type", CONNECTION_TYPES)
    def test_idle_unconnected_rejects_connection_types(self, dpdu_type):
        sm = DTSStateMachine()
        assert not sm.is_allowed(dpdu_type)
        assert sm.warning_reason(dpdu_type) == WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED

    @pytest.mark.parametrize("dpdu_type", ALWAYS_ALLOWED)
    def test_idle_unconnected_allows_non_connection_types(self, dpdu_type):
        sm = DTSStateMachine()
        assert sm.is_allowed(dpdu_type)
        assert sm.warning_reason(dpdu_type) is None

    @pytest.mark.parametrize("dpdu_type", CONNECTION_TYPES)
    def test_connected_allows_data_types(self, dpdu_type):
        """DATA_CONNECTED should accept all connection-related types."""
        sm = DTSStateMachine()
        sm.on_link_made()
        sm.enter_data()
        assert sm.state == DTSState.DATA_CONNECTED
        assert sm.is_allowed(dpdu_type)


# =======================================================================
# Issue #7 — Warning reason code (stanag_node.py)
# =======================================================================

class TestWarningReasonCode:
    """Table C-3: Connection-related D_PDU w/o connection → reason 1, not 2."""

    def test_reason_1_for_unconnected(self):
        sm = DTSStateMachine()
        reason = sm.warning_reason(DPDUType.DATA_ONLY)
        assert reason == 1  # WARNING_REASON_CONN_DPDU_BUT_UNCONNECTED

    def test_reason_3_for_invalid_state(self):
        sm = DTSStateMachine()
        sm.on_link_made()
        # IDLE_CONNECTED doesn't allow DATA_ONLY (need enter_data first)
        reason = sm.warning_reason(DPDUType.DATA_ONLY)
        # In IDLE_CONNECTED (connected), DATA_ONLY is not connection-related reason,
        # it's invalid for state → reason 3
        assert reason == WARNING_REASON_INVALID_DPDU_FOR_STATE


# =======================================================================
# Issue #9 — Selective ACK bitmap truncated at UWE (arq.py)
# =======================================================================

class TestSelectiveAckBitmapTruncation:
    """C.3.4§(13-14): Bitmap truncated after byte containing RX UWE bit."""

    def _make_window(self, size, received_seqs, lwe):
        """Create an RX window with specific slots marked as RECEIVED."""
        window = [_RxSlot() for _ in range(size)]
        for seq in received_seqs:
            idx = seq % size
            window[idx].seq = seq
            window[idx].status = RxFrameStatus.RECEIVED
        return window

    def test_empty_window_returns_empty(self):
        window = self._make_window(128, [], 0)
        bitmap = _build_selective_ack_bitmap(0, window, 128)
        assert bitmap == b""

    def test_single_frame_truncates_to_one_byte(self):
        # rx_lwe=0, frame at seq=1 received
        window = self._make_window(128, [1], 0)
        bitmap = _build_selective_ack_bitmap(0, window, 128)
        assert len(bitmap) == 1
        assert bitmap[0] & 0x01 == 1  # bit 0 = seq 1

    def test_frames_near_lwe_minimal_bitmap(self):
        # rx_lwe=0, frames at seq=1,2,3 received
        window = self._make_window(128, [1, 2, 3], 0)
        bitmap = _build_selective_ack_bitmap(0, window, 128)
        assert len(bitmap) == 1
        assert bitmap[0] == 0x07  # bits 0,1,2

    def test_gap_produces_correct_bitmap(self):
        # rx_lwe=0, frame at seq=5 received (gap at 1-4)
        window = self._make_window(128, [5], 0)
        bitmap = _build_selective_ack_bitmap(0, window, 128)
        assert len(bitmap) == 1
        assert bitmap[0] == 0x10  # bit 4 = seq 5

    def test_frame_in_second_byte(self):
        # rx_lwe=0, frame at seq=9 received → needs 2 bytes
        window = self._make_window(128, [9], 0)
        bitmap = _build_selective_ack_bitmap(0, window, 128)
        assert len(bitmap) == 2
        assert bitmap[0] == 0x00  # no frames in bits 0-7
        assert bitmap[1] == 0x01  # bit 0 of byte 1 = seq 9

    def test_no_trailing_zero_bytes(self):
        # Only seq=2 received → bitmap should be 1 byte, not 16
        window = self._make_window(128, [2], 0)
        bitmap = _build_selective_ack_bitmap(0, window, 128)
        assert len(bitmap) == 1
        assert len(bitmap) < 16  # was 16 before fix

    def test_full_window_not_truncated_when_uwe_at_end(self):
        # All 128 slots received
        seqs = list(range(1, 129))
        window = self._make_window(128, seqs, 0)
        bitmap = _build_selective_ack_bitmap(0, window, 128)
        assert len(bitmap) == 16  # 128 bits = 16 bytes


# =======================================================================
# Issues #10-#13 — ManagementEngine fixes (management.py)
# =======================================================================

class TestManagementFrameIdConsistency:
    """Issue #10: send() returns frame_id that process_tx() actually uses."""

    def test_frame_id_matches(self):
        eng = ManagementEngine(1, 2)
        fid = eng.send(1, 0x50)
        frames = eng.process_tx(0)
        assert len(frames) >= 1
        decoded = decode_dpdu(frames[0])
        assert decoded.management.management_frame_id == fid

    def test_sequential_frame_ids(self):
        eng = ManagementEngine(1, 2)
        fid0 = eng.send(1, 0x10)
        fid1 = eng.send(1, 0x20)
        assert fid1 == fid0 + 1

        # Process first — ACK it — process second
        frames0 = eng.process_tx(0)
        dec0 = decode_dpdu(frames0[0])
        assert dec0.management.management_frame_id == fid0

        # Simulate ACK
        ack = build_management(0, 0, _addr(dest=1, src=2),
                               msg_type=0, data=b"", message_ack=True,
                               valid_message=False,
                               management_frame_id=fid0)
        eng.process_rx(decode_dpdu(encode_dpdu(ack)))

        frames1 = eng.process_tx(100)
        dec1 = decode_dpdu(frames1[0])
        assert dec1.management.management_frame_id == fid1


class TestManagementDuplicateDetection:
    """Issue #11: C.3.9§(21-25) duplicate frames are ACKed but not re-delivered."""

    def test_duplicate_not_delivered(self):
        received = []
        eng = ManagementEngine(1, 2, on_rx_callback=lambda d: received.append(d))

        dpdu = build_management(0, 0, _addr(dest=1, src=2),
                                msg_type=1, data=b"test",
                                message_contents=0x50,
                                management_frame_id=42)
        dec = decode_dpdu(encode_dpdu(dpdu))

        resp1 = eng.process_rx(dec)
        assert len(received) == 1
        assert len(resp1) == 1  # ACK

        received.clear()
        resp2 = eng.process_rx(dec)
        assert len(received) == 0  # NOT delivered again
        assert len(resp2) == 1  # still ACKed

    def test_different_frame_id_delivered(self):
        received = []
        eng = ManagementEngine(1, 2, on_rx_callback=lambda d: received.append(d))

        dpdu1 = build_management(0, 0, _addr(dest=1, src=2),
                                 msg_type=1, data=b"", management_frame_id=10)
        dpdu2 = build_management(0, 0, _addr(dest=1, src=2),
                                 msg_type=1, data=b"", management_frame_id=11)

        eng.process_rx(decode_dpdu(encode_dpdu(dpdu1)))
        assert len(received) == 1

        eng.process_rx(decode_dpdu(encode_dpdu(dpdu2)))
        assert len(received) == 2


class TestManagementAckValidMessage:
    """Issue #12: C.3.9§(7-8) ACK-only D_PDUs have valid_message=False."""

    def test_ack_has_valid_message_false(self):
        eng = ManagementEngine(1, 2)
        dpdu = build_management(0, 0, _addr(dest=1, src=2),
                                msg_type=1, data=b"", management_frame_id=0)
        responses = eng.process_rx(decode_dpdu(encode_dpdu(dpdu)))
        assert len(responses) == 1
        ack = decode_dpdu(responses[0])
        assert ack.management.message_ack is True
        assert ack.management.valid_message is False


class TestManagementNoForcedData:
    """Issue #13: C.3.9§(9-10) no forced data=b'\\x00' when no extended message."""

    def test_no_extended_message_data(self):
        eng = ManagementEngine(1, 2)
        eng.send(1, 0x50)  # no data
        frames = eng.process_tx(0)
        decoded = decode_dpdu(frames[0])
        assert decoded.user_data == b""

    def test_extended_message_preserved(self):
        eng = ManagementEngine(1, 2)
        eng.send(1, 0x50, b"\x01\x02\x03")
        frames = eng.process_tx(0)
        decoded = decode_dpdu(frames[0])
        assert decoded.user_data == b"\x01\x02\x03"


# =======================================================================
# Issue #15 — Expedited ARQ CRC validation (expedited_arq.py)
# =======================================================================

class TestExpeditedArqCrcValidation:
    """C.3.4§(6): Expedited D_PDUs with bad data CRC are not accepted."""

    def test_good_crc_accepted(self):
        from src.dpdu_frame import build_expedited_data_only

        eng = ExpeditedArqEngine(1, 2)
        dpdu = build_expedited_data_only(0, 0, _addr(dest=1, src=2),
                                          b"hello", tx_frame_seq=0, cpdu_id=0)
        raw = encode_dpdu(dpdu)
        decoded = decode_dpdu(raw)
        assert decoded.data_crc_ok is True

        eng.process_rx_dpdu(decoded)
        delivered = eng.get_delivered_cpdus()
        assert len(delivered) == 1
        assert delivered[0] == b"hello"

    def test_bad_crc_rejected(self):
        from src.dpdu_frame import build_expedited_data_only, flip_bit

        eng = ExpeditedArqEngine(1, 2)
        dpdu = build_expedited_data_only(0, 0, _addr(dest=1, src=2),
                                          b"hello", tx_frame_seq=0, cpdu_id=0)
        raw = encode_dpdu(dpdu)

        # Corrupt a data byte (after header CRC)
        corrupted = flip_bit(raw, (len(raw) - 5) * 8)
        decoded = decode_dpdu(corrupted)
        assert decoded.data_crc_ok is False

        eng.process_rx_dpdu(decoded)
        delivered = eng.get_delivered_cpdus()
        assert len(delivered) == 0  # rejected

    def test_no_ack_for_bad_crc(self):
        from src.dpdu_frame import build_expedited_data_only, flip_bit

        eng = ExpeditedArqEngine(1, 2)
        dpdu = build_expedited_data_only(0, 0, _addr(dest=1, src=2),
                                          b"hello", tx_frame_seq=0, cpdu_id=0)
        raw = encode_dpdu(dpdu)
        corrupted = flip_bit(raw, (len(raw) - 5) * 8)
        decoded = decode_dpdu(corrupted)

        eng.process_rx_dpdu(decoded)
        # No ACK should be pending
        frames = eng.process_tx(0)
        assert len(frames) == 0


# =======================================================================
# Issue #14 — Expedited ARQ multi-D_PDU segmentation (C.3.7)
# =======================================================================

class TestExpeditedArqSegmentation:
    """C.3.7: Expedited D_PDUs support C_PDU START/END segmentation."""

    def _make_ack(self, rx_lwe, dest=1, src=2):
        from src.dpdu_frame import build_expedited_ack_only, dpdu_calc_eot_field
        addr = _addr(dest=dest, src=src)
        ack = build_expedited_ack_only(0, dpdu_calc_eot_field(1), addr, rx_lwe=rx_lwe)
        raw = encode_dpdu(ack)
        return decode_dpdu(raw)

    def test_small_cpdu_single_segment(self):
        """C_PDU <= 1023 bytes → single D_PDU with pdu_start=True, pdu_end=True."""
        eng = ExpeditedArqEngine(1, 2)
        eng.submit_cpdu(b"A" * 100)
        frames = eng.process_tx(0)
        assert len(frames) == 1
        d = decode_dpdu(frames[0])
        assert d.data.pdu_start is True
        assert d.data.pdu_end is True
        assert d.data.tx_frame_seq == 0
        assert d.data.cpdu_id == 0
        assert len(d.user_data) == 100

    def test_exactly_1023_single_segment(self):
        """Boundary: exactly 1023 bytes → single segment."""
        eng = ExpeditedArqEngine(1, 2)
        eng.submit_cpdu(b"X" * 1023)
        frames = eng.process_tx(0)
        assert len(frames) == 1
        d = decode_dpdu(frames[0])
        assert d.data.pdu_start is True
        assert d.data.pdu_end is True
        assert len(d.user_data) == 1023

    def test_exactly_1024_two_segments(self):
        """Boundary: 1024 bytes → 2 segments (1023 + 1)."""
        eng = ExpeditedArqEngine(1, 2)
        eng.submit_cpdu(b"Y" * 1024)

        # Primeiro segmento
        frames = eng.process_tx(0)
        assert len(frames) == 1
        d1 = decode_dpdu(frames[0])
        assert d1.data.pdu_start is True
        assert d1.data.pdu_end is False
        assert d1.data.tx_frame_seq == 0
        assert len(d1.user_data) == 1023

        # ACK primeiro segmento (C.6.2 §12: rx_lwe = next expected = seq+1)
        eng.process_rx_dpdu(self._make_ack(1, dest=1, src=2))

        # Segundo segmento
        frames = eng.process_tx(1)
        assert len(frames) == 1
        d2 = decode_dpdu(frames[0])
        assert d2.data.pdu_start is False
        assert d2.data.pdu_end is True
        assert d2.data.tx_frame_seq == 1
        assert len(d2.user_data) == 1

    def test_large_cpdu_multiple_segments(self):
        """2500 bytes → 3 segments (1023 + 1023 + 454), full stop-and-wait cycle."""
        eng = ExpeditedArqEngine(1, 2)
        payload = bytes(range(256)) * 10  # 2560 bytes
        payload = payload[:2500]
        eng.submit_cpdu(payload)

        # Segmento 1: pdu_start=True, pdu_end=False
        frames = eng.process_tx(0)
        assert len(frames) == 1
        d1 = decode_dpdu(frames[0])
        assert d1.data.pdu_start is True
        assert d1.data.pdu_end is False
        assert d1.data.tx_frame_seq == 0
        assert d1.data.cpdu_id == 0
        assert len(d1.user_data) == 1023

        # ACK segmento 1 (rx_lwe = seq+1 = 1)
        eng.process_rx_dpdu(self._make_ack(1, dest=1, src=2))

        # Segmento 2: pdu_start=False, pdu_end=False
        frames = eng.process_tx(1)
        assert len(frames) == 1
        d2 = decode_dpdu(frames[0])
        assert d2.data.pdu_start is False
        assert d2.data.pdu_end is False
        assert d2.data.tx_frame_seq == 1
        assert d2.data.cpdu_id == 0

        # ACK segmento 2 (rx_lwe = 2)
        eng.process_rx_dpdu(self._make_ack(2, dest=1, src=2))

        # Segmento 3: pdu_start=False, pdu_end=True
        frames = eng.process_tx(2)
        assert len(frames) == 1
        d3 = decode_dpdu(frames[0])
        assert d3.data.pdu_start is False
        assert d3.data.pdu_end is True
        assert d3.data.tx_frame_seq == 2
        assert d3.data.cpdu_id == 0
        assert len(d3.user_data) == 2500 - 1023 - 1023

        # ACK segmento 3 (rx_lwe = 3)
        eng.process_rx_dpdu(self._make_ack(3, dest=1, src=2))

        # cpdu_id deve ter avançado para 1
        assert eng._cpdu_id == 1
        # Sem mais frames
        frames = eng.process_tx(3)
        assert len(frames) == 0

    def test_cpdu_id_advances_only_after_all_segments(self):
        """cpdu_id stays 0 until last segment of C_PDU is ACKed."""
        eng = ExpeditedArqEngine(1, 2)
        eng.submit_cpdu(b"Z" * 1500)  # 2 segments: 1023 + 477

        # Primeiro segmento
        eng.process_tx(0)
        assert eng._cpdu_id == 0

        # ACK primeiro segmento (rx_lwe = 1)
        eng.process_rx_dpdu(self._make_ack(1, dest=1, src=2))
        assert eng._cpdu_id == 0  # Ainda 0 — falta o segundo segmento

        # Segundo segmento
        eng.process_tx(1)
        eng.process_rx_dpdu(self._make_ack(2, dest=1, src=2))  # rx_lwe = 2
        assert eng._cpdu_id == 1  # Agora avançou

    def test_rx_reassembly_multi_segment(self):
        """RX: 3 segments reassembled into single C_PDU on pdu_end."""
        from src.dpdu_frame import build_expedited_data_only, dpdu_calc_eot_field

        eng = ExpeditedArqEngine(1, 2)
        part_a = b"A" * 1023
        part_b = b"B" * 1023
        part_c = b"C" * 300

        segs = [
            build_expedited_data_only(0, dpdu_calc_eot_field(1), _addr(dest=1, src=2),
                                       part_a, tx_frame_seq=0, cpdu_id=0,
                                       pdu_start=True, pdu_end=False),
            build_expedited_data_only(0, dpdu_calc_eot_field(1), _addr(dest=1, src=2),
                                       part_b, tx_frame_seq=1, cpdu_id=0,
                                       pdu_start=False, pdu_end=False),
            build_expedited_data_only(0, dpdu_calc_eot_field(1), _addr(dest=1, src=2),
                                       part_c, tx_frame_seq=2, cpdu_id=0,
                                       pdu_start=False, pdu_end=True),
        ]

        # Feed segmentos 0 e 1 — nenhum delivery
        for s in segs[:2]:
            raw = encode_dpdu(s)
            eng.process_rx_dpdu(decode_dpdu(raw))
        assert len(eng.get_delivered_cpdus()) == 0

        # Feed segmento 2 (pdu_end) — delivery
        raw = encode_dpdu(segs[2])
        eng.process_rx_dpdu(decode_dpdu(raw))
        delivered = eng.get_delivered_cpdus()
        assert len(delivered) == 1
        assert delivered[0] == part_a + part_b + part_c

    def test_rx_single_segment_still_works(self):
        """Regression: single D_PDU with pdu_start+pdu_end delivers immediately."""
        from src.dpdu_frame import build_expedited_data_only

        eng = ExpeditedArqEngine(1, 2)
        dpdu = build_expedited_data_only(0, 0, _addr(dest=1, src=2),
                                          b"hello", tx_frame_seq=0, cpdu_id=0)
        raw = encode_dpdu(dpdu)
        eng.process_rx_dpdu(decode_dpdu(raw))
        delivered = eng.get_delivered_cpdus()
        assert len(delivered) == 1
        assert delivered[0] == b"hello"

    def test_retransmit_preserves_segment_state(self):
        """Timeout retransmits same segment, then continues after ACK."""
        eng = ExpeditedArqEngine(1, 2, retx_timeout_ms=100)
        eng.submit_cpdu(b"R" * 2048)

        # Primeiro segmento
        frames1 = eng.process_tx(0)
        assert len(frames1) == 1
        original_frame = frames1[0]

        # Sem ACK, timeout → retransmissão
        frames2 = eng.process_tx(200)
        assert len(frames2) == 1
        assert frames2[0] == original_frame  # Mesmo frame

        # ACK → próximo segmento (rx_lwe = 1 = next expected)
        eng.process_rx_dpdu(self._make_ack(1, dest=1, src=2))
        frames3 = eng.process_tx(300)
        assert len(frames3) == 1
        d = decode_dpdu(frames3[0])
        assert d.data.tx_frame_seq == 1  # Segundo segmento

    def test_max_retries_discards_entire_cpdu(self):
        """Max retries discards entire C_PDU including remaining segments."""
        eng = ExpeditedArqEngine(1, 2, retx_timeout_ms=100, max_retries=2)
        eng.submit_cpdu(b"D" * 2048)

        # Primeiro segmento
        eng.process_tx(0)

        # Timeout 1
        eng.process_tx(200)
        # Timeout 2
        eng.process_tx(400)
        # Timeout 3 → max_retries excedido (retx_count > 2)
        frames = eng.process_tx(600)
        assert len(frames) == 0

        # Nenhum segmento restante
        frames = eng.process_tx(700)
        assert len(frames) == 0
        assert not eng.has_pending_tx()


# =======================================================================
# Issue #4+#5 integration — build_management valid_message parameter
# =======================================================================

class TestBuildManagementValidMessage:
    """build_management() now accepts valid_message parameter."""

    def test_default_valid_message_true(self):
        dpdu = build_management(0, 0, _addr(), msg_type=1, data=b"")
        assert dpdu.management.valid_message is True

    def test_valid_message_false(self):
        dpdu = build_management(0, 0, _addr(), msg_type=1, data=b"",
                                valid_message=False, message_ack=True)
        assert dpdu.management.valid_message is False
        raw = encode_dpdu(dpdu)
        decoded = decode_dpdu(raw)
        assert decoded.management.valid_message is False
        assert decoded.management.message_ack is True
