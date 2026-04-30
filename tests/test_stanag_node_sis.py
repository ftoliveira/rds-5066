"""Tests for StanagNode SIS business logic corrections."""

import pytest

from src.modem_if import ModemConfig, ModemInterface
from src.stanag_node import StanagNode
from src.stypes import (
    LinkType, ServiceType, SisHardLinkRejectReason,
    SisLinkSessionState,
)


# -----------------------------------------------------------------------
# Minimal modem stub
# -----------------------------------------------------------------------

class _StubModem(ModemInterface):
    def __init__(self):
        super().__init__(config=ModemConfig())
        self.tx_frames: list[bytes] = []

    def modem_rx_read_frame(self):
        return None

    def modem_tx_dpdu(self, dpdu_buffer, length=None):
        self.tx_frames.append(dpdu_buffer)
        return len(dpdu_buffer)

    def modem_tx_burst(self, frames):
        self.tx_frames.extend(frames)
        return sum(len(f) for f in frames)

    def modem_rx_start(self):
        pass

    def modem_rx_stop(self):
        pass

    def modem_get_carrier_status(self):
        return True

    def modem_set_tx_enable(self, enabled):
        pass


def _make_node(**kwargs):
    modem = _StubModem()
    return StanagNode(local_node_address=1, modem=modem, **kwargs)


# -----------------------------------------------------------------------
# #17 — Hard Link Indication only for Type 2
# -----------------------------------------------------------------------

class TestHardLinkIndicationType2Only:
    def test_type2_triggers_indication(self):
        """link_type==2 should invoke hard_link_indication callback."""
        node = _make_node()
        node.bind(5, rank=0)
        indications = []
        node.register_callbacks(
            hard_link_indication=lambda *a: indications.append(a))
        # Simulate receiving S_PDU type 3 (hard link establish request) with link_type=2
        from src.sis import encode_spdu_hard_link_request
        from src.stypes import SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST
        payload = encode_spdu_hard_link_request(link_type=2, link_priority=1,
                                                 requesting_sap=5, remote_sap=5)
        node._process_spdu_control(payload, src_addr=99)
        assert len(indications) == 1

    def test_type0_does_not_trigger_indication(self):
        """link_type==0 should NOT invoke hard_link_indication, auto-accept instead."""
        node = _make_node()
        node.bind(5, rank=0)
        indications = []
        established = []
        node.register_callbacks(
            hard_link_indication=lambda *a: indications.append(a),
            hard_link_established=lambda *a: established.append(a))
        from src.sis import encode_spdu_hard_link_request
        payload = encode_spdu_hard_link_request(link_type=0, link_priority=0,
                                                 requesting_sap=5, remote_sap=5)
        node._process_spdu_control(payload, src_addr=99)
        assert len(indications) == 0


# -----------------------------------------------------------------------
# #16 — Hard link precedence rules
# -----------------------------------------------------------------------

class TestHardLinkPrecedence:
    def _setup_active_hard_link(self, node, owner_sap=5, owner_rank=5,
                                 link_priority=1, link_type=0):
        node.bind(owner_sap, rank=owner_rank)
        node._link_session.link_type = LinkType.HARD
        node._link_session.state = SisLinkSessionState.ACTIVE
        node._link_session.hard_link_owner = owner_sap
        node._link_session.hard_link_owner_rank = owner_rank
        node._link_session.link_priority = link_priority
        node._link_session.sis_hard_link_type = link_type

    def test_lower_priority_rejected(self):
        """Incoming request with lower priority should be silently ignored."""
        node = _make_node()
        self._setup_active_hard_link(node, link_priority=2)
        from src.sis import encode_spdu_hard_link_request
        payload = encode_spdu_hard_link_request(link_type=0, link_priority=1,
                                                 requesting_sap=0, remote_sap=5)
        # Should return without changing state
        prev_state = node._link_session.state
        node._process_spdu_control(payload, src_addr=99)
        assert node._link_session.state == prev_state

    def test_higher_priority_accepted(self):
        """Incoming request with higher priority should preempt (same rank=0)."""
        node = _make_node()
        # Use rank=0 since remote rank is always 0 (unknown from S_PDU)
        self._setup_active_hard_link(node, owner_rank=0, link_priority=1)
        established = []
        node.register_callbacks(hard_link_established=lambda *a: established.append(a))
        from src.sis import encode_spdu_hard_link_request
        payload = encode_spdu_hard_link_request(link_type=0, link_priority=3,
                                                 requesting_sap=0, remote_sap=5)
        node._process_spdu_control(payload, src_addr=99)
        assert len(established) == 1

    def test_same_priority_higher_link_type_accepted(self):
        """Same rank+priority but higher link_type should preempt."""
        node = _make_node()
        self._setup_active_hard_link(node, owner_rank=0, link_priority=1, link_type=0)
        established = []
        node.register_callbacks(hard_link_established=lambda *a: established.append(a))
        from src.sis import encode_spdu_hard_link_request
        payload = encode_spdu_hard_link_request(link_type=1, link_priority=1,
                                                 requesting_sap=0, remote_sap=5)
        node._process_spdu_control(payload, src_addr=99)
        assert len(established) == 1

    def test_higher_rank_blocks_lower_rank_request(self):
        """Existing link with rank>0 blocks remote (rank=0 from S_PDU)."""
        node = _make_node()
        self._setup_active_hard_link(node, owner_rank=5, link_priority=0)
        from src.sis import encode_spdu_hard_link_request
        payload = encode_spdu_hard_link_request(link_type=0, link_priority=3,
                                                 requesting_sap=0, remote_sap=5)
        prev_state = node._link_session.state
        node._process_spdu_control(payload, src_addr=99)
        assert node._link_session.state == prev_state  # not preempted

    def test_same_priority_same_link_type_rejected(self):
        """Same rank+priority+link_type → first-come wins, reject newcomer."""
        node = _make_node()
        self._setup_active_hard_link(node, link_priority=1, link_type=1)
        from src.sis import encode_spdu_hard_link_request
        payload = encode_spdu_hard_link_request(link_type=1, link_priority=1,
                                                 requesting_sap=0, remote_sap=5)
        prev_state = node._link_session.state
        node._process_spdu_control(payload, src_addr=99)
        assert node._link_session.state == prev_state


