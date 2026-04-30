"""Tests for Sprint 3 — ALTAS pendentes (Anexos A, B, C).

Cobre:
- ALTA-A1: bind valida rank 0-15; rank=15 requer allow_management_rank=True.
- ALTA-A2: hard_link_establish satura link_priority em 0-3 (2 bits).
- ALTA-A3: hard_link_terminate só aceita o SAP que iniciou o link.
- ALTA-A4: encode_spdu_data_delivery_{confirm,fail}_from copia campos S_PCI.
- ALTA-B1: CASEngine.send_data(use_arq=True) chama arq_data_handler.
- ALTA-C4: EOW Type 7 (HDR Change Request) — codec 12 bits + Extended Msg
            field de 6 bytes (Tabelas C-9-1 / C-9-2 / C-9-4).
"""

from __future__ import annotations

import pytest

from src.cas import CASEngine
from src.eow import (
    HDR_EXTENDED_MESSAGE_SIZE,
    EOWType,
    HDRWaveform,
    build_eow_hdr_change_request,
    build_hdr_extended_message,
    is_eow_hdr_change_request,
    parse_eow_hdr_change_request,
    parse_hdr_extended_message,
)
from src.modem_if import ModemConfig, ModemInterface
from src.non_arq import NonArqEngine
from src.sis import (
    SPDU_TYPE_DATA_DELIVERY_CONFIRM,
    SPDU_TYPE_DATA_DELIVERY_FAIL,
    decode_spdu_data_delivery_confirm,
    decode_spdu_data_delivery_confirm_full,
    decode_spdu_data_delivery_fail,
    decode_spdu_data_delivery_fail_full,
    encode_spdu_data_delivery_confirm_from,
    encode_spdu_data_delivery_fail_from,
    encode_spdu_hard_link_request,
)
from src.stanag_node import StanagNode
from src.stypes import (
    SPDU,
    LinkType,
    SisLinkSessionState,
    TxMode,
)


class _StubModem(ModemInterface):
    def __init__(self):
        super().__init__(config=ModemConfig())

    def modem_rx_read_frame(self):
        return None

    def modem_tx_dpdu(self, dpdu_buffer, length=None):
        return len(dpdu_buffer)

    def modem_tx_burst(self, frames):
        return sum(len(f) for f in frames)

    def modem_rx_start(self): pass
    def modem_rx_stop(self): pass
    def modem_get_carrier_status(self): return True
    def modem_set_tx_enable(self, enabled): pass


def _make_node(**kwargs) -> StanagNode:
    return StanagNode(local_node_address=1, modem=_StubModem(), **kwargs)


# =======================================================================
# ALTA-A1 — Rank 0-15 + autorização para rank 15
# =======================================================================


class TestBindRankValidation:
    def test_rank_negative_rejected(self):
        node = _make_node()
        with pytest.raises(ValueError):
            node.bind(5, rank=-1)

    def test_rank_above_15_rejected(self):
        node = _make_node()
        with pytest.raises(ValueError):
            node.bind(5, rank=16)

    def test_rank_15_requires_authorization(self):
        node = _make_node()  # allow_management_rank=False default
        with pytest.raises(ValueError):
            node.bind(5, rank=15)
        assert 5 not in node._saps

    def test_rank_15_accepted_when_authorized(self):
        node = _make_node(allow_management_rank=True)
        node.bind(5, rank=15)
        assert node._saps[5].rank == 15

    def test_rank_below_15_does_not_need_authorization(self):
        node = _make_node()
        node.bind(5, rank=14)
        assert node._saps[5].rank == 14

    def test_bind_rejected_callback_used_when_set(self):
        node = _make_node()
        rejections: list = []
        node.register_callbacks(bind_rejected=lambda r: rejections.append(r))
        # rank inválido NÃO levanta quando callback registrado
        result = node.bind(5, rank=99)
        assert result == -1
        assert len(rejections) == 1


# =======================================================================
# ALTA-A2 — Link Priority limitado a 0-3
# =======================================================================


