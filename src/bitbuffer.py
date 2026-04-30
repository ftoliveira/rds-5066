"""Bit-level helpers for LSB-first STANAG framing."""

from __future__ import annotations


class BitWriter:
    """Append integer fields in LSB-first bit order."""

    def __init__(self) -> None:
        self._bits: list[int] = []

    @property
    def bit_length(self) -> int:
        return len(self._bits)

    def append_bits(self, value: int, width: int) -> None:
        if width < 0:
            raise ValueError("Bit width must be non-negative")
        if value < 0:
            raise ValueError("Bit value must be non-negative")
        if width and value >= (1 << width):
            raise ValueError(f"Value {value} does not fit in {width} bits")
        for bit_idx in range(width):
            self._bits.append((value >> bit_idx) & 0x1)

    def append_bytes(self, data: bytes) -> None:
        for byte in data:
            self.append_bits(byte, 8)

    def to_bytes(self) -> bytes:
        out = bytearray((len(self._bits) + 7) // 8)
        for bit_idx, bit in enumerate(self._bits):
            if bit:
                out[bit_idx // 8] |= 1 << (bit_idx % 8)
        return bytes(out)


class BitReader:
    """Read integer fields from an LSB-first byte stream."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._bit_length = len(data) * 8
        self._bit_pos = 0

    @property
    def remaining_bits(self) -> int:
        return self._bit_length - self._bit_pos

    def read_bits(self, width: int) -> int:
        if width < 0:
            raise ValueError("Bit width must be non-negative")
        if self._bit_pos + width > self._bit_length:
            raise ValueError("Not enough bits remaining in buffer")

        value = 0
        for out_idx in range(width):
            absolute_idx = self._bit_pos + out_idx
            byte = self._data[absolute_idx // 8]
            bit = (byte >> (absolute_idx % 8)) & 0x1
            value |= bit << out_idx
        self._bit_pos += width
        return value

    def read_bytes(self, length: int) -> bytes:
        if length < 0:
            raise ValueError("Byte length must be non-negative")
        return bytes(self.read_bits(8) for _ in range(length))
