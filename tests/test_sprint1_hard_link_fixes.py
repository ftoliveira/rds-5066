"""Tests for Sprint 1 — Anexo A CRÍTICAS (Hard Link).

Cobre:
- CRITICA-A1: controle Hard Link via Expedited ARQ quando CAS está MADE
                (pré-CAS continua usando Expedited Non-ARQ como fallback).
- CRITICA-A2: limite de S_EXPEDITED_UNIDATA_REQUEST dispara
                S_UNBIND_INDICATION com reason=4.
- CRITICA-A3: REQUEST que vence precedência encerra Hard Link prévio com
                TERMINATE (S_PDU tipo 6) e notifica owner local antes de
                aceitar novo.
- CRITICA-A4: REQUEST que perde precedência sempre recebe REJECTED (S_PDU
                tipo 5) com reason apropriada (Tabela A.3.2.2.1):
                  - HIGHER_PRIORITY_LINK_EXISTING(2) para Type 1/2
                  - REQUESTED_TYPE0_EXISTS(5) para Type 0 ativo
                Tabela rank-por-remote-sap resolve a regra (1).
"""

from __future__ import annotations

import pytest

from src.cas import CasConfig
from src.modem_if import ModemConfig, ModemInterface
from src.sis import (
    encode_spdu_hard_link_request,
    encode_spdu_hard_link_terminate,
    spdu_type,
)
from src.stanag_node import StanagNode
from src.stypes import (
    CPDU,
    CPDUType,
    CasLinkState,
    DPDUType,
    LinkType,
    SisHardLinkRejectReason,
    SisHardLinkTerminateReason,
    SisLinkSessionState,
    SisUnbindIndicationReason,
    SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED,
    SPDU_TYPE_HARD_LINK_TERMINATE,
)


# -----------------------------------------------------------------------
# Stubs
# -----------------------------------------------------------------------


class _StubModem(ModemInterface):
    def __init__(self) -> None:
        super().__init__(config=ModemConfig())
        self.tx_frames: list[bytes] = []

    def modem_rx_read_frame(self):
        return None

    def modem_tx_dpdu(self, dpdu_buffer, length=None):
        self.tx_frames.append(bytes(dpdu_buffer))
        return len(dpdu_buffer)

    def modem_tx_burst(self, frames):
        self.tx_frames.extend(bytes(f) for f in frames)
        return sum(len(f) for f in frames)

    def modem_rx_start(self):
        pass

    def modem_rx_stop(self):
        pass

    def modem_get_carrier_status(self):
        return True

    def modem_set_tx_enable(self, enabled):
        pass


def _make_node(**kwargs) -> StanagNode:
    return StanagNode(local_node_address=1, modem=_StubModem(), **kwargs)


def _force_cas_made(node: StanagNode, remote_addr: int) -> None:
    """Coloca o CAS local em estado MADE com peer ``remote_addr``."""
    ctx = node.cas._ensure_ctx(remote_addr)
    ctx.state = CasLinkState.MADE
    node.cas.remote_node_address = remote_addr


# =======================================================================
# CRITICA-A1 — controle Hard Link via Expedited ARQ
# =======================================================================


class TestSendControlViaExpeditedArq:
    def test_uses_expedited_arq_when_cas_made(self):
        """Quando CAS=MADE, _send_control_expedited submete via Expedited ARQ."""
        node = _make_node()
        _force_cas_made(node, remote_addr=99)

        node._send_control_expedited(
            99, encode_spdu_hard_link_terminate(1)
        )

        assert node.expedited_arq.has_pending_tx() is True
        assert node.expedited_arq.remote_node_address == 99

    def test_falls_back_to_non_arq_when_cas_idle(self):
        """Pré-CAS, sem MADE, recorre a Expedited Non-ARQ (Tipo 8)."""
        node = _make_node()
        # CAS começa em IDLE — usar fallback.
        node._send_control_expedited(
            42, encode_spdu_hard_link_terminate(1)
        )

        assert node.expedited_arq.has_pending_tx() is False
        assert _non_arq_has_pending(node) is True

    def test_falls_back_when_dest_differs_from_cas_peer(self):
        """Se o destino divergir do peer CAS, usa Non-ARQ por segurança."""
        node = _make_node()
        _force_cas_made(node, remote_addr=99)
        node._send_control_expedited(
            123, encode_spdu_hard_link_terminate(1)
        )
        assert node.expedited_arq.has_pending_tx() is False
        assert _non_arq_has_pending(node) is True


# =======================================================================
# CRITICA-A2 — limite de Expedited Requests dispara S_UNBIND_INDICATION
# =======================================================================