class TestLinkPrioritySaturation:
    def test_priority_clamped_to_3(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.hard_link_establish(
            sap_id=5, link_priority=15,
            remote_addr=99, remote_sap=3, link_type=0,
        )
        assert node._link_session.link_priority == 3

    def test_priority_negative_clamped_to_0(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.hard_link_establish(
            sap_id=5, link_priority=-2,
            remote_addr=99, remote_sap=3, link_type=0,
        )
        assert node._link_session.link_priority == 0

    def test_priority_in_range_preserved(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.hard_link_establish(
            sap_id=5, link_priority=2,
            remote_addr=99, remote_sap=3, link_type=0,
        )
        assert node._link_session.link_priority == 2


# =======================================================================
# ALTA-A3 — Terminate só pelo originador local
# =======================================================================


class TestHardLinkTerminateByOwner:
    def test_initiator_can_terminate(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.hard_link_establish(
            sap_id=5, link_priority=1,
            remote_addr=99, remote_sap=3, link_type=0,
        )
        node._link_session.state = SisLinkSessionState.ACTIVE
        node.hard_link_terminate(sap_id=5, remote_addr=99)
        assert node._link_session.state == SisLinkSessionState.TERMINATING

    def test_other_local_sap_cannot_terminate(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.bind(6, rank=0)
        node.hard_link_establish(
            sap_id=5, link_priority=1,
            remote_addr=99, remote_sap=3, link_type=0,
        )
        node._link_session.state = SisLinkSessionState.ACTIVE
        node.hard_link_terminate(sap_id=6, remote_addr=99)
        # Sessão não muda — SAP 6 não é o originador.
        assert node._link_session.state == SisLinkSessionState.ACTIVE
        assert node._link_session.awaiting_terminate_confirm is False

    def test_called_node_cannot_terminate_locally(self):
        """Lado solicitado em Type 0/1 (sem initiator local) não permite
        terminate de nenhum SAP local (A.2.1.12 §2)."""
        node = _make_node()
        node.bind(5, rank=0)
        # Simula recepção de Type 0 REQUEST do peer 99 (remote_sap=3).
        payload = encode_spdu_hard_link_request(
            link_type=0, link_priority=1, requesting_sap=3, remote_sap=5,
        )
        node._process_spdu_control(payload, src_addr=99)
        assert node._link_session.state == SisLinkSessionState.ACTIVE
        assert node._link_session.local_initiator_sap == -1
        # Nenhum SAP local pode terminar: só TERMINATE remoto encerra.
        node.hard_link_terminate(sap_id=5, remote_addr=99)
        assert node._link_session.state == SisLinkSessionState.ACTIVE


# =======================================================================
# ALTA-A4 — DELIVERY CONFIRM/FAIL copia campos S_PCI
# =======================================================================


class TestDeliveryConfirmCopiesPci:
    def _make_data_spdu(self) -> SPDU:
        # TTD: dia 100 do ano, 12:34 GMT (2 segundos resolution).
        return SPDU(
            version=1,
            src_sap=3,
            dest_sap=5,
            priority=11,
            ttd=86400 * 99 + 12 * 3600 + 34 * 60,  # offset desde epoch
            tx_mode=int(TxMode.ARQ),
            client_delivery_confirm_required=True,
            updu=b"hello",
        )

    def test_confirm_copies_priority(self):
        spdu = self._make_data_spdu()
        encoded = encode_spdu_data_delivery_confirm_from(spdu, b"hel")
        assert (encoded[0] >> 4) & 0x0F == SPDU_TYPE_DATA_DELIVERY_CONFIRM
        assert encoded[0] & 0x0F == 11  # priority preservada

    def test_confirm_full_decodes_priority_and_flags(self):
        spdu = self._make_data_spdu()
        encoded = encode_spdu_data_delivery_confirm_from(spdu, b"hel")
        decoded = decode_spdu_data_delivery_confirm_full(encoded)
        assert decoded["priority"] == 11
        assert decoded["src_sap"] == 3
        assert decoded["dest_sap"] == 5
        assert decoded["delivery_confirm"] == 1
        assert decoded["valid_ttd"] == 1
        assert decoded["updu_partial"] == b"hel"

    def test_legacy_decode_still_works(self):
        spdu = self._make_data_spdu()
        encoded = encode_spdu_data_delivery_confirm_from(spdu, b"hel")
        src_sap, dest_sap, partial = decode_spdu_data_delivery_confirm(encoded)
        assert (src_sap, dest_sap) == (3, 5)
        assert partial == b"hel"

    def test_fail_copies_priority_and_carries_reason(self):
        spdu = self._make_data_spdu()
        encoded = encode_spdu_data_delivery_fail_from(spdu, reason=2, updu_partial=b"bye")
        assert (encoded[0] >> 4) & 0x0F == SPDU_TYPE_DATA_DELIVERY_FAIL
        assert encoded[0] & 0x0F == 11
        decoded = decode_spdu_data_delivery_fail_full(encoded)
        assert decoded["reason"] == 2
        assert decoded["priority"] == 11
        assert decoded["updu_partial"] == b"bye"

    def test_fail_legacy_decode_still_works(self):
        spdu = self._make_data_spdu()
        encoded = encode_spdu_data_delivery_fail_from(spdu, reason=2, updu_partial=b"bye")
        src_sap, dest_sap, reason, partial = decode_spdu_data_delivery_fail(encoded)
        assert (src_sap, dest_sap, reason) == (3, 5, 2)
        assert partial == b"bye"

    def test_priority_zero_when_no_original(self):
        """Sem campo S_PCI propagado, priority encoded é 0."""
        from src.sis import encode_spdu_data_delivery_confirm
        encoded = encode_spdu_data_delivery_confirm(3, 5, b"x")
        assert encoded[0] & 0x0F == 0


# =======================================================================
# ALTA-B1 — CASEngine.send_data(use_arq=True)
# =======================================================================


class _FakeNonArq(NonArqEngine):
    def __init__(self):
        super().__init__(local_node_address=1, modem=_StubModem())


class TestCasSendDataArq:
    def _make_cas(self, handler=None):
        non_arq = _FakeNonArq()
        cas = CASEngine(
            local_node_address=1,
            non_arq=non_arq,
            arq_data_handler=handler,
        )
        # Força link MADE com peer 42.
        from src.cas import _LinkContext
        from src.stypes import CasLinkState
        ctx = _LinkContext(remote_address=42)
        ctx.state = CasLinkState.MADE
        cas._links[42] = ctx
        cas._primary_remote = 42
        return cas

    def test_send_data_arq_calls_handler(self):
        calls: list[tuple[int, bytes]] = []
        cas = self._make_cas(handler=lambda dest, encoded: calls.append((dest, encoded)))
        cas.send_data(b"payload", use_arq=True)
        assert len(calls) == 1
        dest, encoded = calls[0]
        assert dest == 42
        # encoded começa com 0x00 (DATA C_PDU).
        assert encoded[0] == 0x00
        assert encoded[1:] == b"payload"

    def test_send_data_arq_without_handler_raises(self):
        cas = self._make_cas(handler=None)
        with pytest.raises(RuntimeError):
            cas.send_data(b"payload", use_arq=True)

    def test_send_data_non_arq_unchanged(self):
        """Sem use_arq, mantém comportamento legado (Non-ARQ)."""
        cas = self._make_cas()
        cas.send_data(b"payload")
        # Verifica que enfileirou no Non-ARQ.
        assert cas.non_arq._tx_queue_normal or cas.non_arq._tx_queue_expedited

    def test_stanag_node_wires_arq_handler(self):
        """StanagNode injeta seu próprio handler no construtor."""
        node = _make_node()
        assert node.cas.arq_data_handler is not None


# =======================================================================
# ALTA-C4 — EOW Type 7 HDR Change Request
# =======================================================================


class TestEowType7HdrChange:
    def test_build_and_parse_roundtrip(self):
        eow = build_eow_hdr_change_request(
            waveform=HDRWaveform.STANAG_4539,
            number_of_channels=4,
        )
        # 12 bits — não excede 0xFFF.
        assert 0 <= eow <= 0xFFF
        parsed = parse_eow_hdr_change_request(eow)
        assert parsed.waveform == int(HDRWaveform.STANAG_4539)
        assert parsed.number_of_channels == 4

    def test_8_channels_encoded_as_zero(self):
        """C.5.5 §5: 8 channels → wire value 000."""
        eow = build_eow_hdr_change_request(
            waveform=HDRWaveform.MS110A,
            number_of_channels=8,
        )
        assert (eow & 0x07) == 0
        parsed = parse_eow_hdr_change_request(eow)
        assert parsed.number_of_channels == 8

    def test_type_field_in_msb(self):
        """Tabela C-9-1: TYPE = 7 está em bits 11-8 (high nibble)."""
        eow = build_eow_hdr_change_request(
            waveform=HDRWaveform.MS110A, number_of_channels=1,
        )
        assert (eow >> 8) & 0x0F == int(EOWType.HDR_CHANGE_REQUEST)
        assert is_eow_hdr_change_request(eow)

    def test_invalid_waveform_rejected(self):
        with pytest.raises(ValueError):
            build_eow_hdr_change_request(
                waveform=32, number_of_channels=1,
            )

    def test_invalid_channels_rejected(self):
        with pytest.raises(ValueError):
            build_eow_hdr_change_request(
                waveform=HDRWaveform.MS110A, number_of_channels=0,
            )
        with pytest.raises(ValueError):
            build_eow_hdr_change_request(
                waveform=HDRWaveform.MS110A, number_of_channels=9,
            )

    def test_parse_rejects_non_type7(self):
        # EOW Type 1 (DRC Request) — não é Type 7.
        from src.eow import build_eow_drc
        eow = build_eow_drc(data_rate_code=2)
        assert not is_eow_hdr_change_request(eow)
        with pytest.raises(ValueError):
            parse_eow_hdr_change_request(eow)

    def test_extended_message_roundtrip(self):
        payload = build_hdr_extended_message(
            data_rate_bps=12800,
            interleaver_centiseconds=480,  # 4.8s
        )
        assert len(payload) == HDR_EXTENDED_MESSAGE_SIZE
        # MSB at offset 0 (Tabela C-9-4).
        assert int.from_bytes(payload[:4], "big") == 12800
        assert int.from_bytes(payload[4:6], "big") == 480
        parsed = parse_hdr_extended_message(payload)
        assert parsed.data_rate_bps == 12800
        assert parsed.interleaver_centiseconds == 480

    def test_extended_message_size_validation(self):
        with pytest.raises(ValueError):
            parse_hdr_extended_message(b"\x00" * 5)
        with pytest.raises(ValueError):
            parse_hdr_extended_message(b"\x00" * 7)
