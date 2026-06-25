"""Codec puro do MIL-STD-188-110D Annex A — interface TCP (TDSI).

Sem nenhuma I/O: apenas (de)serialização byte-a-byte do protocolo TCP do Anexo A,
casada *exatamente* com a implementação de referência do modem (`rds-hf`,
`backend/mil110/src/lan/packets.c` + `lan_constants.h` + `crc16.c`).

Fidelidade (validada contra os vetores golden do C):
  - CRC-16: poly 0x9299, init 0x0000, LSB-first por byte (A.5.3 / crc16.c).
  - Todos os campos multi-byte são big-endian (A.4.3); o CRC é empacotado MSB-first.
  - Header TCP (8 bytes): 49 50 55 | type | size(2 BE) | hdrCRC(2 BE sobre os 6 primeiros).
  - Pacote: header | payload | payloadCRC(2 BE sobre o payload)  — payloadCRC só existe
    quando payload_size > 0 (keep-alive vazio não tem CRC de payload).

O `PacketReader` espelha `mil110_tcp_reader_next` (packets.c): fragmentação,
resync de preâmbulo e descarte silencioso em falha de CRC (pula 3 bytes e
ressincroniza).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterator

# ── Constantes (lan_constants.h) ─────────────────────────────────────────────

TCP_PREAMBLE = b"\x49\x50\x55"
TCP_HEADER_SIZE = 8
TCP_PACKET_ID_SIZE = 12
MAX_TCP_PACKET_BYTES = 4096
MAX_TCP_PAYLOAD_BYTES = 4086   # 4096 - 8 header - 2 CRC
MAX_TCP_DATA_BYTES = 4072      # 4086 - 1 cmd - 1 order - 12 id
PROTOCOL_VERSION = 12          # CONNECT / CONNECTACK (A.5.1.1.2)


class PacketType(IntEnum):
    """Campo Packet Type — Table A-I."""
    DATA = 0x00
    CONNECT = 0x01
    CONNECTACK = 0x02
    ERROR = 0xFF


class PayloadCommand(IntEnum):
    """Campo Payload Command dentro de pacotes DATA — Table A-II."""
    DATA_TRANSFER = 0x00
    TRANSMIT_ARM = 0x01
    TRANSMIT_START = 0x02
    REQUEST_TX_STATUS = 0x03
    TX_DATA_NAK = 0x04
    TX_STATUS = 0x05
    ABORT_RECEPTION = 0x06
    CARRIER_DETECT = 0x08
    TRANSMIT_SETUP = 0x09
    INITIAL_SETUP = 0x0A
    CONNECTION_PROBE = 0x0B


class PacketOrder(IntEnum):
    """Byte Packet Order no payload Data Transfer — Table A-III."""
    FIRST_ONLY = 1
    FIRST_AND_LAST = 2
    CONTINUATION = 3
    LAST = 4


class NackCause(IntEnum):
    """Byte Cause no payload Tx_Data_NAK — Table A-IV."""
    QUEUES_NOT_ARMED = 0
    TRANSMIT_UNDERRUN = 1
    MISSING_FIRST = 2
    MULTIPLE_FIRST = 3


class TxStateWire(IntEnum):
    """Byte Transmitter State no payload Tx_Status (valores de fio) — Table A-V."""
    FLUSHED = 1
    QUEUES_ARMED_PORT_NOT_READY = 2
    QUEUES_ARMED_PORT_READY = 3
    STARTED = 4
    DRAINING_OK = 5
    DRAINING_FORCED = 6


class CarrierState(IntEnum):
    """Byte Carrier State no payload Carrier Detect — Table A-VI."""
    NONE = 0
    DETECTED = 1


class SyncFlag(IntEnum):
    """Synchronous Flag — Table A-VII."""
    ASYNCHRONOUS = 0
    SYNCHRONOUS = 1


# ── CRC-16 (A.5.3 / crc16.c) ─────────────────────────────────────────────────

_CRC16_POLY = 0x9299
_CRC16_INIT = 0x0000


def crc16(data: bytes) -> int:
    """CRC-16 do Anexo A: poly 0x9299, init 0, LSB-first por byte.

    Tradução direta de `mil110_crc16` (crc16.c). Validado contra os vetores
    golden gerados pelo próprio C do `rds-hf`.
    """
    crc = _CRC16_INIT
    for byte in data:
        for mask in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80):
            bit = (crc & 0x0001) ^ (1 if (byte & mask) else 0)
            crc >>= 1
            if bit:
                crc ^= _CRC16_POLY
    return crc & 0xFFFF


# ── helpers big-endian ───────────────────────────────────────────────────────

def _u16(v: int) -> bytes:
    return bytes(((v >> 8) & 0xFF, v & 0xFF))


def _u32(v: int) -> bytes:
    return bytes(((v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))


def _get_u16(b: bytes, off: int) -> int:
    return (b[off] << 8) | b[off + 1]


def _get_u32(b: bytes, off: int) -> int:
    return (b[off] << 24) | (b[off + 1] << 16) | (b[off + 2] << 8) | b[off + 3]


# ── Pacote TCP (header + payload + CRCs) ─────────────────────────────────────

def encode_packet(ptype: int, payload: bytes = b"") -> bytes:
    """Serializa um pacote TCP do Anexo A.

    Layout: 49 50 55 | type | size(2 BE) | hdrCRC(2 BE) | payload | payloadCRC(2 BE).
    O CRC de payload só é emitido quando há payload (espelha packets.c).
    """
    if len(payload) > MAX_TCP_PAYLOAD_BYTES:
        raise ValueError(f"payload {len(payload)} excede MAX_TCP_PAYLOAD_BYTES")
    header = bytearray()
    header += TCP_PREAMBLE
    header.append(ptype & 0xFF)
    header += _u16(len(payload))
    header += _u16(crc16(bytes(header)))   # CRC sobre os 6 primeiros bytes
    out = bytes(header)
    if payload:
        out += payload + _u16(crc16(payload))
    return out


class PacketReader:
    """Parser de stream TCP do Anexo A (espelha `mil110_tcp_reader`).

    Alimente bytes com :meth:`feed`; itere pacotes completos com :meth:`read`
    (ou :meth:`read_all`). Cada pacote é uma tupla ``(packet_type, payload)``.
    Trata fragmentação entre feeds, resync de preâmbulo e descarte silencioso
    em falha de CRC (pula 3 bytes e ressincroniza).
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> None:
        if data:
            self._buf += data

    def _find_preamble(self) -> int:
        return self._buf.find(TCP_PREAMBLE)

    def read(self) -> tuple[int, bytes] | None:
        """Retorna o próximo ``(type, payload)`` completo, ou ``None``."""
        buf = self._buf
        while True:
            idx = buf.find(TCP_PREAMBLE)
            if idx < 0:
                # Sem preâmbulo: preserva até 2 bytes finais (preâmbulo parcial).
                keep = min(len(buf), 2)
                del buf[: len(buf) - keep]
                return None
            if idx > 0:
                del buf[:idx]
            if len(buf) < TCP_HEADER_SIZE:
                return None
            # valida CRC do header (sobre os 6 primeiros bytes)
            if _get_u16(buf, 6) != crc16(bytes(buf[:6])):
                del buf[:3]   # preâmbulo ruim — pula e ressincroniza
                continue
            ptype = buf[3]
            payload_size = _get_u16(buf, 4)
            total = TCP_HEADER_SIZE + payload_size + (2 if payload_size else 0)
            if len(buf) < total:
                return None
            if payload_size:
                payload = bytes(buf[TCP_HEADER_SIZE: TCP_HEADER_SIZE + payload_size])
                if _get_u16(buf, TCP_HEADER_SIZE + payload_size) != crc16(payload):
                    del buf[:3]   # CRC de payload ruim — ressincroniza
                    continue
            else:
                payload = b""
            del buf[:total]
            return (ptype, payload)

    def read_all(self) -> Iterator[tuple[int, bytes]]:
        while True:
            pkt = self.read()
            if pkt is None:
                return
            yield pkt


