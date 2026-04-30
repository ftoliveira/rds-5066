"""DPDU framing helpers for STANAG 5066 Edition 3.

HDR_SIZE excludes address bytes but includes CRC per C.3.2.5 (mandatório v3).
"""

from __future__ import annotations

import math
from dataclasses import replace

from src.bitbuffer import BitReader, BitWriter
from src.crc import (
    crc_from_wire_bytes,
    crc16_ccitt,
    crc_to_wire_bytes,
    crc32_from_wire_bytes,
    crc32_s5066,
    crc32_to_wire_bytes,
    validate_crc,
    validate_crc32,
)
from src.stypes import (
    ACK_DPDU_TYPES,
    DATA_CRC_DPDU_TYPES,
    DATA_DPDU_TYPES,
    MAX_DATA_BYTES,
    NON_ARQ_DPDU_TYPES,
    Address,
    AckHeader,
    DPDU,
    DPDUType,
    DataHeader,
    ManagementHeader,
    NonArqHeader,
    ResetHeader,
    SYNC_BYTES,
    WarningHeader,
)


def dpdu_calc_eot_field(remaining_half_seconds: float | int) -> int:
    """Clamp an EOT field to the representable range.

    Per C.3.2.3 shall(5), the value is rounded up to the nearest
    half-second interval.
    """
    if remaining_half_seconds < 0:
        return 0
    return min(math.ceil(remaining_half_seconds), 0xFF)


def dpdu_set_address(destination: int, source: int, size: int | None = None) -> Address:
    """Construct an address pair using either an explicit or automatic size."""
    if size is None:
        return Address.auto(destination=destination, source=source)
    return Address(destination=destination, source=source, size=size)


def _encode_address(address: Address) -> bytes:
    nibble_count = address.size

    def to_nibbles(value: int) -> list[int]:
        return [
            (value >> (4 * shift)) & 0x0F
            for shift in range(nibble_count - 1, -1, -1)
        ]

    nibbles = to_nibbles(address.destination) + to_nibbles(address.source)
    out = bytearray()
    for idx in range(0, len(nibbles), 2):
        out.append((nibbles[idx] << 4) | nibbles[idx + 1])
    return bytes(out)


def _decode_address(data: bytes, size: int) -> Address:
    nibbles: list[int] = []
    for byte in data:
        nibbles.append((byte >> 4) & 0x0F)
        nibbles.append(byte & 0x0F)

    def from_nibbles(values: list[int]) -> int:
        value = 0
        for nibble in values:
            value = (value << 4) | nibble
        return value

    destination = from_nibbles(nibbles[:size])
    source = from_nibbles(nibbles[size : 2 * size])
    return Address(destination=destination, source=source, size=size)


def _ensure_data_payload(dpdu: DPDU) -> None:
    if len(dpdu.user_data) > MAX_DATA_BYTES:
        raise ValueError("User data exceeds STANAG phase 1 maximum")

    if dpdu.dpdu_type in DATA_DPDU_TYPES:
        if dpdu.data is None:
            raise ValueError("Data DPDU requires a data header")
        if dpdu.data.data_size != len(dpdu.user_data):
            raise ValueError("Data size does not match payload length")

    if dpdu.dpdu_type in NON_ARQ_DPDU_TYPES and len(dpdu.user_data) == 0:
        raise ValueError("Non-ARQ DPDUs require user data")
    if dpdu.dpdu_type in NON_ARQ_DPDU_TYPES and dpdu.non_arq is None:
        raise ValueError("Non-ARQ DPDUs require non-ARQ header fields")


