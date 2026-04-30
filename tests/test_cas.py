"""Tests for CAS (Channel Access Sublayer) — STANAG 5066 Annex B, Edition 3."""

from __future__ import annotations

import pytest

from src.cas import CASEngine, _LinkContext, decode_cpdu, encode_cpdu
from src.stypes import (
    CPDU,
    CAS_LOCAL_TIMEOUT,
    CPDUBreakReason,
    CPDURejectReason,
    CPDUType,
    CasEvent,
    CasLinkState,
    DPDUType,
    PhysicalLinkType,
)


# -----------------------------------------------------------------------
# Stub NonArqEngine — records queued C_PDUs
# -----------------------------------------------------------------------

class _StubNonArq:
    """Minimal stub that captures queue_cpdu calls."""

    def __init__(self):
        self.sent: list[tuple[DPDUType, int, bytes]] = []

    def queue_cpdu(self, dpdu_type: DPDUType, destination: int,
                   payload: bytes, **kwargs) -> None:
        self.sent.append((dpdu_type, destination, payload))

    def last_cpdu(self) -> CPDU:
        """Decode most recent sent C_PDU."""
        assert self.sent, "no C_PDU sent"
        return decode_cpdu(self.sent[-1][2])

    def sent_cpdus(self) -> list[CPDU]:
        return [decode_cpdu(s[2]) for s in self.sent]

    def clear(self) -> None:
        self.sent.clear()


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture
def non_arq():
    return _StubNonArq()


@pytest.fixture
def cas(non_arq):
    """CAS engine with short timeouts for testing."""
    return CASEngine(
        local_node_address=1,
        non_arq=non_arq,
        call_timeout_ms=1000,
        break_timeout_ms=500,
        max_retries=2,
        called_idle_timeout_ms=5000,
    )


def _accept_incoming(cas: CASEngine, remote: int, current_time_ms: int,
                     link_type: int = 0) -> None:
    """Simulate an incoming LINK_REQUEST that gets accepted."""
    cas.process_cpdu(
        CPDU(CPDUType.LINK_REQUEST, link_type=link_type),
        remote, current_time_ms,
    )


def _send_data_to(cas: CASEngine, remote: int, current_time_ms: int) -> None:
    """Simulate receiving a DATA C_PDU from remote."""
    cas.process_cpdu(CPDU(CPDUType.DATA, payload=b"\x01"), remote, current_time_ms)


# -----------------------------------------------------------------------
# Test 1 — Called idle timeout: no DATA → link removed
# -----------------------------------------------------------------------

class TestCalledIdleTimeoutNoData:
    def test_link_removed_after_timeout(self, cas, non_arq):
        _accept_incoming(cas, remote=10, current_time_ms=1000)
        assert cas.get_link_state(10) == CasLinkState.MADE

        # Advance past idle timeout (5000ms) with no DATA
        cas.tick(6001)
        assert cas.get_link_state(10) == CasLinkState.IDLE

    def test_link_persists_before_timeout(self, cas, non_arq):
        _accept_incoming(cas, remote=10, current_time_ms=1000)
        cas.tick(5999)
        assert cas.get_link_state(10) == CasLinkState.MADE


# -----------------------------------------------------------------------
# Test 2 — Called idle timeout: DATA resets timer
# -----------------------------------------------------------------------

class TestCalledIdleTimeoutDataResetsTimer:
    def test_data_resets_timer(self, cas, non_arq):
        _accept_incoming(cas, remote=10, current_time_ms=1000)

        # Receive DATA at t=4000 (before 5s timeout)
        _send_data_to(cas, remote=10, current_time_ms=4000)

        # At t=6001 (>5s from link made, but <5s from last data) → still alive
        cas.tick(6001)
        assert cas.get_link_state(10) == CasLinkState.MADE

        # At t=9001 (>5s from last data at 4000) → removed
        cas.tick(9001)
        assert cas.get_link_state(10) == CasLinkState.IDLE


# -----------------------------------------------------------------------
# Test 3 — Called idle timeout: DATA then silence → removed
# -----------------------------------------------------------------------

class TestCalledIdleTimeoutAfterData:
    def test_timeout_after_data_stops(self, cas, non_arq):
        _accept_incoming(cas, remote=10, current_time_ms=1000)

        # Receive DATA at t=2000
        _send_data_to(cas, remote=10, current_time_ms=2000)

        # Still alive at t=6999 (4999ms after last DATA)
        cas.tick(6999)
        assert cas.get_link_state(10) == CasLinkState.MADE

        # Removed at t=7001 (5001ms after last DATA)
        cas.tick(7001)
        assert cas.get_link_state(10) == CasLinkState.IDLE


