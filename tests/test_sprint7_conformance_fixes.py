"""Tests for Sprint 7 — conformidades resolvidas a partir das figuras da norma.

Cobre:
- MÉDIA-C1: posição do campo TYPE do EOW. Fig C-37 / C-38 e o texto C.5 §2
            ("the first 4 bits of the EOW shall contain the EOW-type field")
            colocam TYPE nos bits 11-8 (4 MSB) e o conteúdo nos bits 7-0.
            Antes, Types 1-3 usavam o nibble baixo (inconsistente com o Type 7,
            que já era correto desde a Sprint 3).
- MÉDIA-A1: layout de SERVICE_TYPE / DELIVERY_MODE. Fig A-3 (idêntica à
            Fig A-29) define byte 0 = TX_MODE[7:4] | CONFIRM[3:2] | ORDER[1] |
            EXT[0] e byte 1 (Non-ARQ) = MIN. No OF RETXS[7:4]. Antes o
            transmission mode era tratado como 2 bits e o min-retx ia para a
            posição errada.
- Limitação conhecida (pós-Sprint 6): na tomada de hard link por precedência,
            os SAPs afetados agora recebem hard_link_terminated_per_sap
            (A.3.2.2.3 §3), não só o callback global.
"""

from __future__ import annotations

import pytest

from src.eow import (
    EOWType,
    build_eow_capability,
    build_eow_drc,
    build_eow_drc_response,
    build_eow_hdr_change_request,
    build_eow_unrecognized,
    is_eow_hdr_change_request,
    parse_eow,
)
from src.dpdu_frame import (
    build_management,
    decode_dpdu,
    dpdu_set_address,
    encode_dpdu,
)
from src.s_primitive_codec import (
    decode_delivery_mode,
    decode_service_type,
    encode_delivery_mode,
    encode_service_type,
)
from src.cas import CasConfig
from src.modem_if import ModemConfig, ModemInterface
from src.sis import encode_spdu_hard_link_request
from src.stanag_node import StanagNode
from src.stypes import LinkType, SisLinkSessionState


# =======================================================================
# Helpers
# =======================================================================

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


def _addr(dest=1, src=2):
    return dpdu_set_address(destination=dest, source=src)


# =======================================================================
# MÉDIA-C1 — EOW TYPE field nos bits 11-8 (Fig C-37 / C-38)
# =======================================================================

class TestEowTypeFieldPosition:
    def test_drc_request_type_in_high_nibble(self):
        # Fig C-38: TYPE=0001 em [11:8]; Data Rate [7:4]; Interleave [3:2].
        eow = build_eow_drc(data_rate_code=5, interleave_mode=1)
        assert (eow >> 8) & 0x0F == int(EOWType.DRC_REQUEST)
        assert eow & 0xFF == (5 << 4) | (1 << 2)
        # Valor completo de 12 bits: 0x154 (TYPE=1 em [11:8], content=0x54).
        assert eow == 0x154

    def test_drc_request_roundtrip(self):
        eow = build_eow_drc(data_rate_code=5, interleave_mode=2)
        parsed = parse_eow(eow)
        assert parsed.msg_type == int(EOWType.DRC_REQUEST)
        assert parsed.drc_request is not None
        assert parsed.drc_request.data_rate_code == 5
        assert parsed.drc_request.interleave_mode == 2

    def test_drc_response_type_position(self):
        eow = build_eow_drc_response(response=1, reason=2)
        assert (eow >> 8) & 0x0F == int(EOWType.DRC_RESPONSE)
        parsed = parse_eow(eow)
        assert parsed.drc_response is not None
        assert parsed.drc_response.response == 1
        assert parsed.drc_response.reason == 2

    def test_unrecognized_type_position(self):
        eow = build_eow_unrecognized(5)
        assert (eow >> 8) & 0x0F == int(EOWType.UNRECOGNIZED_TYPE)
        parsed = parse_eow(eow)
        assert parsed.unrecognized_type == 5

    def test_capability_type_position(self):
        eow = build_eow_capability(0xA5)
        assert (eow >> 8) & 0x0F == int(EOWType.CAPABILITY_ADVERTISEMENT)
        parsed = parse_eow(eow)
        assert parsed.capability_bitmap == 0xA5

    def test_consistent_with_type7(self):
        # Type 7 (HDR Change) já usava [11:8] desde a Sprint 3; os demais
        # tipos agora seguem a mesma convenção.
        t1 = build_eow_drc(data_rate_code=2)
        t7 = build_eow_hdr_change_request(waveform=2, number_of_channels=4)
        assert (t1 >> 8) & 0x0F == int(EOWType.DRC_REQUEST)
        assert (t7 >> 8) & 0x0F == int(EOWType.HDR_CHANGE_REQUEST)
        assert not is_eow_hdr_change_request(t1)
        assert is_eow_hdr_change_request(t7)

    def test_all_eow_within_12_bits(self):
        for eow in (
            build_eow_drc(11, interleave_mode=3),
            build_eow_drc_response(3, 31),
            build_eow_unrecognized(15),
            build_eow_capability(0xFF),
        ):
            assert 0 <= eow <= 0xFFF

    def test_management_dpdu_roundtrip(self):
        # MGMT (Type 6) D_PDU: o campo EOW também segue Fig C-37.
        dpdu = build_management(
            0, 0, _addr(dest=1, src=2),
            msg_type=int(EOWType.DRC_REQUEST),
            message_contents=0x54,
            data=b"",
        )
        assert (dpdu.management.message_field >> 8) & 0x0F == int(EOWType.DRC_REQUEST)
        assert dpdu.management.message_field & 0xFF == 0x54
        dec = decode_dpdu(encode_dpdu(dpdu))
        assert dec.management.msg_type == int(EOWType.DRC_REQUEST)
        assert dec.management.message_contents == 0x54