def _encode_type_specific_header(dpdu: DPDU) -> bytes:
    if dpdu.dpdu_type is DPDUType.DATA_ACK:
        if dpdu.data is None:
            raise ValueError("DATA-ACK DPDU requires data header fields")
        if dpdu.ack is None:
            raise ValueError("DATA-ACK DPDU requires ack header fields")
        flags = (
            ((1 if dpdu.data.pdu_start else 0) << 7)
            | ((1 if dpdu.data.pdu_end else 0) << 6)
            | ((1 if dpdu.data.deliver_in_order else 0) << 5)
            | ((1 if dpdu.data.drop_pdu else 0) << 4)
            | ((1 if dpdu.data.tx_uwe else 0) << 3)
            | ((1 if dpdu.data.tx_lwe else 0) << 2)
            | ((dpdu.data.data_size >> 8) & 0x03)
        )
        data_header = bytes([
            flags,
            dpdu.data.data_size & 0xFF,
            dpdu.data.tx_frame_seq & 0xFF,
            dpdu.ack.rx_lwe & 0xFF,
        ])
        return data_header + dpdu.ack.sel_acks

    if dpdu.dpdu_type in DATA_DPDU_TYPES:
        if dpdu.data is None:
            raise ValueError("Data DPDU requires data header fields")
        if dpdu.dpdu_type is DPDUType.EXPEDITED_DATA_ONLY:
            flags = (
                ((1 if dpdu.data.pdu_start else 0) << 7)
                | ((1 if dpdu.data.pdu_end else 0) << 6)
                | ((dpdu.data.data_size >> 8) & 0x03)
            )
            return bytes([
                flags,
                dpdu.data.data_size & 0xFF,
                dpdu.data.tx_frame_seq & 0xFF,
                dpdu.data.cpdu_id & 0xFF,
            ])

        flags = (
            ((1 if dpdu.data.pdu_start else 0) << 7)
            | ((1 if dpdu.data.pdu_end else 0) << 6)
            | ((1 if dpdu.data.deliver_in_order else 0) << 5)
            | ((1 if dpdu.data.drop_pdu else 0) << 4)
            | ((1 if dpdu.data.tx_uwe else 0) << 3)
            | ((1 if dpdu.data.tx_lwe else 0) << 2)
            | ((dpdu.data.data_size >> 8) & 0x03)
        )
        return bytes([
            flags,
            dpdu.data.data_size & 0xFF,
            dpdu.data.tx_frame_seq & 0xFF,
        ])

    if dpdu.dpdu_type in ACK_DPDU_TYPES:
        if dpdu.ack is None:
            raise ValueError("ACK DPDU requires ack header fields")
        return bytes([dpdu.ack.rx_lwe & 0xFF]) + dpdu.ack.sel_acks

    if dpdu.dpdu_type is DPDUType.RESETWIN_RESYNC:
        if dpdu.reset is None:
            raise ValueError("RESET DPDU requires reset header fields")
        flags = (
            ((1 if dpdu.reset.full_reset_cmd else 0) << 7)
            | ((1 if dpdu.reset.reset_tx_win_req else 0) << 6)
            | ((1 if dpdu.reset.reset_rx_win_cmd else 0) << 5)
            | ((1 if dpdu.reset.reset_ack else 0) << 4)
        )
        return bytes([
            flags,
            dpdu.reset.new_rx_lwe & 0xFF,
            dpdu.reset.reset_frame_id & 0xFF,
        ])

    if dpdu.dpdu_type is DPDUType.MANAGEMENT:
        if dpdu.management is None:
            raise ValueError("Management DPDU requires a management header")
        # C.3.9: byte layout [NOT_USED(5)][EXTENDED_MSG(1)][VALID_MSG(1)][ACK(1)]
        flags = (
            ((1 if dpdu.user_data else 0) << 2)
            | ((1 if dpdu.management.valid_message else 0) << 1)
            | (1 if dpdu.management.message_ack else 0)
        )
        return bytes([
            flags,
            dpdu.management.management_frame_id & 0xFF,
        ]) + dpdu.user_data

    if dpdu.dpdu_type in NON_ARQ_DPDU_TYPES:
        header = dpdu.non_arq
        assert header is not None
        first = (
            ((header.cpdu_id >> 8) & 0x0F) << 4
            | ((1 if header.deliver_in_order else 0) << 3)
            | ((1 if header.group_address else 0) << 2)
            | ((len(dpdu.user_data) >> 8) & 0x03)
        )
        return (
            bytes([
                first,
                len(dpdu.user_data) & 0xFF,
                header.cpdu_id & 0xFF,
            ])
            + header.cpdu_size.to_bytes(2, "big")
            + header.first_byte_position.to_bytes(2, "big")
            + header.cpdu_reception_window.to_bytes(2, "big")
        )

    if dpdu.dpdu_type is DPDUType.WARNING:
        if dpdu.warning is None:
            raise ValueError("Warning DPDU requires warning header fields")
        return bytes([(dpdu.warning.received_dpdu_type << 4) | dpdu.warning.reason])

    raise ValueError(f"Unsupported DPDU type: {dpdu.dpdu_type}")


