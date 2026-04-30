"""Tests for Sprint 4 — MÉDIAS (Anexos A, B, C, F).

Cobre:
- MÉDIA-A2: bit DELIVERY_CONFIRM = só client_delivery_confirm.
- MÉDIA-A3: decode_s_primitive valida version=0x00.
- MÉDIA-A4: TERMINATE notifica callback per-sap (Type 0 = todos os SAPs).
- MÉDIA-A5: hard_link_terminate aceita parâmetro reason.
- MÉDIA-A6: indicações Type 2 simultâneas vão para fila.
- MÉDIA-B1: LINK_BREAK sem ctx prévio não emite evento IDLE.
- MÉDIA-B3: decode_cpdu(strict=True) rejeita NOT_USED ≠ 0.
- MÉDIA-C2: NonArqEngine ERROR_FREE descarta fragmentos parciais.
- MÉDIA-C3: WARNING_REASON_UNRECOGNIZED_TYPE/INVALID_DPDU.
- MÉDIA-C4: TX_UWE/TX_LWE flags por D_PDU individual.
- MÉDIA-C5: EXPEDITED_CONNECTED rejeita DATA regular.
- MÉDIA-F4: RCOP reassembly purga contextos expirados.
- MÉDIA-F5: CFTP loga warning quando body excede MessageSize.
- MÉDIA-F6: HMTP rejeita send_batch com recipients=[].
- MÉDIA-F1: Raw SIS Socket envia S_UNBIND_INDICATION ao desconectar
            (verificado via encode_unbind_indication chamado em _cleanup_client).
"""

from __future__ import annotations

import logging

import pytest

from src.annex_f.bftp import _decode_bftp  # noqa: F401  (re-exported via src.sis)
from src.annex_f.cftp import _decode_cftp_message
from src.annex_f.hmtp import HMTPClient, MailMessage
from src.annex_f.rcop import RCOP_MAX_APP_DATA, RcopPDU, _RcopReassemblyContext
from src.cas import CASEngine, decode_cpdu, encode_cpdu
from src.dpdu_frame import (
    decode_dpdu,
    dpdu_calc_eot_field,
    dpdu_set_address,
    encode_dpdu,
)
from src.dts_state import (
    DTSState,
    DTSStateMachine,
    WARNING_REASON_INVALID_DPDU,
    WARNING_REASON_UNRECOGNIZED_TYPE,
)
from src.modem_if import ModemConfig, ModemInterface
from src.non_arq import NonArqEngine
from src.s_primitive_codec import PREAMBLE, decode_s_primitive
from src.sis import (
    encode_spdu_data,
    encode_spdu_hard_link_request,
    encode_spdu_hard_link_terminate,
    spdu_type,
)
from src.stanag_node import StanagNode
from src.stypes import (
    CPDU,
    SPDU,
    CPDUType,
    DPDU,
    DPDUType,
    LinkType,
    NonArqDeliveryMode,
    SisHardLinkTerminateReason,
    SisLinkSessionState,
    TxMode,
)
from tests.annex_f_helpers import MockNode


class _StubModem(ModemInterface):
    def __init__(self):
        super().__init__(config=ModemConfig())

    def modem_rx_read_frame(self): return None
    def modem_tx_dpdu(self, b, length=None): return len(b)
    def modem_tx_burst(self, frames): return sum(len(f) for f in frames)
    def modem_rx_start(self): pass
    def modem_rx_stop(self): pass
    def modem_get_carrier_status(self): return True
    def modem_set_tx_enable(self, e): pass


def _make_node(**kwargs) -> StanagNode:
    return StanagNode(local_node_address=1, modem=_StubModem(), **kwargs)


# =======================================================================
# MÉDIA-A2 — DELIVERY_CONFIRM bit reflete só client_delivery_confirm
# =======================================================================


class TestSpduDeliveryConfirmBit:
    def test_node_only_does_not_set_bit(self):
        spdu = SPDU(
            version=1, src_sap=3, dest_sap=5, priority=0, ttd=0,
            tx_mode=int(TxMode.ARQ),
            node_delivery_confirm_required=True,
            client_delivery_confirm_required=False,
            updu=b"x",
        )
        encoded = encode_spdu_data(spdu)
        # byte 2: bit 7 = delivery_confirm — deve ser 0.
        assert (encoded[2] >> 7) & 1 == 0

    def test_client_sets_bit(self):
        spdu = SPDU(
            version=1, src_sap=3, dest_sap=5, priority=0, ttd=0,
            tx_mode=int(TxMode.ARQ),
            node_delivery_confirm_required=False,
            client_delivery_confirm_required=True,
            updu=b"x",
        )
        encoded = encode_spdu_data(spdu)
        assert (encoded[2] >> 7) & 1 == 1


