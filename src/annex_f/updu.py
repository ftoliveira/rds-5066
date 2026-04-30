"""
Codec UPDU (User Protocol Data Unit) — Anexo F, Figura F-2.

Formato:
  Byte 0: CONNECTION_ID[7:4] | UPDU_ID[11:8]
  Byte 1: UPDU_ID[7:0]
  Byte 2: SEGMENT_NUMBER
  Byte 3: RESERVED[7:3] | (DATA começa em [2:0])
  Bytes 4+: DATA
"""

from __future__ import annotations

from dataclasses import dataclass

UPDU_HEADER_SIZE = 4
DEFAULT_MTU = 2048
MAX_SEGMENT_DATA = DEFAULT_MTU - UPDU_HEADER_SIZE  # 2044


@dataclass(slots=True)
class UPDUHeader:
    """Cabeçalho UPDU (Figura F-2)."""
    connection_id: int   # 0-15 (4 bits)
    updu_id: int         # 0-4095 (12 bits)
    segment_number: int  # 0-255 (8 bits)

    def __post_init__(self):
        if not (0 <= self.connection_id <= 15):
            raise ValueError(f"connection_id deve ser 0-15, recebeu {self.connection_id}")
        if not (0 <= self.updu_id <= 4095):
            raise ValueError(f"updu_id deve ser 0-4095, recebeu {self.updu_id}")
        if not (0 <= self.segment_number <= 255):
            raise ValueError(f"segment_number deve ser 0-255, recebeu {self.segment_number}")


def encode_updu(header: UPDUHeader, data: bytes) -> bytes:
    """Codifica cabeçalho UPDU + dados em bytes."""
    byte0 = ((header.connection_id & 0x0F) << 4) | ((header.updu_id >> 8) & 0x0F)
    byte1 = header.updu_id & 0xFF
    byte2 = header.segment_number & 0xFF
    byte3 = 0x00  # reserved
    return bytes([byte0, byte1, byte2, byte3]) + data


def decode_updu(raw: bytes) -> tuple[UPDUHeader, bytes]:
    """Decodifica bytes em (UPDUHeader, dados)."""
    if len(raw) < UPDU_HEADER_SIZE:
        raise ValueError(f"UPDU muito curto: {len(raw)} bytes (mínimo {UPDU_HEADER_SIZE})")
    connection_id = (raw[0] >> 4) & 0x0F
    updu_id = ((raw[0] & 0x0F) << 8) | raw[1]
    segment_number = raw[2]
    # byte 3 é reservado
    data = raw[UPDU_HEADER_SIZE:]
    return UPDUHeader(connection_id, updu_id, segment_number), data


def segment_updu(connection_id: int, updu_id: int, data: bytes,
                 mtu: int = DEFAULT_MTU) -> list[bytes]:
    """Divide dados em segmentos UPDU que cabem no MTU.

    Retorna lista de bytes, cada um com cabeçalho UPDU + dados do segmento.
    """
    max_data = mtu - UPDU_HEADER_SIZE
    if max_data <= 0:
        raise ValueError(f"MTU {mtu} muito pequeno para UPDU (min {UPDU_HEADER_SIZE + 1})")

    if len(data) == 0:
        # Mensagem vazia — um segmento sem dados
        header = UPDUHeader(connection_id, updu_id, 0)
        return [encode_updu(header, b"")]

    segments = []
    offset = 0
    seg_num = 0
    while offset < len(data):
        chunk = data[offset:offset + max_data]
        header = UPDUHeader(connection_id, updu_id, seg_num)
        segments.append(encode_updu(header, chunk))
        offset += max_data
        seg_num += 1
        if seg_num > 255:
            raise ValueError(f"Dados muito grandes: requerem mais de 256 segmentos")
    return segments


class ReassemblyContext:
    """Acumula segmentos UPDU por (src_addr, connection_id, updu_id).

    Detecta completude quando recebe segmento com dados menores que max_data
    (último segmento) ou quando gap é detectado.
    """

    def __init__(self, mtu: int = DEFAULT_MTU):
        self._mtu = mtu
        self._max_data = mtu - UPDU_HEADER_SIZE
        # Chave: (src_addr, connection_id, updu_id) -> {seg_num: data}
        self._buffers: dict[tuple[int, int, int], dict[int, bytes]] = {}

    def feed(self, src_addr: int, header: UPDUHeader, data: bytes) -> bytes | None:
        """Alimenta um segmento. Retorna dados completos quando UPDU está remontado,
        ou None se ainda faltam segmentos.

        Heurística de completude: segmento com len(data) < max_data é o último.
        Segmento único (seg_num=0) com qualquer tamanho é completo.
        """
        key = (src_addr, header.connection_id, header.updu_id)

        # Segmento único (não segmentado)
        if header.segment_number == 0 and len(data) < self._max_data:
            # Limpa buffer se existia
            self._buffers.pop(key, None)
            return data

        # Acumula
        if key not in self._buffers:
            self._buffers[key] = {}
        self._buffers[key][header.segment_number] = data

        # Verifica completude: último segmento tem dados < max_data
        buf = self._buffers[key]
        is_last = len(data) < self._max_data
        if not is_last:
            return None

        # Último segmento recebido — verifica se todos os anteriores existem
        max_seg = max(buf.keys())
        if len(buf) != max_seg + 1:
            # Faltam segmentos intermediários
            return None

        # Remonta em ordem
        reassembled = b""
        for i in range(max_seg + 1):
            reassembled += buf[i]

        # Limpa buffer
        del self._buffers[key]
        return reassembled

    def clear(self):
        """Limpa todos os buffers de remontagem."""
        self._buffers.clear()
