"""Testes do helper compartilhado de framing de D_PDU (`src.modem.dpdu_framing`).

Fixa o comportamento de re-split de D_PDUs contra o codec autoritativo
`src.dpdu_frame.encode_dpdu`/`decode_dpdu` (não contra a implementação antiga,
que tinha um off-by-2 em `_dpdu_wire_size`). Cobre:
  - `dpdu_wire_size` == `len(encode_dpdu(...))` para todos os tipos do Annex C.
  - `split_stream` em stream concatenado com pré/post-fill (lixo do Annex D).
  - `DpduReassembler` incremental (stream fatiado byte-a-byte).
"""

from __future__ import annotations

import pytest

from src.dpdu_frame import (
    AckHeader,
    DataHeader,
    DPDU,
    ManagementHeader,
    NonArqHeader,
    ResetHeader,
    WarningHeader,
    dpdu_set_address,
    encode_dpdu,
)
from src.modem.dpdu_framing import DpduReassembler, dpdu_wire_size, split_stream
from src.stypes import DPDUType

_ADDR = dpdu_set_address(destination=1, source=2, size=2)


def _frames() -> dict[str, bytes]:
    """Um D_PDU representativo por tipo do Annex C."""
    specs = {
        "DATA_ONLY": DPDU(
            dpdu_type=DPDUType.DATA_ONLY, eow=0, eot=10, address=_ADDR,
            data=DataHeader(True, True, False, False, False, False, 5, 1),
            user_data=b"hello"),
        "DATA_ONLY_empty": DPDU(
            dpdu_type=DPDUType.DATA_ONLY, eow=1, eot=2, address=_ADDR,
            data=DataHeader(True, True, False, False, False, False, 0, 3),
            user_data=b""),
        "NON_ARQ": DPDU(
            dpdu_type=DPDUType.NON_ARQ, eow=0, eot=4, address=_ADDR,
            non_arq=NonArqHeader(3, 0, 1, False, False, 7), user_data=b"abc"),
        "EXP_NON_ARQ": DPDU(
            dpdu_type=DPDUType.EXPEDITED_NON_ARQ, eow=0, eot=4, address=_ADDR,
            non_arq=NonArqHeader(2, 0, 1, False, False, 5), user_data=b"hi"),
        "ACK_ONLY": DPDU(
            dpdu_type=DPDUType.ACK_ONLY, eow=0, eot=0, address=_ADDR,
            ack=AckHeader(rx_lwe=4, sel_acks=b"\x01\x02")),
        "DATA_ACK": DPDU(
            dpdu_type=DPDUType.DATA_ACK, eow=0, eot=3, address=_ADDR,
            data=DataHeader(True, True, False, False, False, False, 4, 2),
            ack=AckHeader(rx_lwe=1, sel_acks=b"\x00"), user_data=b"data"),
        "RESET": DPDU(
            dpdu_type=DPDUType.RESETWIN_RESYNC, eow=0, eot=0, address=_ADDR,
            reset=ResetHeader(True, False, False, False, 0, 5)),
        "WARNING": DPDU(
            dpdu_type=DPDUType.WARNING, eow=0, eot=0, address=_ADDR,
            warning=WarningHeader(received_dpdu_type=0, reason=1)),
        "MGMT": DPDU(
            dpdu_type=DPDUType.MANAGEMENT, eow=0, eot=0, address=_ADDR,
            management=ManagementHeader(0, False, True, 9)),
    }
    return {name: encode_dpdu(d) for name, d in specs.items()}


@pytest.mark.parametrize("name", list(_frames().keys()))
def test_wire_size_matches_encoder(name):
    f = _frames()[name]
    assert dpdu_wire_size(f, 0) == len(f)


def test_split_single_frame():
    f = _frames()["DATA_ONLY"]
    assert split_stream(f) == [f]


def test_split_concatenated_with_prefill_postfill():
    frames = _frames()
    seq = [frames["DATA_ONLY"], frames["NON_ARQ"], frames["ACK_ONLY"]]
    # lixo (pré/post-fill do Annex D) entre e em torno dos D_PDUs
    stream = b"\x00\xAA" + seq[0] + b"\xFF\x11\x22" + seq[1] + seq[2] + b"\x90\x00trailing"
    assert split_stream(stream) == seq


def test_split_ignores_trailing_partial():
    f = _frames()["DATA_ONLY"]
    stream = f + f[:5]                       # segundo frame truncado
    assert split_stream(stream) == [f]


def test_reassembler_byte_by_byte():
    frames = _frames()
    seq = [frames["DATA_ONLY"], frames["NON_ARQ"]]
    stream = b"\x01\x02" + seq[0] + b"\xFF" + seq[1]
    r = DpduReassembler()
    out: list[bytes] = []
    for b in stream:
        out += r.feed(bytes((b,)))
    assert out == seq


def test_reassembler_chunked_feeds():
    frames = _frames()
    seq = [frames["DATA_ACK"], frames["EXP_NON_ARQ"], frames["WARNING"]]
    stream = b"".join(seq)
    r = DpduReassembler()
    out: list[bytes] = []
    # alimenta em fatias arbitrárias de 7 bytes
    for i in range(0, len(stream), 7):
        out += r.feed(stream[i:i + 7])
    assert out == seq
    assert r.pending == 0


def test_reassembler_resync_after_garbage():
    f = _frames()["NON_ARQ"]
    r = DpduReassembler()
    assert r.feed(b"no-sync-here-just-noise") == []
    assert r.feed(f) == [f]
