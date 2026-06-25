"""Testes do codec MIL-STD-188-110D Annex A (TCP/TDSI).

Fidelidade de byte garantida por **vetores golden gerados pelo próprio C de
referência do `rds-hf`** (`backend/mil110/src/lan/{packets.c,crc16.c}`):
  - CRC-16 (poly 0x9299, init 0, LSB-first).
  - Pacotes completos (header + payload + CRCs) de cada tipo do handshake/dados.
Além disso: round-trip de cada payload, fragmentação, resync de preâmbulo e
descarte em CRC inválido no `PacketReader`.
"""

from __future__ import annotations

import pytest

from src.modem.appendix_a_codec import (
    CarrierDetectPayload,
    ConnectPayload,
    DataTransferPayload,
    InitialSetupPayload,
    PacketOrder,
    PacketReader,
    PacketType,
    PayloadCommand,
    SyncFlag,
    TransmitSetupPayload,
    TxStatusPayload,
    TxDataNakPayload,
    TxStateWire,
    build_command,
    build_connect,
    build_connectack,
    build_data_transfer,
    build_keepalive,
    crc16,
    encode_packet,
)


# ── CRC-16 vs vetores golden do C (crc16.c) ──────────────────────────────────

@pytest.mark.parametrize("data,expected", [
    (b"", 0x0000),
    (b"1", 0x1C81),
    (b"123456789", 0x7D01),
    (b"\x00", 0x0000),
    (b"\xff", 0x05B1),
    (bytes([0x49, 0x50, 0x55, 0x01, 0x00, 0x01]), 0x5709),
    (b"\x0c", 0x4DD8),
    (bytes(range(8)), 0xB076),
])
def test_crc16_golden_vectors(data, expected):
    assert crc16(data) == expected


# ── Pacotes completos vs vetores golden do C (packets.c) ─────────────────────
# Gerados por mil110_tcp_packet_serialize(...) compilado do rds-hf.

def _hx(s: str) -> bytes:
    return bytes.fromhex(s)


def test_golden_connect():
    assert build_connect(12) == _hx("49505501000157090c4dd8")


def test_golden_connectack():
    assert build_connectack(12) == _hx("495055020001e6220c4dd8")


def test_golden_keepalive_empty_data():
    # DATA vazio: NÃO tem CRC de payload (só header de 8 bytes).
    assert build_keepalive() == _hx("49505500000036c2")
    assert len(build_keepalive()) == 8


def test_golden_probe_command():
    assert build_command(PayloadCommand.CONNECTION_PROBE) == _hx("49505500000138100b65e6")


def test_golden_transmit_arm_command():
    assert build_command(PayloadCommand.TRANSMIT_ARM) == _hx("4950550000013810010ed2")


def test_golden_data_transfer():
    pid = bytes(range(12))
    pkt = build_data_transfer(PacketOrder.FIRST_AND_LAST, pid, b"Hi")
    assert pkt == _hx("495055000010dbe20002000102030405060708090a0b4869a796")


def test_golden_tx_status():
    payload = TxStatusPayload(
        tx_state=TxStateWire.FLUSHED, serial_fifo_space=100,
        serial_fifo_fill=0, fifo_critical_ms=0, fifo_critical_bytes=0,
    ).encode()
    pkt = encode_packet(PacketType.DATA, payload)
    assert pkt == _hx("495055000012c6460501000000640000000000000000000000005999")


def test_golden_carrier_detect():
    payload = CarrierDetectPayload(carrier_state=1, rx_data_rate=2400, rx_blocking_factor=600).encode()
    pkt = encode_packet(PacketType.DATA, payload)
    assert pkt == _hx("49505500000a5df608010000096000000258d760")


def test_golden_transmit_setup():
    payload = TransmitSetupPayload(tx_data_rate=2400, tx_blocking_factor=600).encode()
    pkt = encode_packet(PacketType.DATA, payload)
    assert pkt == _hx("4950550000094e80090000096000000258d5b9")


def test_golden_initial_setup():
    payload = InitialSetupPayload(
        round_trip_time=15, min_socket_latency=0, max_socket_latency=5000,
        sync_flag=SyncFlag.SYNCHRONOUS, async_data_bits=3, async_stop_bits=0,
        async_parity=0, async_data_mode=0,
    ).encode()
    pkt = encode_packet(PacketType.DATA, payload)
    assert pkt == _hx("495055000012c6460a0000000f0000000000001388010300000001fb")


# ── Round-trip de payloads ───────────────────────────────────────────────────

def test_connect_payload_roundtrip():
    assert ConnectPayload.decode(ConnectPayload(12).encode()).version == 12