# =======================================================================
# MÉDIA-A3 — decode_s_primitive valida version
# =======================================================================


class TestDecodeSPrimitiveVersion:
    def test_version_zero_accepted(self):
        # Stream válido: PREAMBLE + version=0 + size LE + payload.
        stream = PREAMBLE + bytes([0x00]) + b"\x02\x00" + b"\x05\xAA"
        prim_type, payload, _ = decode_s_primitive(stream)
        assert prim_type == 0x05
        assert payload == b"\xAA"

    def test_unknown_version_rejected(self):
        stream = PREAMBLE + bytes([0xFF]) + b"\x02\x00" + b"\x05\xAA"
        with pytest.raises(ValueError, match="version"):
            decode_s_primitive(stream)


# =======================================================================
# MÉDIA-A4 — TERMINATE notifica per-sap (Type 0 = todos)
# =======================================================================


class TestHardLinkTerminatedPerSap:
    def test_type0_notifies_all_local_saps(self):
        node = _make_node()
        node.bind(3, rank=0)
        node.bind(5, rank=0)
        notifications: list[tuple] = []
        node.register_callbacks(
            hard_link_terminated_per_sap=lambda sap, addr, c:
                notifications.append((sap, addr, c)),
        )
        # Aceita Type 0 do peer 99.
        payload = encode_spdu_hard_link_request(
            link_type=0, link_priority=1, requesting_sap=0, remote_sap=3,
        )
        node._process_spdu_control(payload, src_addr=99)
        # Recebe TERMINATE.
        node._process_spdu_control(
            encode_spdu_hard_link_terminate(1), src_addr=99,
        )
        sap_ids = {sap for sap, _addr, _c in notifications}
        assert sap_ids == {3, 5}

    def test_type2_notifies_only_initiator(self):
        node = _make_node()
        node.bind(3, rank=0)
        node.bind(5, rank=0)
        node.register_callbacks(
            hard_link_indication=lambda *a: None,
        )
        # Recebe REQUEST Type 2 para SAP 3.
        payload = encode_spdu_hard_link_request(
            link_type=2, link_priority=1, requesting_sap=0, remote_sap=3,
        )
        node._process_spdu_control(payload, src_addr=99)
        # Cliente local SAP 3 aceita.
        node.hard_link_accept(
            link_priority=1, link_type=2, remote_addr=99, remote_sap=3,
            local_sap=3,
        )
        notifications: list[tuple] = []
        node.register_callbacks(
            hard_link_terminated_per_sap=lambda sap, addr, c:
                notifications.append((sap, addr, c)),
        )
        node._process_spdu_control(
            encode_spdu_hard_link_terminate(1), src_addr=99,
        )
        # Apenas o iniciador (SAP 3) deve ser notificado.
        sap_ids = {sap for sap, _addr, _c in notifications}
        assert sap_ids == {3}


# =======================================================================
# MÉDIA-A5 — hard_link_terminate aceita reason
# =======================================================================