# =======================================================================
# MÉDIA-A1 — layout SERVICE_TYPE / DELIVERY_MODE (Fig A-3 / A-29)
# =======================================================================

class TestServiceTypeLayout:
    def test_vector_fig_a3(self):
        # (tm=2, dc=1, order=True, ext=False, mr=5) -> bytes 0x26 0x50.
        encoded = encode_service_type(2, 1, True, False, 5)
        assert encoded == bytes([0x26, 0x50])

    def test_transmission_mode_is_4_bits(self):
        # Non-ARQ-with-errors (3) e até 4 bits cabem no high nibble do byte 0.
        for tm in range(0, 16):
            d = decode_service_type(encode_service_type(tm, 0, False, False, 0))
            assert d['transmission_mode'] == tm

    def test_min_retransmissions_in_byte1(self):
        for mr in range(0, 16):
            encoded = encode_service_type(2, 0, False, False, mr)
            assert (encoded[1] >> 4) & 0x0F == mr
            assert decode_service_type(encoded)['min_retransmissions'] == mr

    def test_roundtrip(self):
        for tm, dc, do_, ext, mr in [(0, 0, False, False, 0),
                                     (3, 3, True, True, 15),
                                     (1, 2, False, True, 7)]:
            d = decode_service_type(encode_service_type(tm, dc, do_, ext, mr))
            assert (d['transmission_mode'], d['delivery_confirmation'],
                    d['delivery_order'], d['extended'],
                    d['min_retransmissions']) == (tm, dc, do_, ext, mr)


class TestDeliveryModeLayout:
    def test_byte0_matches_service_type(self):
        # Fig A-29 (Delivery Mode) é idêntica à Fig A-3 (Service Type).
        dm = encode_delivery_mode(tx_mode=2, delivery_confirm=1,
                                  delivery_order=True, ext=False)
        st = encode_service_type(2, 1, True, False, 0)
        assert dm[0] == st[0] == 0x26

    def test_default_single_byte(self):
        assert len(encode_delivery_mode(tx_mode=1)) == 1

    def test_optional_min_retx_second_byte(self):
        dm = encode_delivery_mode(tx_mode=2, min_retransmissions=5)
        assert len(dm) == 2
        assert (dm[1] >> 4) & 0x0F == 5
        d = decode_delivery_mode(dm, with_min_retx=True)
        assert d['tx_mode'] == 2
        assert d['min_retransmissions'] == 5

    def test_decode_without_min_retx_unchanged(self):
        d = decode_delivery_mode(encode_delivery_mode(0))
        assert 'min_retransmissions' not in d
        assert d == {'tx_mode': 0, 'delivery_confirm': 0,
                     'delivery_order': False, 'extension': False}


# =======================================================================
# Limitação conhecida — hard_link_terminated_per_sap na tomada por precedência
# =======================================================================

class TestHardLinkTerminatedPerSapOnTakeover:
    def _setup_active_hard_link(self, node, *, owner_sap, link_type, remote_addr=42):
        node.bind(owner_sap, rank=0)
        node._link_session.link_type = LinkType.HARD
        node._link_session.state = SisLinkSessionState.ACTIVE
        node._link_session.hard_link_owner = owner_sap
        node._link_session.hard_link_owner_rank = 0
        node._link_session.link_priority = 1
        node._link_session.sis_hard_link_type = link_type
        node._link_session.local_initiator_sap = owner_sap
        node._link_session.remote_addr = remote_addr

    def test_per_sap_fired_on_type1_takeover(self):
        node = _make_node()
        self._setup_active_hard_link(node, owner_sap=5, link_type=1, remote_addr=42)

        per_sap: list = []
        node.register_callbacks(
            hard_link_terminated_per_sap=lambda sap_id, addr, conf:
                per_sap.append((sap_id, addr, conf)),
        )

        # REQUEST vencedor (priority maior) do peer 99.
        payload = encode_spdu_hard_link_request(
            link_type=1, link_priority=3, requesting_sap=0, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)

        # O SAP iniciador local (5) foi notificado granularmente.
        assert (5, 42, False) in per_sap

    def test_per_sap_fired_for_all_saps_on_type0_takeover(self):
        node = _make_node()
        node.bind(3, rank=0)
        self._setup_active_hard_link(node, owner_sap=4, link_type=0, remote_addr=42)

        per_sap: list = []
        node.register_callbacks(
            hard_link_terminated_per_sap=lambda sap_id, addr, conf:
                per_sap.append(sap_id),
        )
        payload = encode_spdu_hard_link_request(
            link_type=1, link_priority=3, requesting_sap=0, remote_sap=4,
        )
        node._process_spdu_control(payload, src_addr=99)

        # Type 0: todos os SAPs locais bound são notificados.
        assert 3 in per_sap and 4 in per_sap