# ── Payloads de pacotes DATA (cada um começa com o byte de comando) ──────────

@dataclass(slots=True)
class ConnectPayload:
    version: int = PROTOCOL_VERSION

    def encode(self) -> bytes:
        return bytes((self.version & 0xFF,))

    @classmethod
    def decode(cls, data: bytes) -> "ConnectPayload":
        if len(data) < 1:
            raise ValueError("connect payload curto")
        return cls(version=data[0])


@dataclass(slots=True)
class DataTransferPayload:
    packet_order: int
    packet_id: bytes = field(default=b"\x00" * TCP_PACKET_ID_SIZE)
    data: bytes = b""

    def encode(self) -> bytes:
        pid = bytes(self.packet_id[:TCP_PACKET_ID_SIZE]).ljust(TCP_PACKET_ID_SIZE, b"\x00")
        return (bytes((PayloadCommand.DATA_TRANSFER, self.packet_order & 0xFF)) + pid + self.data)

    @classmethod
    def decode(cls, data: bytes) -> "DataTransferPayload":
        min_len = 2 + TCP_PACKET_ID_SIZE
        if len(data) < min_len:
            raise ValueError("data transfer payload curto")
        return cls(
            packet_order=data[1],
            packet_id=bytes(data[2:2 + TCP_PACKET_ID_SIZE]),
            data=bytes(data[min_len:]),
        )


@dataclass(slots=True)
class TxDataNakPayload:
    cause: int
    nacked_packet_id: bytes = field(default=b"\x00" * TCP_PACKET_ID_SIZE)

    def encode(self) -> bytes:
        pid = bytes(self.nacked_packet_id[:TCP_PACKET_ID_SIZE]).ljust(TCP_PACKET_ID_SIZE, b"\x00")
        return bytes((PayloadCommand.TX_DATA_NAK, self.cause & 0xFF)) + pid

    @classmethod
    def decode(cls, data: bytes) -> "TxDataNakPayload":
        if len(data) < 2 + TCP_PACKET_ID_SIZE:
            raise ValueError("tx_data_nak payload curto")
        return cls(cause=data[1], nacked_packet_id=bytes(data[2:2 + TCP_PACKET_ID_SIZE]))