def _parse_type_specific_header(
    dpdu_type: DPDUType, header_bytes: bytes
) -> tuple[
    DataHeader | None,
    AckHeader | None,
    ResetHeader | None,
    ManagementHeader | None,
    NonArqHeader | None,
    WarningHeader | None,
    int,
]:
    if dpdu_type is DPDUType.DATA_ACK:
        if len(header_bytes) < 4:
            raise ValueError("DATA-ACK DPDU header must be at least 4 bytes")
        flags = header_bytes[0]
        data = DataHeader(
            pdu_start=bool(flags & 0x80),
            pdu_end=bool(flags & 0x40),
            deliver_in_order=bool(flags & 0x20),
            drop_pdu=bool(flags & 0x10),
            tx_uwe=bool(flags & 0x08),
            tx_lwe=bool(flags & 0x04),
            data_size=((flags & 0x03) << 8) | header_bytes[1],
            tx_frame_seq=header_bytes[2],
        )
        ack = AckHeader(
            rx_lwe=header_bytes[3],
            sel_acks=header_bytes[4:],
        )
        return data, ack, None, None, None, None, data.data_size

    if dpdu_type in DATA_DPDU_TYPES:
        if dpdu_type is DPDUType.EXPEDITED_DATA_ONLY:
            if len(header_bytes) != 4:
                raise ValueError("EXPEDITED DATA header must be 4 bytes")
            flags = header_bytes[0]
            data = DataHeader(
                pdu_start=bool(flags & 0x80),
                pdu_end=bool(flags & 0x40),
                data_size=((flags & 0x03) << 8) | header_bytes[1],
                tx_frame_seq=header_bytes[2],
                cpdu_id=header_bytes[3],
            )
            return data, None, None, None, None, None, data.data_size

        if len(header_bytes) != 3:
            raise ValueError("DATA DPDU header must be 3 bytes")
        flags = header_bytes[0]
        data = DataHeader(
            pdu_start=bool(flags & 0x80),
            pdu_end=bool(flags & 0x40),
            deliver_in_order=bool(flags & 0x20),
            drop_pdu=bool(flags & 0x10),
            tx_uwe=bool(flags & 0x08),
            tx_lwe=bool(flags & 0x04),
            data_size=((flags & 0x03) << 8) | header_bytes[1],
            tx_frame_seq=header_bytes[2],
        )
        return data, None, None, None, None, None, data.data_size

    if dpdu_type in ACK_DPDU_TYPES:
        if len(header_bytes) < 1:
            raise ValueError("ACK DPDU header must be at least 1 byte")
        ack = AckHeader(
            rx_lwe=header_bytes[0],
            sel_acks=header_bytes[1:],
        )
        return None, ack, None, None, None, None, 0

    if dpdu_type is DPDUType.RESETWIN_RESYNC:
        if len(header_bytes) != 3:
            raise ValueError("RESET DPDU header must be 3 bytes")
        flags = header_bytes[0]
        reset = ResetHeader(
            full_reset_cmd=bool(flags & 0x80),
            reset_tx_win_req=bool(flags & 0x40),
            reset_rx_win_cmd=bool(flags & 0x20),
            reset_ack=bool(flags & 0x10),
            new_rx_lwe=header_bytes[1],
            reset_frame_id=header_bytes[2],
        )
        return None, None, reset, None, None, None, 0

    if dpdu_type is DPDUType.MANAGEMENT:
        if len(header_bytes) < 2:
            raise ValueError("MANAGEMENT DPDU header must be at least 2 bytes")
        flags = header_bytes[0]
        if flags & 0xF8:
            raise ValueError("Unused management control bits must be zero")
        # C.3.9: [NOT_USED(5)][EXTENDED_MSG(1)][VALID_MSG(1)][ACK(1)]
        extended = bool(flags & 0x04)
        management = ManagementHeader(
            message_field=0,
            message_ack=bool(flags & 0x01),
            valid_message=bool(flags & 0x02),
            management_frame_id=header_bytes[1],
        )
        payload = header_bytes[2:] if extended else b""
        if not extended and payload:
            raise ValueError("Management header without extension cannot carry payload")
        return None, None, None, management, None, None, len(payload)

    if dpdu_type in NON_ARQ_DPDU_TYPES:
        if len(header_bytes) != 9:
            raise ValueError("NON-ARQ DPDU header must be 9 bytes")
        first = header_bytes[0]
        data_len = ((first & 0x03) << 8) | header_bytes[1]
        non_arq = NonArqHeader(
            cpdu_size=int.from_bytes(header_bytes[3:5], "big"),
            first_byte_position=int.from_bytes(header_bytes[5:7], "big"),
            cpdu_reception_window=int.from_bytes(header_bytes[7:9], "big"),
            group_address=bool(first & 0x04),
            deliver_in_order=bool(first & 0x08),
            cpdu_id=((first >> 4) << 8) | header_bytes[2],
        )
        return None, None, None, None, non_arq, None, data_len

    if dpdu_type is DPDUType.WARNING:
        if len(header_bytes) != 1:
            raise ValueError("WARNING DPDU header must be 1 byte")
        warning = WarningHeader(
            received_dpdu_type=(header_bytes[0] >> 4) & 0x0F,
            reason=header_bytes[0] & 0x0F,
        )
        return None, None, None, None, None, warning, 0

    raise ValueError(f"Unsupported DPDU type: {dpdu_type}")


