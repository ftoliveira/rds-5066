"""Tests for Sprint 5 — BAIXAs + recuperação de MÉDIA-F3.

Cobre:
- BAIXA-A1: SAP 0 requer allow_management_rank=True.
- BAIXA-A2: TTL=0 → ttd=inf; SPDU codifica valid_ttd=0.
- BAIXA-A3: decode_spdu aceita tipos 3-7 sem ValueError.
- BAIXA-B3: idle timeout do Called envia LINK_BREAK reason=NO_MORE_DATA.
- BAIXA-B4: make_link Nonexclusive bloqueado quando Exclusive ativo.
- BAIXA-C1: WARNING D_PDU recebido não dispara WARNING de resposta.
- BAIXA-F1: decode_rcop_pdu aceita RESERVED ≠ 0 mas loga warning.
- MÉDIA-F3: callback hard_link_established do raw_sis usa link_priority real.
"""

from __future__ import annotations

import logging

import math
import pytest

from src.annex_f.rcop import decode_rcop_pdu, encode_rcop_pdu, RcopPDU
from src.cas import (
    CASEngine,
    PhysicalLinkType,
)
from src.modem_if import ModemConfig, ModemInterface
from src.non_arq import NonArqEngine
from src.sis import (
    SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST,
    decode_spdu,
    encode_spdu_data,
    encode_spdu_hard_link_confirm,
    encode_spdu_hard_link_rejected,
    encode_spdu_hard_link_request,
    encode_spdu_hard_link_terminate,
    encode_spdu_hard_link_terminate_confirm,
)
from src.stanag_node import StanagNode
from src.stypes import (
    CPDUBreakReason,
    CasLinkState,
    DPDUType,
    SPDU,
    TxMode,
)


class _StubModem(ModemInterface):
    def __init__(self):
        super().__init__(config=ModemConfig())
        self.tx_frames: list[bytes] = []

    def modem_rx_read_frame(self): return None
    def modem_tx_dpdu(self, b, length=None):
        self.tx_frames.append(bytes(b))
        return len(b)
    def modem_tx_burst(self, frames):
        self.tx_frames.extend(bytes(f) for f in frames)
        return sum(len(f) for f in frames)
    def modem_rx_start(self): pass
    def modem_rx_stop(self): pass
    def modem_get_carrier_status(self): return True
    def modem_set_tx_enable(self, e): pass


def _make_node(**kwargs) -> StanagNode:
    return StanagNode(local_node_address=1, modem=_StubModem(), **kwargs)


# =======================================================================
# BAIXA-A1 — SAP 0 requer allow_management_rank
# =======================================================================


class TestSapZeroPrivileged:
    def test_sap0_rejected_by_default(self):
        node = _make_node()
        with pytest.raises(ValueError, match="SAP 0"):
            node.bind(0, rank=0)

    def test_sap0_accepted_with_management_flag(self):
        node = _make_node(allow_management_rank=True)
        node.bind(0, rank=15)
        assert 0 in node._saps

    def test_other_saps_unaffected(self):
        node = _make_node()
        node.bind(1, rank=0)
        node.bind(15, rank=0)
        assert {1, 15} <= set(node._saps)


# =======================================================================
# BAIXA-A2 — TTL=0 = infinito
# =======================================================================


class TestTtlInfinite:
    def test_ttl_zero_makes_ttd_infinite(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            priority=0, ttl_seconds=0, updu=b"x",
        )
        assert len(node._tx_queue) == 1
        ttd = node._tx_queue[0].spdu.ttd
        assert math.isinf(ttd)

    def test_finite_ttl_is_finite(self):
        node = _make_node()
        node.bind(5, rank=0)
        node.unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            priority=0, ttl_seconds=10.0, updu=b"x",
        )
        ttd = node._tx_queue[0].spdu.ttd
        assert math.isfinite(ttd)

    def test_inf_ttd_encodes_as_invalid(self):
        spdu = SPDU(
            version=1, src_sap=3, dest_sap=5, priority=0,
            ttd=float("inf"), tx_mode=int(TxMode.ARQ),
            updu=b"x",
        )
        encoded = encode_spdu_data(spdu)
        # Byte 2 bit 6 = VALID_TTD; deve estar 0.
        assert (encoded[2] >> 6) & 1 == 0
        # Sem campo TTD: total = 3 (S_PCI) + 1 (updu).
        assert len(encoded) == 4

    def test_inf_ttd_does_not_purge(self):
        """Itens com TTD=inf nunca expiram em _purge_expired."""
        node = _make_node()
        node.bind(5, rank=0)
        node.unidata_request(
            sap_id=5, dest_addr=99, dest_sap=3,
            priority=0, ttl_seconds=0, updu=b"x",
        )
        node._current_time_ms = 10**18  # avança muito no tempo
        node._purge_expired()
        assert len(node._tx_queue) == 1