@dataclass(slots=True)
class TxStatusPayload:
    tx_state: int
    serial_fifo_space: int = 0
    serial_fifo_fill: int = 0
    fifo_critical_ms: int = 0
    fifo_critical_bytes: int = 0

    def encode(self) -> bytes:
        return (
            bytes((PayloadCommand.TX_STATUS, self.tx_state & 0xFF))
            + _u32(self.serial_fifo_space)
            + _u32(self.serial_fifo_fill)
            + _u32(self.fifo_critical_ms)
            + _u32(self.fifo_critical_bytes)
        )

    @classmethod
    def decode(cls, data: bytes) -> "TxStatusPayload":
        if len(data) < 18:
            raise ValueError("tx_status payload curto")
        return cls(
            tx_state=data[1],
            serial_fifo_space=_get_u32(data, 2),
            serial_fifo_fill=_get_u32(data, 6),
            fifo_critical_ms=_get_u32(data, 10),
            fifo_critical_bytes=_get_u32(data, 14),
        )


@dataclass(slots=True)
class CarrierDetectPayload:
    carrier_state: int
    rx_data_rate: int = 0
    rx_blocking_factor: int = 0

    def encode(self) -> bytes:
        return (
            bytes((PayloadCommand.CARRIER_DETECT, self.carrier_state & 0xFF))
            + _u32(self.rx_data_rate)
            + _u32(self.rx_blocking_factor)
        )

    @classmethod
    def decode(cls, data: bytes) -> "CarrierDetectPayload":
        if len(data) < 10:
            raise ValueError("carrier_detect payload curto")
        return cls(
            carrier_state=data[1],
            rx_data_rate=_get_u32(data, 2),
            rx_blocking_factor=_get_u32(data, 6),
        )


@dataclass(slots=True)
class TransmitSetupPayload:
    tx_data_rate: int = 0
    tx_blocking_factor: int = 0

    def encode(self) -> bytes:
        return (
            bytes((PayloadCommand.TRANSMIT_SETUP,))
            + _u32(self.tx_data_rate)
            + _u32(self.tx_blocking_factor)
        )

    @classmethod
    def decode(cls, data: bytes) -> "TransmitSetupPayload":
        if len(data) < 9:
            raise ValueError("transmit_setup payload curto")
        return cls(tx_data_rate=_get_u32(data, 1), tx_blocking_factor=_get_u32(data, 5))


@dataclass(slots=True)
class InitialSetupPayload:
    round_trip_time: int = 0
    min_socket_latency: int = 0
    max_socket_latency: int = 0
    sync_flag: int = SyncFlag.SYNCHRONOUS
    async_data_bits: int = 0
    async_stop_bits: int = 0
    async_parity: int = 0
    async_data_mode: int = 0

    def encode(self) -> bytes:
        return (
            bytes((PayloadCommand.INITIAL_SETUP,))
            + _u32(self.round_trip_time)
            + _u32(self.min_socket_latency)
            + _u32(self.max_socket_latency)
            + bytes((
                self.sync_flag & 0xFF,
                self.async_data_bits & 0xFF,
                self.async_stop_bits & 0xFF,
                self.async_parity & 0xFF,
                self.async_data_mode & 0xFF,
            ))
        )

    @classmethod
    def decode(cls, data: bytes) -> "InitialSetupPayload":
        if len(data) < 18:
            raise ValueError("initial_setup payload curto")
        return cls(
            round_trip_time=_get_u32(data, 1),
            min_socket_latency=_get_u32(data, 5),
            max_socket_latency=_get_u32(data, 9),
            sync_flag=data[13],
            async_data_bits=data[14],
            async_stop_bits=data[15],
            async_parity=data[16],
            async_data_mode=data[17],
        )


# ── Construtores de alto nível para pacotes prontos para o fio ───────────────

def build_connect(version: int = PROTOCOL_VERSION) -> bytes:
    return encode_packet(PacketType.CONNECT, ConnectPayload(version).encode())


def build_connectack(version: int = PROTOCOL_VERSION) -> bytes:
    return encode_packet(PacketType.CONNECTACK, ConnectPayload(version).encode())


def build_command(cmd: int) -> bytes:
    """Pacote DATA de 1 byte (ARM/START/PROBE/REQUEST_TX_STATUS/ABORT)."""
    return encode_packet(PacketType.DATA, bytes((cmd & 0xFF,)))


def build_keepalive() -> bytes:
    """Pacote DATA vazio (keep-alive)."""
    return encode_packet(PacketType.DATA, b"")


def build_data_transfer(packet_order: int, packet_id: bytes, data: bytes) -> bytes:
    return encode_packet(
        PacketType.DATA,
        DataTransferPayload(packet_order, packet_id, data).encode(),
    )
