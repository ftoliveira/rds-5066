"""CRC helpers for STANAG 5066 framing.

CRC-16-STANAG: used for header CRC (C.3.2.8).
CRC-32-S5066: used for data/payload CRC (C.3.2.11, Edition 3).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# CRC-16-STANAG (header CRC per C.3.2.8)
# Polynomial: x16+x15+x12+x11+x8+x6+x3+1
# Normal form = 0x9949, reflected form = 0x9299
# NOTE: This is NOT the CCITT V.41 polynomial (0x1021).
# ---------------------------------------------------------------------------

CRC16_STANAG_POLY = 0x9949
CRC16_STANAG_POLY_REFLECTED = 0x9299
CRC16_STANAG_INIT = 0x0000


def crc16_ccitt(data: bytes, init: int = CRC16_STANAG_INIT) -> int:
    """Compute CRC-16 exactly as STANAG 5066 Annex C.3.2.8 / Code Example C-1 specifies.

    Uses the STANAG-specific polynomial (0x9949, reflected 0x9299):
    - initial register value = 0
    - process each byte LSB-first
    - right-shift the register
    - xor the reflected polynomial 0x9299 when needed
    - no final xor and no extra byte reversal

    Function name kept as ``crc16_ccitt`` for backward compatibility.
    """
    crc = init & 0xFFFF
    for byte in data:
        mask = 0x01
        while mask <= 0x80:
            bit = ((crc & 0x0001) ^ (1 if (byte & mask) else 0)) & 0x1
            crc >>= 1
            if bit:
                crc ^= CRC16_STANAG_POLY_REFLECTED
            mask <<= 1
    return crc


def crc_to_wire_bytes(crc: int) -> bytes:
    """Serialize CRC-16 bytes in the STANAG wire order (LSB first).

    Annex C.3.1.5 states that the example CRC 0xCE5D is transmitted as the byte
    sequence ``0x5D, 0xCE``.
    """
    value = crc & 0xFFFF
    return bytes([value & 0xFF, (value >> 8) & 0xFF])


def crc_from_wire_bytes(data: bytes) -> int:
    """Deserialize a CRC-16 from the STANAG wire byte order."""
    if len(data) != 2:
        raise ValueError("CRC wire format requires exactly 2 bytes")
    return data[0] | (data[1] << 8)


def append_crc(data: bytes) -> bytes:
    """Append a computed CRC-16 to the given payload."""
    return data + crc_to_wire_bytes(crc16_ccitt(data))


def validate_crc(data: bytes, expected_crc: int) -> bool:
    """Validate a CRC-16 against the supplied payload."""
    return crc16_ccitt(data) == (expected_crc & 0xFFFF)


# ---------------------------------------------------------------------------
# CRC-32-S5066 (data CRC per C.3.2.11, Edition 3)
# ---------------------------------------------------------------------------

# Polynomial: x32+x27+x25+x23+x21+x18+x17+x16+x13+x10+x8+x7+x6+x3+x2+x+1
# Reflected form = 0xF3A4E550  (Code Example C-2, p.16)
CRC32_S5066_POLY_REFLECTED = 0xF3A4E550
CRC32_S5066_INIT = 0x00000000


def crc32_s5066(data: bytes, init: int = CRC32_S5066_INIT) -> int:
    """Compute CRC-32-S5066 per Annex C.3.2.11 / Code Example C-2.

    Reflected (LSB-first) algorithm identical to the CRC-16 approach but with
    a 32-bit register and the S5066 polynomial.

    Test vector (p.16): {0xF0,0x00,0x00,0x47,0x05,0x64,0x02} -> 0xF4178F95
    """
    crc = init & 0xFFFFFFFF
    for byte in data:
        mask = 0x01
        while mask <= 0x80:
            bit = ((crc & 1) ^ (1 if (byte & mask) else 0)) & 1
            crc >>= 1
            if bit:
                crc ^= CRC32_S5066_POLY_REFLECTED
            mask <<= 1
    return crc


def crc32_to_wire_bytes(crc: int) -> bytes:
    """Serialize CRC-32 as 4 bytes, LSB first (STANAG wire order)."""
    v = crc & 0xFFFFFFFF
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF])


def crc32_from_wire_bytes(data: bytes) -> int:
    """Deserialize a CRC-32 from 4 wire bytes (LSB first)."""
    if len(data) != 4:
        raise ValueError("CRC-32 wire format requires exactly 4 bytes")
    return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)


def validate_crc32(data: bytes, expected_crc: int) -> bool:
    """Validate a CRC-32-S5066 against the supplied payload."""
    return crc32_s5066(data) == (expected_crc & 0xFFFFFFFF)