# =======================================================================
# BAIXA-A3 — decode_spdu cobre tipos 3-7
# =======================================================================


class TestDecodeSpduAllTypes:
    def test_hard_link_request(self):
        encoded = encode_spdu_hard_link_request(
            link_type=2, link_priority=1, requesting_sap=3, remote_sap=5,
        )
        spdu = decode_spdu(encoded)
        assert spdu.src_sap == 3
        assert spdu.dest_sap == 5
        assert spdu.priority == 1

    def test_hard_link_confirm(self):
        spdu = decode_spdu(encode_spdu_hard_link_confirm())
        assert spdu.updu == b""

    def test_hard_link_rejected(self):
        spdu = decode_spdu(encode_spdu_hard_link_rejected(reason=5))
        assert spdu.priority == 5  # campo priority carrega o reason
        assert spdu.updu == b""

    def test_hard_link_terminate(self):
        spdu = decode_spdu(encode_spdu_hard_link_terminate(reason=2))
        assert spdu.priority == 2
        assert spdu.updu == b""

    def test_hard_link_terminate_confirm(self):
        spdu = decode_spdu(encode_spdu_hard_link_terminate_confirm())
        assert spdu.updu == b""

    def test_unknown_type_returns_transparent_spdu(self):
        # Tipo 14 — reservado. Não levanta.
        raw = bytes([0xE0, 0x00])
        spdu = decode_spdu(raw)
        assert spdu.updu == raw


# =======================================================================
# BAIXA-B3 — idle timeout do Called emite LINK_BREAK
# =======================================================================


class TestCalledIdleTimeoutEmitsLinkBreak:
    def _make_cas_with_called_link(self) -> CASEngine:
        non_arq = NonArqEngine(local_node_address=1, modem=_StubModem())
        cas = CASEngine(
            local_node_address=1, non_arq=non_arq,
            called_idle_timeout_ms=1000,
        )
        ctx = cas._ensure_ctx(99)
        ctx.state = CasLinkState.MADE
        ctx.is_called_node = True
        ctx.link_made_ms = 1
        ctx.last_data_rx_ms = 0
        cas._primary_remote = 99
        return cas

    def test_break_sent_on_idle_timeout(self):
        cas = self._make_cas_with_called_link()
        # Avança o tempo além do timeout.
        cas.tick(current_time_ms=2000)
        # Verifica que enfileirou LINK_BREAK no Non-ARQ.
        sent_payloads = [r.payload for r in cas.non_arq._tx_queue_expedited]
        # C_PDU = 0x40 | reason(4) → reason NO_MORE_DATA = 4 → byte = 0x44
        assert any(p == bytes([0x44]) for p in sent_payloads)

    def test_no_break_before_timeout(self):
        cas = self._make_cas_with_called_link()
        cas.tick(current_time_ms=500)
        sent_payloads = [r.payload for r in cas.non_arq._tx_queue_expedited]
        assert not any(p == bytes([0x44]) for p in sent_payloads)


# =======================================================================
# BAIXA-B4 — make_link rejeita Nonexclusive durante Exclusive
# =======================================================================


class TestMakeLinkBlocksNonexclusive:
    def _make_cas(self) -> CASEngine:
        non_arq = NonArqEngine(local_node_address=1, modem=_StubModem())
        return CASEngine(local_node_address=1, non_arq=non_arq)

    def test_rejects_nonexclusive_when_exclusive_pending(self):
        """Mesmo com link primário em CALLING já preenchido, a tentativa
        Nonexclusive subsequente deve falhar — basta o erro RuntimeError
        (B.3.2 (4))."""
        cas = self._make_cas()
        cas.make_link(99, current_time_ms=0,
                      link_type=PhysicalLinkType.EXCLUSIVE)
        with pytest.raises(RuntimeError):
            cas.make_link(42, current_time_ms=0,
                          link_type=PhysicalLinkType.NONEXCLUSIVE)

    def test_rejects_nonexclusive_when_exclusive_made(self):
        cas = self._make_cas()
        cas.make_link(99, current_time_ms=0,
                      link_type=PhysicalLinkType.EXCLUSIVE)
        cas._links[99].state = CasLinkState.MADE
        with pytest.raises(RuntimeError):
            cas.make_link(42, current_time_ms=0,
                          link_type=PhysicalLinkType.NONEXCLUSIVE)

    def test_allows_exclusive_when_other_exclusive_made(self):
        cas = self._make_cas()
        cas.make_link(99, current_time_ms=0,
                      link_type=PhysicalLinkType.EXCLUSIVE)
        cas._links[99].state = CasLinkState.MADE
        # Outro Exclusive (caso B.3 (5) com até 2 simultâneos) é permitido.
        cas.make_link(42, current_time_ms=0,
                      link_type=PhysicalLinkType.EXCLUSIVE)