# -----------------------------------------------------------------------
# Test 4 — Exclusive accept with one CALLING (not MADE)
# -----------------------------------------------------------------------

class TestExclusiveAcceptWithOneCalling:
    def test_exclusive_accepted_when_one_calling(self, cas, non_arq):
        """1 exclusive CALLING + 0 exclusive MADE → new exclusive request accepted."""
        # Make local node start an outgoing exclusive call (state=CALLING)
        cas.make_link(remote_node_address=20, current_time_ms=0,
                      link_type=PhysicalLinkType.EXCLUSIVE)
        assert cas.get_link_state(20) == CasLinkState.CALLING

        # Incoming exclusive request from different node → should be accepted
        # (CALLING is not "active" per spec B.3.2 shall(5))
        _accept_incoming(cas, remote=30, current_time_ms=100,
                         link_type=int(PhysicalLinkType.EXCLUSIVE))
        assert cas.get_link_state(30) == CasLinkState.MADE

        # Verify ACCEPTED was sent (last control CPDU)
        accepted = [c for c in non_arq.sent_cpdus()
                    if c.cpdu_type == CPDUType.LINK_ACCEPTED]
        assert len(accepted) >= 1


# -----------------------------------------------------------------------
# Test 5 — Exclusive reject with 2 MADE
# -----------------------------------------------------------------------

class TestExclusiveRejectWithTwoMade:
    def test_exclusive_rejected_when_two_made(self, cas, non_arq):
        """2 exclusive MADE → new exclusive request rejected."""
        # Accept two exclusive links
        _accept_incoming(cas, remote=10, current_time_ms=0,
                         link_type=int(PhysicalLinkType.EXCLUSIVE))
        _accept_incoming(cas, remote=20, current_time_ms=0,
                         link_type=int(PhysicalLinkType.EXCLUSIVE))
        assert cas.get_link_state(10) == CasLinkState.MADE
        assert cas.get_link_state(20) == CasLinkState.MADE

        non_arq.clear()
        # Third exclusive request → rejected
        _accept_incoming(cas, remote=30, current_time_ms=0,
                         link_type=int(PhysicalLinkType.EXCLUSIVE))
        assert cas.get_link_state(30) != CasLinkState.MADE
        rejected = [c for c in non_arq.sent_cpdus()
                    if c.cpdu_type == CPDUType.LINK_REJECTED]
        assert len(rejected) == 1


# -----------------------------------------------------------------------
# Test 6 — Nonexclusive rejected if exclusive active
# -----------------------------------------------------------------------

class TestNonexclusiveRejectedIfExclusiveActive:
    def test_nonexclusive_rejected(self, cas, non_arq):
        """Exclusive MADE → nonexclusive request rejected (B.3.2 shall(4))."""
        _accept_incoming(cas, remote=10, current_time_ms=0,
                         link_type=int(PhysicalLinkType.EXCLUSIVE))
        assert cas.get_link_state(10) == CasLinkState.MADE

        non_arq.clear()
        _accept_incoming(cas, remote=20, current_time_ms=0,
                         link_type=int(PhysicalLinkType.NONEXCLUSIVE))
        rejected = [c for c in non_arq.sent_cpdus()
                    if c.cpdu_type == CPDUType.LINK_REJECTED]
        assert len(rejected) == 1
        assert rejected[0].reason == int(CPDURejectReason.SUPPORTING_EXCLUSIVE_LINK)


# -----------------------------------------------------------------------
# Test 7 — Link request re-accept
# -----------------------------------------------------------------------

class TestLinkRequestReaccept:
    def test_reaccept_same_remote(self, cas, non_arq):
        """Link MADE + second LINK_REQUEST from same addr → re-send ACCEPTED."""
        _accept_incoming(cas, remote=10, current_time_ms=0)
        assert cas.get_link_state(10) == CasLinkState.MADE

        non_arq.clear()
        # Second request from same node
        _accept_incoming(cas, remote=10, current_time_ms=100)
        # Still MADE, and ACCEPTED re-sent
        assert cas.get_link_state(10) == CasLinkState.MADE
        accepted = [c for c in non_arq.sent_cpdus()
                    if c.cpdu_type == CPDUType.LINK_ACCEPTED]
        assert len(accepted) == 1