def test_data_transfer_roundtrip():
    pid = bytes(range(12))
    orig = DataTransferPayload(PacketOrder.CONTINUATION, pid, b"payload-bytes")
    dec = DataTransferPayload.decode(orig.encode())
    assert dec.packet_order == PacketOrder.CONTINUATION
    assert dec.packet_id == pid
    assert dec.data == b"payload-bytes"


def test_tx_status_roundtrip():
    orig = TxStatusPayload(TxStateWire.STARTED, 4072, 24, 5, 7)
    dec = TxStatusPayload.decode(orig.encode())
    assert dec == orig


def test_carrier_detect_roundtrip():
    orig = CarrierDetectPayload(1, 4800, 1200)
    assert CarrierDetectPayload.decode(orig.encode()) == orig


def test_transmit_setup_roundtrip():
    orig = TransmitSetupPayload(9600, 3600)
    assert TransmitSetupPayload.decode(orig.encode()) == orig


def test_initial_setup_roundtrip():
    orig = InitialSetupPayload(42, 0, 5000, SyncFlag.SYNCHRONOUS, 3, 0, 0, 0)
    assert InitialSetupPayload.decode(orig.encode()) == orig


def test_tx_data_nak_roundtrip():
    pid = bytes(range(12))
    orig = TxDataNakPayload(cause=1, nacked_packet_id=pid)
    dec = TxDataNakPayload.decode(orig.encode())
    assert dec.cause == 1 and dec.nacked_packet_id == pid


# ── PacketReader: fragmentação, resync, descarte por CRC ─────────────────────

def test_reader_single_packet():
    r = PacketReader()
    r.feed(build_connect(12))
    pkt = r.read()
    assert pkt == (PacketType.CONNECT, ConnectPayload(12).encode())
    assert r.read() is None


def test_reader_multiple_packets_one_feed():
    r = PacketReader()
    r.feed(build_connect(12) + build_keepalive() + build_command(PayloadCommand.TRANSMIT_ARM))
    pkts = list(r.read_all())
    assert [p[0] for p in pkts] == [PacketType.CONNECT, PacketType.DATA, PacketType.DATA]
    assert pkts[1][1] == b""               # keep-alive vazio
    assert pkts[2][1] == bytes((PayloadCommand.TRANSMIT_ARM,))


def test_reader_byte_by_byte_fragmentation():
    r = PacketReader()
    blob = build_connect(12) + build_data_transfer(PacketOrder.FIRST_AND_LAST, bytes(range(12)), b"Hi")
    out = []
    for b in blob:
        r.feed(bytes((b,)))
        pkt = r.read()
        if pkt is not None:
            out.append(pkt)
            # drenar quaisquer pacotes adicionais já completos
            out.extend(r.read_all())
    assert [p[0] for p in out] == [PacketType.CONNECT, PacketType.DATA]
    assert DataTransferPayload.decode(out[1][1]).data == b"Hi"


def test_reader_resync_after_garbage_prefix():
    r = PacketReader()
    r.feed(b"\x00\x11\x22garbage-before" + build_connect(12))
    assert r.read() == (PacketType.CONNECT, ConnectPayload(12).encode())


def test_reader_discards_bad_header_crc():
    r = PacketReader()
    good = build_connect(12)
    bad = bytearray(good)
    bad[6] ^= 0xFF                          # corrompe o CRC do header
    r.feed(bytes(bad) + good)
    # o pacote corrompido é descartado; o próximo válido é entregue
    assert r.read() == (PacketType.CONNECT, ConnectPayload(12).encode())


def test_reader_discards_bad_payload_crc():
    r = PacketReader()
    good = build_data_transfer(PacketOrder.FIRST_ONLY, bytes(range(12)), b"abc")
    bad = bytearray(good)
    bad[-1] ^= 0xFF                         # corrompe o CRC do payload
    nxt = build_keepalive()
    r.feed(bytes(bad) + nxt)
    pkts = list(r.read_all())
    # pacote ruim descartado; só o keep-alive sobrevive
    assert pkts == [(PacketType.DATA, b"")]


def test_reader_partial_preamble_preserved_across_feeds():
    r = PacketReader()
    pkt = build_connect(12)
    r.feed(pkt[:1])                         # só o 1º byte do preâmbulo
    assert r.read() is None
    r.feed(pkt[1:])
    assert r.read() == (PacketType.CONNECT, ConnectPayload(12).encode())


def test_encode_packet_rejects_oversize_payload():
    with pytest.raises(ValueError):
        encode_packet(PacketType.DATA, b"\x00" * 4087)
