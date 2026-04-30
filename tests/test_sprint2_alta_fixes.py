"""Tests for Sprint 2 — ALTAS de interop (Anexos C e F).

Cobre:
- ALTA-C3: Expedited ACK envia rx_lwe = (seq+1) % 256, e ACK válido para o
            emissor é rx_lwe == tx_frame_seq+1.
- ALTA-C2: D_PDU regular com DROP_PDU=1 e CRC inválido recebe ACK positivo
            (status=RECEIVED com payload vazio); sem DROP_PDU mantém ERROR.
- ALTA-C1: Encoder do EXPEDITED_DATA_ONLY (Tipo 4) mascara cpdu_id em 4
            bits e rejeita valores >15. Decoder lê apenas 4 bits.
- ALTA-F1: FRAP/FRAPv2 ACK transmite o updu_id solicitado, não +1.
- ALTA-F2: ETHER ``send_ppp`` aceita chamada mínima sem ``**kw`` extra
            e usa defaults priority=5, ttl=120.
- ALTA-F3: IP Client rejeita MTU < 28 via setter.
- ALTA-F4: HF-POP3 servidor envia greeting espontâneo no primeiro contato
            (sem necessidade de NOOP) e via ``send_greeting_to``.
"""

from __future__ import annotations

import pytest

from src.annex_f.bftp import FrapClient, FrapV2Client
from src.annex_f.ether_client import EtherClient, ETHERTYPE_PPP
from src.annex_f.hf_pop3 import HFPOP3Server, POP3State, StoredMessage
from src.annex_f.ip_client import IPClient
from src.annex_f.rcop import APP_ID_FRAP, APP_ID_FRAPV2, decode_rcop_pdu
from src.arq import ArqEngine, RxFrameStatus
from src.dpdu_frame import (
    build_data_only,
    build_expedited_ack_only,
    build_expedited_data_only,
    decode_dpdu,
    dpdu_calc_eot_field,
    dpdu_set_address,
    encode_dpdu,
)
from src.expedited_arq import EXPEDITED_FSN_MOD, ExpeditedArqEngine
from dataclasses import replace
from src.stypes import DPDU, Address, DataHeader, DPDUType
from tests.annex_f_helpers import MockNode, deliver


# =======================================================================
# ALTA-C3 — Expedited ACK rx_lwe = seq+1
# =======================================================================


def _addr(dest=1, src=2):
    return dpdu_set_address(destination=dest, source=src)