def _header_without_crc(dpdu: DPDU) -> bytes:
    _ensure_data_payload(dpdu)
    type_specific = _encode_type_specific_header(dpdu)
    address_bytes = _encode_address(dpdu.address)
    # HDR_SIZE (C.3.2.5): 4 common + type_specific + 2 CRC (excludes address per v3)
    hdr_size_field = 4 + len(type_specific) + 2
    if hdr_size_field > 31:
        raise ValueError(
            f"Header size {hdr_size_field} exceeds 5-bit maximum (31 bytes)"
        )
    common = bytes([
        ((int(dpdu.dpdu_type) & 0x0F) << 4) | ((dpdu.eow >> 8) & 0x0F),
        dpdu.eow & 0xFF,
        dpdu.eot & 0xFF,
        ((dpdu.address.size & 0x07) << 5) | (hdr_size_field & 0x1F),
    ])

    return common + address_bytes + type_specific


def encode_dpdu(dpdu: DPDU) -> bytes:
    """Encode a DPDU to raw bytes including sync and CRC fields."""
    header = _header_without_crc(dpdu)
    header_crc = crc16_ccitt(header)

    frame = bytearray()
    frame.extend(SYNC_BYTES)
    frame.extend(header)
    frame.extend(crc_to_wire_bytes(header_crc))

    data_crc = None
    if dpdu.dpdu_type in DATA_CRC_DPDU_TYPES:
        frame.extend(dpdu.user_data)
        data_crc = crc32_s5066(dpdu.user_data)
        frame.extend(crc32_to_wire_bytes(data_crc))
    elif dpdu.dpdu_type is DPDUType.MANAGEMENT:
        pass
    elif dpdu.user_data:
        raise ValueError("This DPDU type does not carry user data")

    dpdu.header_crc = header_crc
    dpdu.data_crc = data_crc
    dpdu.header_crc_ok = True
    dpdu.data_crc_ok = True if data_crc is not None else None
    dpdu.raw_bytes = bytes(frame)
    return dpdu.raw_bytes