class TestExpeditedLimitUnbindIndication:
    def test_unbind_indication_fires_with_reason_4(self):
        """Excedido o limite, callback unbind_indication é chamado com reason=4."""
        node = _make_node(max_expedited_per_client=2)
        node.bind(5)
        events: list[tuple[int, int]] = []
        node.register_callbacks(
            unbind_indication=lambda sap, reason: events.append((sap, int(reason))),
        )

        # Dois primeiros: dentro do limite.
        node.expedited_unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            ttl_seconds=60, updu=b"a",
        )
        node.expedited_unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            ttl_seconds=60, updu=b"b",
        )
        assert events == []
        assert 5 in node._saps

        # Terceiro excede → unbind + indicação.
        node.expedited_unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            ttl_seconds=60, updu=b"c",
        )
        assert events == [
            (5, int(SisUnbindIndicationReason.TOO_MANY_EXPEDITED_REQUESTS))
        ]
        assert 5 not in node._saps

    def test_no_indication_when_disabled(self):
        """max_expedited_per_client=0 → tracking off, nunca indica unbind."""
        node = _make_node(max_expedited_per_client=0)
        node.bind(5)
        events: list[tuple[int, int]] = []
        node.register_callbacks(
            unbind_indication=lambda sap, reason: events.append((sap, int(reason))),
        )
        for _ in range(50):
            node.expedited_unidata_request(
                sap_id=5, dest_addr=99, dest_sap=3,
                ttl_seconds=60, updu=b"x",
            )
        assert events == []
        assert 5 in node._saps

    def test_unbound_sap_rejected_without_unbind_indication(self):
        """SAP não vinculado → rejected, sem unbind_indication."""
        node = _make_node(max_expedited_per_client=2)
        rejections: list = []
        unbinds: list = []
        node.register_callbacks(
            request_rejected=lambda sap, reason: rejections.append((sap, reason)),
            unbind_indication=lambda sap, reason: unbinds.append((sap, reason)),
        )
        node.expedited_unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            ttl_seconds=60, updu=b"x",
        )
        assert len(rejections) == 1
        assert unbinds == []


# =======================================================================
# CRITICA-A3 — terminate prévio antes de aceitar novo Hard Link
# =======================================================================


def _decode_outgoing_spdus(node: StanagNode) -> list[tuple[int, bytes]]:
    """Coleta S_PDUs pendentes nas filas Non-ARQ e Expedited ARQ.

    Retorna uma lista de (dest_addr, spdu_bytes) — sempre desencapsula o
    DATA C_PDU (byte 0 = 0x00) antes de devolver.
    """
    out: list[tuple[int, bytes]] = []

    # Non-ARQ queues (expedited prioritária, depois normal).
    for q in (node.non_arq._tx_queue_expedited, node.non_arq._tx_queue_normal):
        for req in list(q):
            seg = req.payload
            if len(seg) >= 1 and seg[0] == 0x00:
                out.append((req.destination, seg[1:]))

    # Expedited ARQ queue.
    for payload in list(node.expedited_arq._tx_queue):
        if len(payload) >= 1 and payload[0] == 0x00:
            out.append((node.expedited_arq.remote_node_address or 0, payload[1:]))

    return out


def _non_arq_has_pending(node: StanagNode) -> bool:
    return bool(node.non_arq._tx_queue_expedited or node.non_arq._tx_queue_normal)


class TestTerminatePreviousHardLink:
    def _setup_active_hard_link(
        self, node: StanagNode, *, owner_sap=5, owner_rank=0,
        link_priority=1, link_type=1, remote_addr=42,
    ):
        node.bind(owner_sap, rank=owner_rank)
        node._link_session.link_type = LinkType.HARD
        node._link_session.state = SisLinkSessionState.ACTIVE
        node._link_session.hard_link_owner = owner_sap
        node._link_session.hard_link_owner_rank = owner_rank
        node._link_session.link_priority = link_priority
        node._link_session.sis_hard_link_type = link_type
        node._link_session.remote_addr = remote_addr

    def test_winning_request_terminates_previous(self):
        """Novo REQUEST vencedor: encerra link prévio (TERMINATE + callback)."""
        node = _make_node()
        self._setup_active_hard_link(
            node, owner_rank=0, link_priority=1, link_type=1,
            remote_addr=42,
        )

        terminated: list = []
        established: list = []
        node.register_callbacks(
            hard_link_terminated=lambda addr, initiator_received_confirm:
                terminated.append((addr, initiator_received_confirm)),
            hard_link_established=lambda addr, sap: established.append((addr, sap)),
        )

        # Pedido novo do peer 99 com priority maior.
        payload = encode_spdu_hard_link_request(
            link_type=1, link_priority=3, requesting_sap=0, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)

        # Owner anterior foi notificado.
        assert len(terminated) == 1
        assert terminated[0][0] == 42
        assert terminated[0][1] is False

        # E o novo foi aceito.
        assert len(established) == 1
        assert established[0][0] == 99

        # Um TERMINATE (S_PDU tipo 6) foi enviado ao peer anterior (42)
        # antes de aceitar o novo.
        outgoing = _decode_outgoing_spdus(node)
        terminate_to_42 = [
            spdu for (addr, spdu) in outgoing
            if addr == 42 and spdu_type(spdu) == SPDU_TYPE_HARD_LINK_TERMINATE
        ]
        assert len(terminate_to_42) == 1
        # Reason esperado: HIGHER_PRIORITY_LINK_REQUESTED (2).
        assert (terminate_to_42[0][0] & 0x0F) == int(
            SisHardLinkTerminateReason.HIGHER_PRIORITY_LINK_REQUESTED
        )

    def test_losing_request_does_not_terminate(self):
        """REQUEST perdedor: não encerra link prévio nem chama callback."""
        node = _make_node()
        self._setup_active_hard_link(
            node, owner_rank=0, link_priority=3, link_type=1,
            remote_addr=42,
        )
        terminated: list = []
        node.register_callbacks(
            hard_link_terminated=lambda addr, initiator_received_confirm:
                terminated.append(addr),
        )
        payload = encode_spdu_hard_link_request(
            link_type=1, link_priority=1, requesting_sap=0, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)
        assert terminated == []
        # Sessão preservada.
        assert node._link_session.state == SisLinkSessionState.ACTIVE
        assert node._link_session.remote_addr == 42