class TestExpeditedAckRxLwe:
    def test_ack_carries_seq_plus_one(self):
        """RX de Tipo 4 com seq=N gera ACK com rx_lwe=N+1 (mod 256)."""
        eng = ExpeditedArqEngine(local_node_address=1, remote_node_address=2)
        dpdu = build_expedited_data_only(
            0, dpdu_calc_eot_field(1), _addr(dest=1, src=2),
            b"X", tx_frame_seq=7, cpdu_id=0,
            pdu_start=True, pdu_end=True,
        )
        eng.process_rx_dpdu(decode_dpdu(encode_dpdu(dpdu)))
        # ACK pendente foi gerado pelo RX
        assert eng._pending_ack is not None
        ack_decoded = decode_dpdu(eng._pending_ack)
        assert ack_decoded.dpdu_type is DPDUType.EXPEDITED_ACK_ONLY
        assert ack_decoded.ack.rx_lwe == 8  # 7+1

    def test_ack_wraps_modulo_256(self):
        eng = ExpeditedArqEngine(local_node_address=1, remote_node_address=2)
        dpdu = build_expedited_data_only(
            0, dpdu_calc_eot_field(1), _addr(dest=1, src=2),
            b"X", tx_frame_seq=255, cpdu_id=0,
            pdu_start=True, pdu_end=True,
        )
        eng.process_rx_dpdu(decode_dpdu(encode_dpdu(dpdu)))
        ack_decoded = decode_dpdu(eng._pending_ack)
        assert ack_decoded.ack.rx_lwe == 0  # (255+1) % 256

    def test_tx_accepts_correct_ack(self):
        """TX recebe ACK rx_lwe=tx_frame_seq+1 e considera segmento ACKed."""
        eng = ExpeditedArqEngine(local_node_address=1, remote_node_address=2)
        eng.submit_cpdu(b"hello")
        # Envia primeiro segmento
        frames = eng.process_tx(0)
        assert len(frames) == 1
        # ACK conformante com rx_lwe = 1
        ack = build_expedited_ack_only(
            0, dpdu_calc_eot_field(1), _addr(dest=1, src=2), rx_lwe=1,
        )
        eng.process_rx_dpdu(decode_dpdu(encode_dpdu(ack)))
        assert eng._waiting_ack is False
        assert eng._tx_frame_seq == 1

    def test_tx_rejects_old_style_ack(self):
        """ACK velho (rx_lwe=seq) NÃO é mais aceito — protocolo conformante."""
        eng = ExpeditedArqEngine(local_node_address=1, remote_node_address=2)
        eng.submit_cpdu(b"hello")
        eng.process_tx(0)
        # ACK não conformante: rx_lwe=0 (seq), em vez de 1 (seq+1)
        ack = build_expedited_ack_only(
            0, dpdu_calc_eot_field(1), _addr(dest=1, src=2), rx_lwe=0,
        )
        eng.process_rx_dpdu(decode_dpdu(encode_dpdu(ack)))
        # Continua aguardando — ACK fora do protocolo é ignorado.
        assert eng._waiting_ack is True
        assert eng._tx_frame_seq == 0


# =======================================================================
# ALTA-C2 — DROP_PDU acka positivo independente do CRC
# =======================================================================


class TestDropPduPositiveAck:
    def _make_data_dpdu_with_drop(self, *, seq=0, drop=True):
        return build_data_only(
            eow=0,
            eot=dpdu_calc_eot_field(1),
            address=_addr(dest=1, src=2),
            user_data=b"PAYLOAD",
            tx_frame_seq=seq,
            pdu_start=True,
            pdu_end=True,
            drop_pdu=drop,
        )

    def test_drop_pdu_corrupt_crc_acked_positive(self):
        """C.3.4 §7: DROP_PDU=1 + CRC inválido → frame considerado recebido,
        RX_LWE avança (= ACK positivo)."""
        eng = ArqEngine(local_node_address=1, remote_node_address=2)
        dpdu = self._make_data_dpdu_with_drop(seq=0, drop=True)
        decoded = replace(decode_dpdu(encode_dpdu(dpdu)), data_crc_ok=False)
        assert eng._rx_lwe == 0
        eng.process_rx_dpdu(decoded)
        # LWE avançou → ACK positivo no Selective ACK do peer.
        assert eng._rx_lwe == 1

    def test_no_drop_pdu_corrupt_crc_acked_negative(self):
        """Sem DROP_PDU, CRC inválido mantém slot em ERROR e LWE travado."""
        eng = ArqEngine(local_node_address=1, remote_node_address=2)
        dpdu = self._make_data_dpdu_with_drop(seq=0, drop=False)
        decoded = replace(decode_dpdu(encode_dpdu(dpdu)), data_crc_ok=False)
        eng.process_rx_dpdu(decoded)
        slot = eng._rx_window[0]
        assert slot.status == RxFrameStatus.ERROR
        assert eng._rx_lwe == 0  # NACK: peer pedirá retransmissão


# =======================================================================
# ALTA-C1 — Tipo 4 cpdu_id mascarado em 4 bits
# =======================================================================