# -----------------------------------------------------------------------
# #22 — Hard link establish timeout
# -----------------------------------------------------------------------

class TestHardLinkEstablishTimeout:
    def test_timeout_fires_rejected(self):
        node = _make_node(hard_link_establish_timeout_ms=1000)
        node.bind(5, rank=0)
        rejected = []
        node.register_callbacks(
            hard_link_rejected=lambda addr, sap, reason: rejected.append(reason))
        # Simulate awaiting response with timeout set
        node._link_session.awaiting_hard_link_response = True
        node._link_session.hard_link_response_timeout_ms = 5000
        node._link_session.remote_addr = 99
        node._link_session.remote_sap = 3

        # Before timeout — nothing happens
        node._check_hard_link_timeouts(4999)
        assert len(rejected) == 0
        assert node._link_session.awaiting_hard_link_response is True

        # At timeout — triggers rejected callback
        node._check_hard_link_timeouts(5000)
        assert len(rejected) == 1
        assert rejected[0] == int(SisHardLinkRejectReason.REMOTE_NODE_NOT_RESPONDING)
        assert node._link_session.awaiting_hard_link_response is False
        assert node._link_session.state == SisLinkSessionState.IDLE


# -----------------------------------------------------------------------
# #23 — Hard link terminate timeout
# -----------------------------------------------------------------------

class TestHardLinkTerminateTimeout:
    def test_timeout_fires_terminated(self):
        node = _make_node(hard_link_terminate_timeout_ms=500)
        node.bind(5, rank=0)
        terminated = []
        node.register_callbacks(
            hard_link_terminated=lambda addr, initiator_received_confirm: terminated.append(
                initiator_received_confirm))
        node._link_session.awaiting_terminate_confirm = True
        node._link_session.terminate_confirm_timeout_ms = 3000
        node._link_session.link_type = LinkType.HARD
        node._link_session.remote_addr = 99

        node._check_hard_link_timeouts(2999)
        assert len(terminated) == 0

        node._check_hard_link_timeouts(3000)
        assert len(terminated) == 1
        assert terminated[0] is False  # initiator_received_confirm=False
        assert node._link_session.state == SisLinkSessionState.IDLE
        assert node._link_session.link_type == LinkType.SOFT


# -----------------------------------------------------------------------
# #19 — Management message rank validation
# -----------------------------------------------------------------------

class TestManagementMsgRank:
    def test_rank_15_accepted(self):
        node = _make_node()
        node.bind(5, rank=15)
        assert node.validate_management_msg_rank(5) is True

    def test_rank_below_15_rejected(self):
        node = _make_node()
        node.bind(5, rank=14)
        assert node.validate_management_msg_rank(5) is False

    def test_unbound_sap_rejected(self):
        node = _make_node()
        assert node.validate_management_msg_rank(5) is False


# -----------------------------------------------------------------------
# #21 — Expedited count tracking
# -----------------------------------------------------------------------

class TestExpeditedTracking:
    def test_disabled_by_default(self):
        node = _make_node()
        node.bind(5)
        assert node.track_expedited_request(5) is True

    def test_within_limit(self):
        node = _make_node(max_expedited_per_client=3)
        node.bind(5)
        assert node.track_expedited_request(5) is True
        assert node.track_expedited_request(5) is True
        assert node.track_expedited_request(5) is True

    def test_exceeds_limit_unbinds(self):
        node = _make_node(max_expedited_per_client=2)
        node.bind(5)
        assert node.track_expedited_request(5) is True
        assert node.track_expedited_request(5) is True
        # Third exceeds limit
        assert node.track_expedited_request(5) is False
        assert 5 not in node._saps  # unbind happened

    def test_per_sap_independent(self):
        node = _make_node(max_expedited_per_client=1)
        node.bind(5)
        node.bind(6)
        assert node.track_expedited_request(5) is True
        assert node.track_expedited_request(6) is True
        assert node.track_expedited_request(5) is False
        assert node.track_expedited_request(6) is False


# -----------------------------------------------------------------------
# Hard link owner rank stored on establish
# -----------------------------------------------------------------------

class TestHardLinkOwnerRank:
    def test_rank_stored(self):
        node = _make_node()
        node.bind(5, rank=10)
        node.hard_link_establish(sap_id=5, link_priority=1,
                                  remote_addr=99, remote_sap=3, link_type=0)
        assert node._link_session.hard_link_owner_rank == 10
        assert node._link_session.is_calling is True


# -----------------------------------------------------------------------
# S_EXPEDITED_UNIDATA_REQUEST uses priority=0 (A.3.1.1)
# -----------------------------------------------------------------------

class TestExpeditedUnidataPriority:
    def test_expedited_request_uses_priority_zero(self):
        """A.3.1.1: S_EXPEDITED_UNIDATA_REQUEST has no PRIORITY; internal value must be 0."""
        node = _make_node()
        node.bind(5, rank=10)
        node.expedited_unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            ttl_seconds=60, updu=b"test",
        )
        assert len(node._tx_queue) == 1
        entry = node._tx_queue[0]
        assert entry.spdu.priority == 0
        assert entry.delivery_mode.expedited is True