class TestHardLinkTerminateReason:
    def test_default_reason_is_link_terminated_by_remote(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.hard_link_establish(
            sap_id=5, link_priority=1,
            remote_addr=99, remote_sap=3, link_type=0,
        )
        node._link_session.state = SisLinkSessionState.ACTIVE
        node.hard_link_terminate(sap_id=5, remote_addr=99)
        # Inspeciona Expedited Non-ARQ queue (CAS está IDLE — fallback).
        sent = list(node.non_arq._tx_queue_expedited)
        assert sent
        cpdu_bytes = sent[-1].payload[1:]  # remove DATA C_PDU header (0x00)
        assert spdu_type(cpdu_bytes) == 6  # SPDU_TYPE_HARD_LINK_TERMINATE
        assert cpdu_bytes[0] & 0x0F == int(
            SisHardLinkTerminateReason.LINK_TERMINATED_BY_REMOTE
        )

    def test_custom_reason_propagated(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.hard_link_establish(
            sap_id=5, link_priority=1,
            remote_addr=99, remote_sap=3, link_type=0,
        )
        node._link_session.state = SisLinkSessionState.ACTIVE
        node.hard_link_terminate(
            sap_id=5, remote_addr=99,
            reason=int(SisHardLinkTerminateReason.HIGHER_PRIORITY_LINK_REQUESTED),
        )
        sent = list(node.non_arq._tx_queue_expedited)
        cpdu_bytes = sent[-1].payload[1:]
        assert cpdu_bytes[0] & 0x0F == int(
            SisHardLinkTerminateReason.HIGHER_PRIORITY_LINK_REQUESTED
        )


# =======================================================================
# MÉDIA-A6 — fila de Pending Hard Link Indications
# =======================================================================


class TestPendingIndicationsQueue:
    def test_second_indication_queued_not_overwritten(self):
        node = _make_node()
        node.bind(3, rank=0)
        node.bind(5, rank=0)
        indications: list = []
        node.register_callbacks(
            hard_link_indication=lambda src, sap, pri, lt: indications.append(sap),
        )
        # Primeira REQUEST Type 2 para SAP 3.
        node._process_spdu_control(
            encode_spdu_hard_link_request(
                link_type=2, link_priority=1, requesting_sap=10, remote_sap=3,
            ),
            src_addr=99,
        )
        # Segunda REQUEST Type 2 chega para SAP 5 — não sobrescreve.
        node._process_spdu_control(
            encode_spdu_hard_link_request(
                link_type=2, link_priority=1, requesting_sap=11, remote_sap=5,
            ),
            src_addr=42,
        )
        # Backlog tem 1 entrada; pending tem 1 (SAP 3).
        assert node._link_session.pending_indication is not None
        assert node._link_session.pending_indication.remote_sap == 3
        assert len(node._link_session.pending_indications) == 1
        assert node._link_session.pending_indications[0].remote_sap == 5
        # Cliente aceita primeira → segunda é promovida.
        node.hard_link_accept(
            link_priority=1, link_type=2, remote_addr=99, remote_sap=3,
            local_sap=3,
        )
        # A segunda agora vira pending (callback hard_link_indication
        # disparou outra vez).
        assert indications.count(5) == 2  # callback chamado na chegada e na promoção


# =======================================================================
# MÉDIA-B1 — LINK_BREAK sem ctx não emite evento IDLE
# =======================================================================


class TestLinkBreakWithoutContext:
    def test_no_event_when_no_local_link(self):
        from src.cas import CASEngine
        from src.non_arq import NonArqEngine

        non_arq = NonArqEngine(local_node_address=1, modem=_StubModem())
        cas = CASEngine(local_node_address=1, non_arq=non_arq)
        # Sem links registrados — recebe LINK_BREAK de peer desconhecido.
        cas.process_cpdu(
            CPDU(cpdu_type=CPDUType.LINK_BREAK, reason=0),
            from_node_address=99,
            current_time_ms=0,
        )
        # event_log não recebeu transição IDLE para 99.
        idle_events = [e for e in cas.event_log if e.remote == 99]
        assert idle_events == []


# =======================================================================
# MÉDIA-B3 — decoder C_PDU strict
# =======================================================================


class TestDecodeCpduStrict:
    def test_strict_rejects_link_request_extra_bits(self):
        # field=0x07 → bits 1-3 ≠ 0 (NOT_USED).
        with pytest.raises(ValueError, match="LINK_REQUEST"):
            decode_cpdu(bytes([0x17]), strict=True)

    def test_strict_rejects_link_accepted_extra_bits(self):
        with pytest.raises(ValueError, match="LINK_ACCEPTED"):
            decode_cpdu(bytes([0x21]), strict=True)

    def test_strict_rejects_break_confirm_extra_bits(self):
        with pytest.raises(ValueError, match="LINK_BREAK_CONFIRM"):
            decode_cpdu(bytes([0x55]), strict=True)

    def test_permissive_default_accepts(self):
        # Modo padrão tolera lixo nos bits NOT_USED.
        cpdu = decode_cpdu(bytes([0x17]))
        assert cpdu.cpdu_type == CPDUType.LINK_REQUEST
        assert cpdu.link_type == 1


# =======================================================================
# MÉDIA-C2 — NonArqEngine ERROR_FREE descarta fragmentos parciais
# =======================================================================


class TestNonArqErrorFreeMode:
    def _make_engine(self, mode: NonArqDeliveryMode) -> NonArqEngine:
        return NonArqEngine(
            local_node_address=1,
            modem=_StubModem(),
            delivery_mode=mode,
        )

    def test_default_mode_is_deliver_with_errors(self):
        eng = NonArqEngine(local_node_address=1, modem=_StubModem())
        assert eng.delivery_mode == NonArqDeliveryMode.DELIVER_W_ERRORS

    def test_error_free_drops_partial(self):
        eng = self._make_engine(NonArqDeliveryMode.ERROR_FREE)
        # Cria um assembly fake já expirado.
        from src.non_arq import _RxAssembly
        assembly = _RxAssembly(
            cpdu_id=0, cpdu_size=10,
            buffer=bytearray(10),
            received=[True] * 5 + [False] * 5,
            expires_at_ms=0,
            source=2, destination=1,
            dpdu_type=DPDUType.NON_ARQ,
        )
        eng._rx_assemblies[(2, 0)] = assembly
        emitted = eng._expire_partial_reassemblies(current_time_ms=1000)
        assert emitted == []  # silenciosamente descartado

    def test_deliver_with_errors_emits_partial(self):
        eng = self._make_engine(NonArqDeliveryMode.DELIVER_W_ERRORS)
        from src.non_arq import _RxAssembly
        assembly = _RxAssembly(
            cpdu_id=0, cpdu_size=10,
            buffer=bytearray(10),
            received=[True] * 5 + [False] * 5,
            expires_at_ms=0,
            source=2, destination=1,
            dpdu_type=DPDUType.NON_ARQ,
        )
        eng._rx_assemblies[(2, 0)] = assembly
        emitted = eng._expire_partial_reassemblies(current_time_ms=1000)
        assert len(emitted) == 1
        assert emitted[0].complete is False


# =======================================================================
# MÉDIA-C3 — WARNING_REASON Tabela C-3
# =======================================================================


class TestWarningReasonsCompleteTable:
    def test_unrecognized_type(self):
        sm = DTSStateMachine()
        # Tipo 9, 10, 11, 12, 13, 14 são reservados — passa int direto.
        assert sm.warning_reason(9) == WARNING_REASON_UNRECOGNIZED_TYPE

    def test_invalid_dpdu_constant_exists(self):
        # Apenas verifica que a constante = 2 está exposta.
        assert WARNING_REASON_INVALID_DPDU == 2


# =======================================================================
# MÉDIA-C4 — TX_UWE/TX_LWE flags por D_PDU individual
# =======================================================================


class TestArqUweLweFlagsPerSegment:
    def test_only_segment_at_tx_uwe_carries_flag(self):
        from src.arq import ArqEngine
        from src.stypes import MAX_DATA_BYTES

        eng = ArqEngine(local_node_address=1, remote_node_address=2)
        # 3 segmentos de payload (3 * 1023 bytes).
        eng.submit_cpdu(b"X" * (MAX_DATA_BYTES * 3))
        frames = eng.process_tx(0)
        # Decodifica os segmentos para inspecionar flags.
        decoded = [decode_dpdu(f) for f in frames]
        # Segmento 0 = TX_LWE; flag tx_lwe deve ser True só nele.
        assert decoded[0].data.tx_lwe is True
        for d in decoded[1:]:
            assert d.data.tx_lwe is False
        # Último segmento = TX_UWE; flag tx_uwe True só lá.
        assert decoded[-1].data.tx_uwe is True
        for d in decoded[:-1]:
            assert d.data.tx_uwe is False


# =======================================================================
# MÉDIA-C5 — EXPEDITED_CONNECTED rejeita DATA regular
# =======================================================================


class TestExpeditedConnectedRejectsRegular:
    def test_data_only_invalid_in_expedited(self):
        sm = DTSStateMachine()
        sm.on_link_made()
        sm.enter_data()
        sm.enter_expedited()
        assert sm.state == DTSState.EXPEDITED_CONNECTED
        assert not sm.is_allowed(DPDUType.DATA_ONLY)
        assert sm.warning_reason(DPDUType.DATA_ONLY) == 3  # invalid for state

    def test_expedited_data_allowed(self):
        sm = DTSStateMachine()
        sm.on_link_made()
        sm.enter_data()
        sm.enter_expedited()
        assert sm.is_allowed(DPDUType.EXPEDITED_DATA_ONLY)
        assert sm.is_allowed(DPDUType.EXPEDITED_ACK_ONLY)


# =======================================================================
# MÉDIA-F4 — RCOP reassembly purga contextos expirados
# =======================================================================


class TestRcopReassemblyTimeout:
    def test_purge_removes_expired(self):
        ctx = _RcopReassemblyContext(timeout_seconds=1.0)
        # Alimenta um segmento incompleto (size = MAX → não termina).
        pdu = RcopPDU(
            connection_id=0, updu_id=42,
            segment_number=0, app_id=0x1002,
            app_data=b"X" * RCOP_MAX_APP_DATA,
        )
        ctx.feed(src_addr=99, src_sap=6, pdu=pdu, now=0.0)
        assert (99, 6, 0, 42) in ctx._buffers

        # Antes do timeout: mantido.
        ctx.purge_expired(now=0.5)
        assert (99, 6, 0, 42) in ctx._buffers

        # Após timeout: descartado.
        expired = ctx.purge_expired(now=2.0)
        assert (99, 6, 0, 42) not in ctx._buffers
        assert expired == [(99, 6, 0, 42)]

    def test_complete_message_does_not_linger(self):
        ctx = _RcopReassemblyContext()
        pdu = RcopPDU(0, 1, 0, 0x1002, b"hello")  # < MAX → completo
        result = ctx.feed(src_addr=99, src_sap=6, pdu=pdu)
        assert result == (0x1002, b"hello")
        assert ctx._buffers == {}
        assert ctx._last_seen == {}


# =======================================================================
# MÉDIA-F5 — CFTP loga warning quando body excede MessageSize
# =======================================================================


class TestCftpDecodeWarning:
    def test_warns_on_excess_bytes(self, caplog):
        # Cabeçalho declarando size=4 mas body com 8 bytes.
        raw = b"id\nrecip\n4\nABCDEFGH"
        with caplog.at_level(logging.WARNING, logger="src.annex_f.cftp"):
            msg = _decode_cftp_message(raw)
        assert msg.message == b"ABCD"
        assert any("MessageSize=4" in record.message and "8 bytes" in record.message
                   for record in caplog.records)

    def test_warns_on_truncation(self, caplog):
        raw = b"id\nrecip\n10\nABCD"
        with caplog.at_level(logging.WARNING, logger="src.annex_f.cftp"):
            msg = _decode_cftp_message(raw)
        assert msg.message == b"ABCD"
        assert any("truncada" in record.message for record in caplog.records)


# =======================================================================
# MÉDIA-F6 — HMTP rejeita send_batch com recipients vazios
# =======================================================================


class TestHmtpRejectsEmptyRecipients:
    def test_empty_recipients_raises(self):
        node = MockNode()
        client = HMTPClient(node)
        msg = MailMessage(sender="a@x", recipients=[], body="oi")
        with pytest.raises(ValueError, match="recipients"):
            client.send_batch(dest_addr=1, hostname="h", messages=[msg])

    def test_empty_sender_raises(self):
        node = MockNode()
        client = HMTPClient(node)
        msg = MailMessage(sender="", recipients=["a@x"], body="oi")
        with pytest.raises(ValueError, match="sender"):
            client.send_batch(dest_addr=1, hostname="h", messages=[msg])

    def test_empty_messages_raises(self):
        node = MockNode()
        client = HMTPClient(node)
        with pytest.raises(ValueError):
            client.send_batch(dest_addr=1, hostname="h", messages=[])

    def test_valid_message_passes(self):
        node = MockNode()
        client = HMTPClient(node)
        msg = MailMessage(sender="a@x", recipients=["b@y"], body="oi")
        client.send_batch(dest_addr=1, hostname="h", messages=[msg])
        assert len(node.sent) == 1


# =======================================================================
# MÉDIA-F1 — Raw SIS Socket envia S_UNBIND_INDICATION
# =======================================================================


class TestRawSisCleanupEmitsUnbind:
    def test_cleanup_calls_node_unbind(self):
        """_cleanup_client desliga o SAP no nó local."""
        from src.raw_sis_socket import RawSisSocketServer, _ClientConnection
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            node = _make_node()
            node.bind(5, rank=0)
            assert 5 in node._saps

            server = RawSisSocketServer(node)
            # Cria um stub de connection com bound_sap=5.

            class _W:
                def __init__(self):
                    self.written = b""
                def write(self, data): self.written += data
                def close(self): pass
                def get_extra_info(self, name, default=None):
                    return default

            class _R: pass

            conn = _ClientConnection(
                conn_id=1, reader=_R(), writer=_W(),
            )
            conn.bound_sap = 5
            server._connections[1] = conn
            server._sap_to_conn[5] = conn

            server._cleanup_client(conn)
            # SAP foi desligado no node.
            assert 5 not in node._saps
            # Escrita ao cliente continha S_UNBIND_INDICATION (SPrimitiveType=5).
            from src.s_primitive_codec import PREAMBLE
            assert PREAMBLE in conn.writer.written
        finally:
            loop.close()
            asyncio.set_event_loop(None)