class TestExpeditedCpduId4Bits:
    def test_encoder_rejects_cpdu_id_out_of_range(self):
        # Validação ocorre no encode (DataHeader aceita 0..255 para uso em
        # outros tipos de D_PDU).
        dpdu = build_expedited_data_only(
            0, dpdu_calc_eot_field(1), _addr(),
            b"X", tx_frame_seq=0, cpdu_id=16,
        )
        with pytest.raises(ValueError):
            encode_dpdu(dpdu)

    def test_encoder_uses_low_nibble_only(self):
        """Bits altos do byte 3 são NOT_USED (zero)."""
        dpdu = build_expedited_data_only(
            0, dpdu_calc_eot_field(1), _addr(),
            b"X", tx_frame_seq=0, cpdu_id=0x0F,
        )
        raw = encode_dpdu(dpdu)
        # Header tem 2 bytes sync + 4 bytes common + addr + 4 bytes type-specific.
        # O byte cpdu_id está exatamente antes do CRC-16 (2 bytes finais do
        # header). Sem assumir layout, validamos via round-trip.
        decoded = decode_dpdu(raw)
        assert decoded.data.cpdu_id == 0x0F

    def test_decoder_masks_high_nibble(self):
        """Decoder ignora high nibble do byte 3 (campo NOT_USED)."""
        # Construímos um D_PDU com cpdu_id=0 e injetamos lixo no high nibble
        # do byte 3 do header type-specific antes de redecodificar.
        dpdu = build_expedited_data_only(
            0, dpdu_calc_eot_field(1), _addr(),
            b"X", tx_frame_seq=0, cpdu_id=0x05,
        )
        raw = bytearray(encode_dpdu(dpdu))
        # Localiza o byte cpdu_id: header_specific começa após sync(2) + common(4)
        # + addr (variável). Para size_addr=2 (default Address.auto), addr=1 byte.
        addr_size = dpdu.address.size  # nibbles per endpoint
        addr_bytes = (addr_size * 2 + 1) // 2  # total bytes
        type_spec_start = 2 + 4 + addr_bytes
        cpdu_byte_idx = type_spec_start + 3
        raw[cpdu_byte_idx] = (raw[cpdu_byte_idx] & 0x0F) | 0xA0  # injeta lixo

        # Recalcula CRC-16 do header (bytes 2..end_of_type_specific)
        from src.crc import crc16_ccitt, crc_to_wire_bytes
        hdr_end = type_spec_start + 4
        hdr_no_crc = raw[2:hdr_end]
        crc = crc16_ccitt(bytes(hdr_no_crc))
        raw[hdr_end : hdr_end + 2] = crc_to_wire_bytes(crc)

        decoded = decode_dpdu(bytes(raw))
        assert decoded.data.cpdu_id == 0x05  # high nibble descartado


# =======================================================================
# ALTA-F1 — FRAP/FRAPv2 usam updu_id solicitado
# =======================================================================


class TestFrapPreservesUpduId:
    def test_frap_ack_uses_requested_updu_id(self):
        node = MockNode()
        client = FrapClient(node)
        client.ack(dest_addr=1, conn_id=3, updu_id=42)
        assert len(node.sent) == 1
        pdu = decode_rcop_pdu(node.sent[0]["updu"])
        assert pdu.app_id == APP_ID_FRAP
        assert pdu.updu_id == 42  # antes era 43 (=42+1)
        assert pdu.connection_id == 3

    def test_frapv2_ack_uses_requested_updu_id(self):
        node = MockNode()
        client = FrapV2Client(node)
        client.ack(dest_addr=1, filename="x.bin", file_size=100,
                   conn_id=2, updu_id=200)
        pdu = decode_rcop_pdu(node.sent[0]["updu"])
        assert pdu.app_id == APP_ID_FRAPV2
        assert pdu.updu_id == 200
        assert pdu.connection_id == 2

    def test_internal_counter_not_advanced_by_ack(self):
        """ack() com updu_id explícito NÃO avança _rcop_updu_id interno."""
        node = MockNode()
        client = FrapClient(node)
        before = client._rcop_updu_id
        client.ack(dest_addr=1, conn_id=0, updu_id=99)
        assert client._rcop_updu_id == before


# =======================================================================
# ALTA-F2 — ETHER send_ppp defaults
# =======================================================================


