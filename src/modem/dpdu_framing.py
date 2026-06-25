"""Helper compartilhado de framing de D_PDU (STANAG 5066 Annex C).

Extraído de `hf_modem_adapter.py` para um módulo comum reutilizável por HF, UDP
e o adaptador TCP/110D. Concentra o parsing frágil de stream de D_PDUs:
sync `0x90 0xEB`, tamanho de fio por header (`HDR_SIZE`/`ADR_SIZE`) e CRC de dados.

Funções puras (sem I/O):
  - :func:`dpdu_wire_size` — tamanho em bytes do D_PDU que começa num offset.
  - :func:`split_stream`   — divide um stream concatenado em D_PDUs completos
    (comportamento idêntico ao original do `hf_modem_adapter`).
  - :class:`DpduReassembler` — parser incremental: alimente bytes e receba os
    D_PDUs completos já fechados, mantendo o resto parcial para o próximo feed
    (usado pelo caminho de RX do TCP/110D, onde a recepção OTA chega fatiada em
    múltiplos pacotes DATA do Anexo A).
"""

from __future__ import annotations

from src.flow_log import SYNC_BYTES

# Nibbles de DPDUType que carregam payload de dados + CRC de dados (CRC-32, 4 bytes).
# DATA_ONLY=0, DATA_ACK=2, EXPEDITED_DATA_ONLY=4, NON_ARQ=7, EXPEDITED_NON_ARQ=8
_DATA_CRC_NIBBLES: frozenset[int] = frozenset({0, 2, 4, 7, 8})


def dpdu_wire_size(stream: bytes, offset: int) -> int:
    """Retorna o tamanho em bytes do D_PDU que começa em ``stream[offset]``.

    Suporta todos os tipos definidos em STANAG 5066 Annex C.
    Lança ``ValueError`` se o stream for curto ou o sync estiver ausente.
    """
    if len(stream) < offset + 8:
        raise ValueError("Stream curto demais para conter um D_PDU")
    if stream[offset:offset + 2] != SYNC_BYTES:
        raise ValueError(f"Sync 0x90EB não encontrado em offset {offset}")
    header_size = stream[offset + 5] & 0x1F
    address_size = (stream[offset + 5] >> 5) & 0x07
    # Offset do início do payload (user_data) relativo ao sync:
    #   sync(2) + common(4) + address + type_specific + hdr_crc(2)
    # O campo HDR_SIZE já conta common(4) + type_specific + crc(2) e exclui o
    # endereço (v3 obrigatório, C.3.2.5), então payload começa em
    #   2 (sync) + header_size + address_size.
    # NB: a versão original em hf_modem_adapter somava um "+2" espúrio aqui, o que
    # superdimensionava cada D_PDU em 2 bytes (descartava frames únicos e
    # desalinhava streams concatenados). Validado contra encode_dpdu para todos
    # os tipos de D_PDU do Annex C.
    payload_rel = 2 + header_size + address_size
    dpdu_type_nibble = (stream[offset + 2] >> 4) & 0x0F
    if dpdu_type_nibble in _DATA_CRC_NIBBLES:
        ts_offset = offset + 6 + address_size       # início do header type-specific (absoluto)
        if len(stream) < ts_offset + 2:
            raise ValueError("Stream curto para ler data_len")
        first = stream[ts_offset]
        second = stream[ts_offset + 1]
        data_len = ((first & 0x03) << 8) | second
        return payload_rel + data_len + 4            # dados + data_crc (4 bytes, CRC-32)
    return payload_rel                               # sem payload de dados


def split_stream(stream: bytes) -> list[bytes]:
    """Divide um stream de bytes contendo D_PDUs concatenados em D_PDUs individuais.

    Percorre o stream usando :func:`dpdu_wire_size` para saber onde cada D_PDU
    termina; ignora bytes espúrios (pré/post-fill do Annex D) antes do próximo
    sync. Trailing parcial (D_PDU incompleto no fim) é descartado.
    """
    result: list[bytes] = []
    pos = 0
    length = len(stream)
    while pos < length:
        if stream[pos:pos + 2] != SYNC_BYTES:
            next_sync = stream.find(SYNC_BYTES, pos + 1)
            if next_sync == -1:
                break
            pos = next_sync
        try:
            size = dpdu_wire_size(stream, pos)
        except ValueError:
            # Sync falso ou stream corrompido — avançar 1 byte e tentar de novo
            pos += 1
            continue
        if pos + size > length:
            break
        result.append(stream[pos:pos + size])
        pos += size
    return result


class DpduReassembler:
    """Parser incremental de D_PDUs a partir de um stream fatiado.

    Mantém um buffer interno; cada :meth:`feed` consome o máximo possível de
    D_PDUs completos a partir do início, devolvendo-os, e preserva o resto
    parcial para o próximo feed. Robusto a pré/post-fill (lixo entre D_PDUs):
    ressincroniza pelo sync ``0x90 0xEB``.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    @property
    def pending(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[bytes]:
        """Adiciona bytes e retorna a lista de D_PDUs completos recém-fechados."""
        if data:
            self._buf += data
        return self._drain()

    def _drain(self) -> list[bytes]:
        buf = self._buf
        out: list[bytes] = []
        pos = 0
        length = len(buf)
        while pos < length:
            if buf[pos:pos + 2] != SYNC_BYTES:
                nxt = buf.find(SYNC_BYTES, pos + 1)
                if nxt == -1:
                    # Sem mais sync — descarta lixo, exceto 1 byte final (sync parcial).
                    pos = max(pos, length - 1)
                    break
                pos = nxt
            if length - pos < 8:
                break   # header incompleto — aguarda mais bytes
            try:
                size = dpdu_wire_size(bytes(buf[pos:]), 0)
            except ValueError:
                # header presente mas data_len ainda não — aguarda mais bytes
                break
            if pos + size > length:
                break   # D_PDU incompleto — aguarda mais bytes
            out.append(bytes(buf[pos:pos + size]))
            pos += size
        if pos:
            del buf[:pos]
        return out