def decode_dpdu(raw_buffer: bytes) -> DPDU:
    """Decode a complete DPDU frame from bytes."""
    if len(raw_buffer) < 8:
        raise ValueError("Buffer too short to contain a DPDU")
    if raw_buffer[:2] != SYNC_BYTES:
        raise ValueError("Invalid STANAG sync bytes")

    common = raw_buffer[2:6]
    dpdu_type = DPDUType((common[0] >> 4) & 0x0F)
    eow = ((common[0] & 0x0F) << 8) | common[1]
    eot = common[2]
    address_size = (common[3] >> 5) & 0x07
    header_size = common[3] & 0x1F
    if header_size < 6:
        raise ValueError("Header size is smaller than common header + CRC")
    if address_size == 0:
        raise ValueError("Address size 0 is not supported")

    address_len = address_size
    # HDR_SIZE excludes address but includes CRC (v3 mandatory, C.3.2.5)
    type_specific_len = header_size - 6  # 4 common + 2 CRC
    frame_min_len = 2 + header_size + address_len  # CRC already in h
    header_crc_offset = header_size + address_len  # 2(sync) + h - 2(CRC at end) + m
    if type_specific_len < 0:
        raise ValueError("Header size is too small for advertised type")
    if len(raw_buffer) < frame_min_len:
        raise ValueError("Buffer too short for advertised header")

    address_offset = 6
    address_bytes = raw_buffer[address_offset : address_offset + address_len]
    type_specific_offset = address_offset + address_len
    type_specific_bytes = raw_buffer[type_specific_offset : type_specific_offset + type_specific_len]
    header_bytes = raw_buffer[2:header_crc_offset]
    header_crc = crc_from_wire_bytes(raw_buffer[header_crc_offset : header_crc_offset + 2])

    address = _decode_address(address_bytes, address_size)
    data, ack, reset, management, non_arq, warning, data_len = _parse_type_specific_header(
        dpdu_type, type_specific_bytes
    )

    payload_offset = header_crc_offset + 2
    user_data = b""
    data_crc = None
    data_crc_ok = None

    if dpdu_type in DATA_CRC_DPDU_TYPES:
        payload_end = payload_offset + data_len
        data_crc_end = payload_end + 4  # CRC-32 = 4 bytes
        if len(raw_buffer) != data_crc_end:
            raise ValueError("Buffer length does not match DPDU payload length")
        user_data = raw_buffer[payload_offset:payload_end]
        data_crc = crc32_from_wire_bytes(raw_buffer[payload_end:data_crc_end])
        data_crc_ok = validate_crc32(user_data, data_crc)
    elif dpdu_type is DPDUType.MANAGEMENT:
        user_data = type_specific_bytes[2:] if management and len(type_specific_bytes) > 2 else b""
        if management is not None:
            management.message_field = eow
        if len(raw_buffer) != payload_offset:
            raise ValueError("Unexpected trailing data for management DPDU")
    elif len(raw_buffer) != payload_offset:
        raise ValueError("Unexpected trailing data for DPDU without payload")

    dpdu = DPDU(
        dpdu_type=dpdu_type,
        eow=eow,
        eot=eot,
        address=address,
        data=data,
        ack=ack,
        reset=reset,
        management=management,
        non_arq=non_arq,
        warning=warning,
        user_data=user_data,
        header_crc=header_crc,
        data_crc=data_crc,
        header_crc_ok=validate_crc(header_bytes, header_crc),
        data_crc_ok=data_crc_ok,
        raw_bytes=raw_buffer,
    )
    return dpdu


def dpdu_validate_header_crc(dpdu: DPDU) -> bool:
    """Validate or recompute the header CRC for a DPDU."""
    if dpdu.raw_bytes:
        return decode_dpdu(dpdu.raw_bytes).header_crc_ok is True
    if dpdu.header_crc is None:
        return False
    return crc16_ccitt(_header_without_crc(dpdu)) == dpdu.header_crc


def dpdu_validate_data_crc(dpdu: DPDU) -> bool:
    """Validate or recompute the payload CRC-32 for a DPDU."""
    if not dpdu.requires_data_crc():
        return True
    if dpdu.raw_bytes:
        decoded = decode_dpdu(dpdu.raw_bytes)
        return decoded.data_crc_ok is True
    if dpdu.data_crc is None:
        return False
    return crc32_s5066(dpdu.user_data) == dpdu.data_crc