# =======================================================================
# CRITICA-A4 — REJECT explícito + tabela rank-por-remote-sap
# =======================================================================


class TestExplicitRejectAndRanks:
    def _setup_active(self, node, *, link_priority=1, link_type=1,
                      owner_rank=0, remote_addr=42):
        node.bind(5, rank=owner_rank)
        node._link_session.link_type = LinkType.HARD
        node._link_session.state = SisLinkSessionState.ACTIVE
        node._link_session.hard_link_owner = 5
        node._link_session.hard_link_owner_rank = owner_rank
        node._link_session.link_priority = link_priority
        node._link_session.sis_hard_link_type = link_type
        node._link_session.remote_addr = remote_addr

    @staticmethod
    def _last_reject_reason(node: StanagNode, peer: int) -> int | None:
        for addr, spdu in _decode_outgoing_spdus(node):
            if addr == peer and spdu_type(spdu) == SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED:
                return spdu[0] & 0x0F
        return None

    def test_loser_receives_reject_higher_priority_existing(self):
        """Perdedor com link_type≥1 ativo → reason=2."""
        node = _make_node()
        self._setup_active(node, link_priority=3, link_type=1)
        payload = encode_spdu_hard_link_request(
            link_type=1, link_priority=1, requesting_sap=0, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)
        reason = self._last_reject_reason(node, 99)
        assert reason == int(SisHardLinkRejectReason.HIGHER_PRIORITY_LINK_EXISTING)

    def test_loser_receives_reject_type0_exists(self):
        """Type 0 ativo → reason=5 (REQUESTED_TYPE0_EXISTS)."""
        node = _make_node()
        self._setup_active(node, link_priority=3, link_type=0)
        payload = encode_spdu_hard_link_request(
            link_type=0, link_priority=0, requesting_sap=0, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)
        reason = self._last_reject_reason(node, 99)
        assert reason == int(SisHardLinkRejectReason.REQUESTED_TYPE0_EXISTS)

    def test_remote_rank_table_blocks_higher_priority(self):
        """Tabela rank-remoto: existing rank=2 vs requester rank=1 → existing vence
        mesmo com priority maior do requester."""
        node = _make_node()
        self._setup_active(node, owner_rank=2, link_priority=0, link_type=0)
        node.set_remote_rank(remote_addr=99, remote_sap=0, rank=1)

        payload = encode_spdu_hard_link_request(
            link_type=2, link_priority=3, requesting_sap=0, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)
        reason = self._last_reject_reason(node, 99)
        # Existing é Type 0 → reason=5.
        assert reason == int(SisHardLinkRejectReason.REQUESTED_TYPE0_EXISTS)

    def test_remote_rank_table_promotes_winner(self):
        """Tabela rank-remoto: requester rank=10 supera existing rank=0
        e novo é aceito mesmo com priority menor."""
        node = _make_node()
        self._setup_active(node, owner_rank=0, link_priority=3, link_type=1,
                           remote_addr=42)
        node.set_remote_rank(remote_addr=99, remote_sap=0, rank=10)
        established: list = []
        node.register_callbacks(
            hard_link_established=lambda addr, sap: established.append((addr, sap)),
        )

        payload = encode_spdu_hard_link_request(
            link_type=0, link_priority=0, requesting_sap=0, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)
        assert len(established) == 1
        assert established[0][0] == 99

    def test_default_remote_rank_setter_validates_range(self):
        node = _make_node()
        with pytest.raises(ValueError):
            node.set_default_remote_rank(99)
        with pytest.raises(ValueError):
            node.set_remote_rank(0, 0, 16)