class TestEtherSendPppDefaults:
    def test_send_ppp_minimal_args_works(self):
        """send_ppp(dest_addr, frame) não deve lançar TypeError."""
        node = MockNode()
        client = EtherClient(node)
        client.send_ppp(dest_addr=1, ppp_frame=b"\xff\xfe")
        assert len(node.sent) == 1
        sent = node.sent[0]
        assert sent["priority"] == 5
        assert sent["ttl_seconds"] == 120.0
        assert sent["mode"].arq_mode is True
        assert sent["mode"].in_order is True

    def test_send_ppp_overrides_priority(self):
        node = MockNode()
        client = EtherClient(node)
        client.send_ppp(dest_addr=1, ppp_frame=b"x", priority=12, ttl_seconds=30.0)
        assert node.sent[0]["priority"] == 12
        assert node.sent[0]["ttl_seconds"] == 30.0


# =======================================================================
# ALTA-F3 — IP Client MTU mínimo
# =======================================================================


class TestIpClientMtuMinimum:
    def test_setter_rejects_below_28(self):
        node = MockNode()
        client = IPClient(node)
        with pytest.raises(ValueError):
            client.mtu = 27

    def test_setter_accepts_28(self):
        node = MockNode()
        client = IPClient(node)
        client.mtu = 28
        assert client.mtu == 28

    def test_setter_accepts_typical_values(self):
        node = MockNode()
        client = IPClient(node)
        client.mtu = 1500
        assert client.mtu == 1500


# =======================================================================
# ALTA-F4 — HF-POP3 greeting espontâneo
# =======================================================================


class TestPop3SpontaneousGreeting:
    def _make_server(self):
        node = MockNode()
        server = HFPOP3Server(
            node,
            maildrop={"u": [StoredMessage("body")]},
            shared_secrets={"u": "secret"},
        )
        return node, server

    def test_first_data_triggers_greeting(self):
        """Primeiro dado em AUTHORIZATION emite greeting espontâneo."""
        node, server = self._make_server()
        # Primeiro contato com APOP inválido — servidor ainda emite greeting
        deliver(server, src_addr=1, data=b"APOP wrong digest\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK POP3 server ready <" in response
        # Servidor também responde APOP (mas é ERR pois user inexistente)
        assert b"-ERR" in response

    def test_send_greeting_to_does_not_duplicate(self):
        """send_greeting_to + dado subsequente não duplica greeting."""
        node, server = self._make_server()
        server.send_greeting_to(dest_addr=1)
        node.sent.clear()
        # Próximo dado em AUTH não deve emitir greeting de novo
        deliver(server, src_addr=1, data=b"NOOP\r\n")
        response = node.sent[0]["updu"]
        assert b"POP3 server ready" not in response
        assert b"+OK" in response

    def test_noop_after_greeting_returns_simple_ok(self):
        """NOOP após greeting espontâneo retorna +OK simples (RFC 1939)."""
        node, server = self._make_server()
        # Primeiro contato com NOOP gera greeting + +OK
        deliver(server, src_addr=1, data=b"NOOP\r\n")
        # Segundo NOOP não deve trazer greeting
        node.sent.clear()
        deliver(server, src_addr=1, data=b"NOOP\r\n")
        response = node.sent[0]["updu"]
        assert b"POP3 server ready" not in response
        assert b"+OK" in response

    def test_greeting_only_for_authorization_state(self):
        """Após APOP, novo dado não emite greeting (estado != AUTHORIZATION)."""
        node, server = self._make_server()
        # APOP válido — entra em TRANSACTION
        import hashlib
        digest = hashlib.md5(f"{server._timestamp}secret".encode()).hexdigest()
        deliver(server, src_addr=1, data=f"APOP u {digest}\r\n".encode())
        node.sent.clear()
        # Novo dado em TRANSACTION não emite greeting
        deliver(server, src_addr=2, data=b"LIST\r\n")
        response = node.sent[0]["updu"]
        assert b"POP3 server ready" not in response