# -----------------------------------------------------------------------
# Test 8 — Caller breaks nonexclusive on exclusive accept
# -----------------------------------------------------------------------

class TestCallerBreaksNonexclusiveOnExclusiveAccept:
    def test_nonexclusive_enters_breaking(self, cas, non_arq):
        """Caller has nonexclusive MADE, receives exclusive ACCEPTED → nonexclusive BREAKING."""
        # Accept incoming nonexclusive link (called node)
        _accept_incoming(cas, remote=10, current_time_ms=0)
        assert cas.get_link_state(10) == CasLinkState.MADE

        # Start outgoing exclusive call to different node
        cas.make_link(remote_node_address=20, current_time_ms=100,
                      link_type=PhysicalLinkType.EXCLUSIVE)
        assert cas.get_link_state(20) == CasLinkState.CALLING

        # Remote 20 accepts → nonexclusive with 10 should enter BREAKING
        cas.process_cpdu(CPDU(CPDUType.LINK_ACCEPTED), 20, 200)
        assert cas.get_link_state(20) == CasLinkState.MADE
        assert cas.get_link_state(10) == CasLinkState.BREAKING


# -----------------------------------------------------------------------
# Test 9 — Encode/decode roundtrip for all 6 C_PDU types
# -----------------------------------------------------------------------

class TestEncodeDecodeRoundtrip:
    @pytest.mark.parametrize("cpdu", [
        CPDU(CPDUType.DATA, payload=b"\xDE\xAD\xBE\xEF"),
        CPDU(CPDUType.LINK_REQUEST, link_type=0),
        CPDU(CPDUType.LINK_REQUEST, link_type=1),
        CPDU(CPDUType.LINK_ACCEPTED),
        CPDU(CPDUType.LINK_REJECTED, reason=3),
        CPDU(CPDUType.LINK_BREAK, reason=4),
        CPDU(CPDUType.LINK_BREAK_CONFIRM),
    ])
    def test_roundtrip(self, cpdu):
        encoded = encode_cpdu(cpdu)
        decoded = decode_cpdu(encoded)
        assert decoded.cpdu_type == cpdu.cpdu_type
        if cpdu.cpdu_type == CPDUType.DATA:
            assert decoded.payload == cpdu.payload
        elif cpdu.cpdu_type == CPDUType.LINK_REQUEST:
            assert decoded.link_type == cpdu.link_type
        elif cpdu.cpdu_type in (CPDUType.LINK_REJECTED, CPDUType.LINK_BREAK):
            assert decoded.reason == cpdu.reason


# -----------------------------------------------------------------------
# Test 10 — Breaking timeout with retry
# -----------------------------------------------------------------------

class TestBreakingTimeoutRetry:
    def test_retry_then_broken(self, cas, non_arq):
        """LINK_BREAK timeout → retry → eventual link broken."""
        _accept_incoming(cas, remote=10, current_time_ms=0)
        assert cas.get_link_state(10) == CasLinkState.MADE

        cas.break_link(current_time_ms=100, remote=10)
        assert cas.get_link_state(10) == CasLinkState.BREAKING

        # Count initial BREAK sent
        breaks_sent = sum(1 for c in non_arq.sent_cpdus()
                          if c.cpdu_type == CPDUType.LINK_BREAK)
        assert breaks_sent == 1

        # Retry 1 at t=601 (>500ms timeout)
        cas.tick(601)
        breaks_sent = sum(1 for c in non_arq.sent_cpdus()
                          if c.cpdu_type == CPDUType.LINK_BREAK)
        assert breaks_sent == 2
        assert cas.get_link_state(10) == CasLinkState.BREAKING

        # Retry 2 at t=1102
        cas.tick(1102)
        breaks_sent = sum(1 for c in non_arq.sent_cpdus()
                          if c.cpdu_type == CPDUType.LINK_BREAK)
        assert breaks_sent == 3
        assert cas.get_link_state(10) == CasLinkState.BREAKING

        # Max retries exhausted at t=1603 → link removed
        cas.tick(1603)
        assert cas.get_link_state(10) == CasLinkState.IDLE

        # Verify IDLE event emitted
        idle_events = [e for e in cas.event_log
                       if e.state == CasLinkState.IDLE and e.remote == 10]
        assert len(idle_events) >= 1