def flip_bit(data: bytes, bit_index: int) -> bytes:
    """Return a new byte string with a single bit toggled."""
    if bit_index < 0 or bit_index >= len(data) * 8:
        raise ValueError("Bit index out of range")
    mutated = bytearray(data)
    mutated[bit_index // 8] ^= 1 << (bit_index % 8)
    return bytes(mutated)


def _normalize_payload_lengths(dpdu: DPDU) -> DPDU:
    if dpdu.dpdu_type in DATA_DPDU_TYPES and dpdu.data is not None:
        return replace(dpdu, data=replace(dpdu.data, data_size=len(dpdu.user_data)))
    return dpdu


def build_data_only(
    eow: int,
    eot: int,
    address: Address,
    user_data: bytes,
    tx_frame_seq: int,
    *,
    pdu_start: bool = True,
    pdu_end: bool = True,
    deliver_in_order: bool = False,
    drop_pdu: bool = False,
    tx_uwe: bool = False,
    tx_lwe: bool = False,
) -> DPDU:
    return _normalize_payload_lengths(
        DPDU(
            dpdu_type=DPDUType.DATA_ONLY,
            eow=eow,
            eot=eot,
            address=address,
            data=DataHeader(
                pdu_start=pdu_start,
                pdu_end=pdu_end,
                deliver_in_order=deliver_in_order,
                drop_pdu=drop_pdu,
                tx_uwe=tx_uwe,
                tx_lwe=tx_lwe,
                data_size=len(user_data),
                tx_frame_seq=tx_frame_seq,
            ),
            user_data=user_data,
        )
    )


def build_ack_only(eow: int, eot: int, address: Address, rx_lwe: int, sel_acks: bytes = b"") -> DPDU:
    return DPDU(
        dpdu_type=DPDUType.ACK_ONLY,
        eow=eow,
        eot=eot,
        address=address,
        ack=AckHeader(rx_lwe=rx_lwe, sel_acks=sel_acks),
    )


def build_data_ack(
    eow: int,
    eot: int,
    address: Address,
    user_data: bytes,
    tx_frame_seq: int,
    rx_lwe: int,
    sel_acks: bytes = b"",
    *,
    pdu_start: bool = True,
    pdu_end: bool = True,
    deliver_in_order: bool = False,
) -> DPDU:
    return _normalize_payload_lengths(
        DPDU(
            dpdu_type=DPDUType.DATA_ACK,
            eow=eow,
            eot=eot,
            address=address,
            data=DataHeader(
                pdu_start=pdu_start,
                pdu_end=pdu_end,
                deliver_in_order=deliver_in_order,
                data_size=len(user_data),
                tx_frame_seq=tx_frame_seq,
            ),
            ack=AckHeader(rx_lwe=rx_lwe, sel_acks=sel_acks),
            user_data=user_data,
        )
    )


def build_resetwin_resync(
    eow: int,
    eot: int,
    address: Address,
    *,
    full_reset_cmd: bool,
    reset_tx_win_req: bool = False,
    reset_rx_win_cmd: bool = False,
    reset_ack: bool = False,
    new_rx_lwe: int = 0,
    reset_frame_id: int = 0,
) -> DPDU:
    return DPDU(
        dpdu_type=DPDUType.RESETWIN_RESYNC,
        eow=eow,
        eot=eot,
        address=address,
        reset=ResetHeader(
            full_reset_cmd=full_reset_cmd,
            reset_tx_win_req=reset_tx_win_req,
            reset_rx_win_cmd=reset_rx_win_cmd,
            reset_ack=reset_ack,
            new_rx_lwe=new_rx_lwe,
            reset_frame_id=reset_frame_id,
        ),
    )


def build_expedited_data_only(
    eow: int,
    eot: int,
    address: Address,
    user_data: bytes,
    tx_frame_seq: int,
    *,
    cpdu_id: int = 0,
    pdu_start: bool = True,
    pdu_end: bool = True,
) -> DPDU:
    return _normalize_payload_lengths(
        DPDU(
            dpdu_type=DPDUType.EXPEDITED_DATA_ONLY,
            eow=eow,
            eot=eot,
            address=address,
            data=DataHeader(
                pdu_start=pdu_start,
                pdu_end=pdu_end,
                data_size=len(user_data),
                tx_frame_seq=tx_frame_seq,
                cpdu_id=cpdu_id,
            ),
            user_data=user_data,
        )
    )


def build_expedited_ack_only(
    eow: int, eot: int, address: Address, rx_lwe: int, sel_acks: bytes = b""
) -> DPDU:
    return DPDU(
        dpdu_type=DPDUType.EXPEDITED_ACK_ONLY,
        eow=eow,
        eot=eot,
        address=address,
        ack=AckHeader(rx_lwe=rx_lwe, sel_acks=sel_acks),
    )


def build_eow(*args, **kwargs) -> DPDU:
    """Compat wrapper kept during the transition from the old local naming."""
    return build_expedited_data_only(*args, **kwargs)


def build_ack_eow(*args, **kwargs) -> DPDU:
    """Compat wrapper kept during the transition from the old local naming."""
    return build_expedited_ack_only(*args, **kwargs)


def build_management(
    eow: int,
    eot: int,
    address: Address,
    msg_type: int,
    data: bytes,
    *,
    message_contents: int = 0,
    message_ack: bool = False,
    valid_message: bool = True,
    management_frame_id: int = 0,
) -> DPDU:
    if len(data) > 23:
        raise ValueError("Management extended message cannot exceed 23 bytes")
    message_field = ((message_contents & 0xFF) << 4) | (msg_type & 0x0F)
    return DPDU(
        dpdu_type=DPDUType.MANAGEMENT,
        eow=message_field & 0xFFF,
        eot=eot,
        address=address,
        management=ManagementHeader(
            message_field=message_field,
            message_ack=message_ack,
            valid_message=valid_message,
            management_frame_id=management_frame_id,
        ),
        user_data=data,
    )


def build_non_arq(
    eow: int,
    eot: int,
    address: Address,
    data: bytes,
    *,
    cpdu_reception_window: int = 0,
    first_byte_position: int = 0,
    cpdu_size: int | None = None,
    group_address: bool = False,
    deliver_in_order: bool = False,
    cpdu_id: int = 0,
) -> DPDU:
    return DPDU(
        dpdu_type=DPDUType.NON_ARQ,
        eow=eow,
        eot=eot,
        address=address,
        non_arq=NonArqHeader(
            cpdu_reception_window=cpdu_reception_window,
            first_byte_position=first_byte_position,
            cpdu_size=len(data) if cpdu_size is None else cpdu_size,
            group_address=group_address,
            deliver_in_order=deliver_in_order,
            cpdu_id=cpdu_id,
        ),
        user_data=data,
    )


def build_expedited_non_arq(
    eow: int,
    eot: int,
    address: Address,
    data: bytes,
    *,
    cpdu_reception_window: int = 0,
    first_byte_position: int = 0,
    cpdu_size: int | None = None,
    group_address: bool = False,
    deliver_in_order: bool = False,
    cpdu_id: int = 0,
) -> DPDU:
    return DPDU(
        dpdu_type=DPDUType.EXPEDITED_NON_ARQ,
        eow=eow,
        eot=eot,
        address=address,
        non_arq=NonArqHeader(
            cpdu_reception_window=cpdu_reception_window,
            first_byte_position=first_byte_position,
            cpdu_size=len(data) if cpdu_size is None else cpdu_size,
            group_address=group_address,
            deliver_in_order=deliver_in_order,
            cpdu_id=cpdu_id,
        ),
        user_data=data,
    )


def build_warning(eow: int, eot: int, address: Address, received_dpdu_type: int, reason: int) -> DPDU:
    return DPDU(
        dpdu_type=DPDUType.WARNING,
        eow=eow,
        eot=eot,
        address=address,
        warning=WarningHeader(received_dpdu_type=received_dpdu_type, reason=reason),
    )


def encode_data_only(*args, **kwargs) -> bytes:
    return encode_dpdu(build_data_only(*args, **kwargs))


def encode_ack_only(*args, **kwargs) -> bytes:
    return encode_dpdu(build_ack_only(*args, **kwargs))


def encode_data_ack(*args, **kwargs) -> bytes:
    return encode_dpdu(build_data_ack(*args, **kwargs))


def encode_resetwin_resync(*args, **kwargs) -> bytes:
    return encode_dpdu(build_resetwin_resync(*args, **kwargs))


def encode_expedited_data_only(*args, **kwargs) -> bytes:
    return encode_dpdu(build_expedited_data_only(*args, **kwargs))


def encode_expedited_ack_only(*args, **kwargs) -> bytes:
    return encode_dpdu(build_expedited_ack_only(*args, **kwargs))


def encode_eow(*args, **kwargs) -> bytes:
    return encode_expedited_data_only(*args, **kwargs)


def encode_ack_eow(*args, **kwargs) -> bytes:
    return encode_expedited_ack_only(*args, **kwargs)


def encode_management(*args, **kwargs) -> bytes:
    return encode_dpdu(build_management(*args, **kwargs))


def encode_non_arq(*args, **kwargs) -> bytes:
    return encode_dpdu(build_non_arq(*args, **kwargs))


def encode_expedited_non_arq(*args, **kwargs) -> bytes:
    return encode_dpdu(build_expedited_non_arq(*args, **kwargs))


def encode_warning(*args, **kwargs) -> bytes:
    return encode_dpdu(build_warning(*args, **kwargs))