# =======================================================================
# BAIXA-C1 — WARNING não responde com WARNING
# =======================================================================


class TestNoWarningOnWarning:
    def test_warning_dpdu_does_not_emit_response(self):
        from src.dpdu_frame import (
            build_warning, dpdu_set_address, encode_dpdu, dpdu_calc_eot_field,
        )

        node = _make_node()
        addr = dpdu_set_address(destination=1, source=2)
        warn = build_warning(0, dpdu_calc_eot_field(1), addr,
                             received_dpdu_type=int(DPDUType.DATA_ONLY), reason=3)
        encoded = encode_dpdu(warn)

        # Captura tx_frames antes de injetar.
        modem: _StubModem = node.modem  # type: ignore[assignment]
        before = len(modem.tx_frames)
        node._dispatch_rx_frame(encoded)
        # Não emitiu novo WARNING.
        assert len(modem.tx_frames) == before


# =======================================================================
# BAIXA-F1 — decode_rcop_pdu warn em RESERVED ≠ 0
# =======================================================================


class TestRcopReservedWarn:
    def test_reserved_zero_no_warning(self, caplog):
        pdu = RcopPDU(connection_id=0, updu_id=0, segment_number=0,
                      app_id=0x1002, app_data=b"x")
        raw = encode_rcop_pdu(pdu)
        with caplog.at_level(logging.WARNING, logger="src.annex_f.rcop"):
            decode_rcop_pdu(raw)
        assert not any("RESERVED" in rec.message for rec in caplog.records)

    def test_reserved_nonzero_warns(self, caplog):
        pdu = RcopPDU(connection_id=0, updu_id=0, segment_number=0,
                      app_id=0x1002, app_data=b"x")
        raw = bytearray(encode_rcop_pdu(pdu))
        raw[0] |= 0x05  # injeta lixo nos bits RESERVED (low nibble)
        with caplog.at_level(logging.WARNING, logger="src.annex_f.rcop"):
            decode = decode_rcop_pdu(bytes(raw))
        assert decode.connection_id == 0
        assert any("RESERVED" in rec.message for rec in caplog.records)


# =======================================================================
# MÉDIA-F3 — link_priority real no callback raw_sis
# =======================================================================


class TestRawSisHardLinkEstablishedUsesNegotiatedPriority:
    def test_link_priority_reflects_session(self):
        """O bytes do encode_hard_link_established devem refletir
        ``_link_session.link_priority`` real, não o antigo default 5."""
        from src.s_primitive_codec import (
            decode_s_primitive, decode_hard_link_established,
        )
        from src.raw_sis_socket import RawSisSocketServer, _ClientConnection
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            node = _make_node()
            node.bind(5, rank=0)
            # Configura sessão com prioridade negociada = 2, type = 1.
            node._link_session.link_priority = 2
            node._link_session.sis_hard_link_type = 1

            server = RawSisSocketServer(node)

            class _W:
                def __init__(self): self.written = b""
                def write(self, data): self.written += data
                def close(self): pass
                def is_closing(self): return False
                def get_extra_info(self, name, default=None): return default
            class _R: pass

            conn = _ClientConnection(conn_id=1, reader=_R(), writer=_W())
            conn.bound_sap = 5
            server._connections[1] = conn
            server._sap_to_conn[5] = conn.conn_id  # mapeia sap_id -> conn_id

            # Simula o callback registrado pelo server.
            server._install_hard_link_callbacks(conn)
            cb = node._callbacks.hard_link_established
            assert cb is not None
            cb(99, 3)  # remote_addr, remote_sap

            # Decodifica a primitiva enviada.
            written = conn.writer.written
            prim_type, payload, _ = decode_s_primitive(written)
            decoded = decode_hard_link_established(payload)
            assert decoded["link_priority"] == 2
            assert decoded["link_type"] == 1
        finally:
            loop.close()
            asyncio.set_event_loop(None)
