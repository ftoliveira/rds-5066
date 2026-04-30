"""
Codec binário das S_PRIMITIVEs — Annex A §A.2.2 (Edition 3).

Formato wire genérico (Figure A-1):
    Byte 0-1: Preamble = 0x90, 0xEB (Maury-Styles, LSB first)
    Byte 2:   Version  = 0x00
    Byte 3-4: Size_of_S_Primitive (2 bytes, low-byte first)
    Byte 5+:  Encoded S_Primitive (first byte = TYPE number)
"""

from __future__ import annotations

import struct

from src.stypes import SPrimitiveType

PREAMBLE = b'\x90\xEB'
VERSION = 0x00
HEADER_SIZE = 5  # preamble(2) + version(1) + size(2)


# ---------------------------------------------------------------------------
# Generic wrap / unwrap
# ---------------------------------------------------------------------------

def encode_s_primitive(prim_type: int, payload: bytes) -> bytes:
    """Wraps encoded primitive with preamble + version + size header."""
    encoded = bytes([prim_type]) + payload
    size = len(encoded)
    return PREAMBLE + bytes([VERSION]) + struct.pack('<H', size) + encoded


def decode_s_primitive(stream: bytes) -> tuple[int, bytes, int]:
    """Decode one S_PRIMITIVE from stream.

    Returns (prim_type, payload, bytes_consumed).
    Scans for preamble first; raises ValueError if not found or incomplete.
    """
    idx = stream.find(PREAMBLE)
    if idx == -1:
        raise ValueError("Preamble not found in stream")
    if len(stream) < idx + HEADER_SIZE:
        raise ValueError("Incomplete header")

    version = stream[idx + 2]
    size = struct.unpack_from('<H', stream, idx + 3)[0]

    if size < 1:
        raise ValueError("S_Primitive size must be >= 1")
    if len(stream) < idx + HEADER_SIZE + size:
        raise ValueError("Incomplete S_Primitive data")

    encoded = stream[idx + HEADER_SIZE: idx + HEADER_SIZE + size]
    prim_type = encoded[0]
    payload = encoded[1:]
    bytes_consumed = idx + HEADER_SIZE + size
    return prim_type, payload, bytes_consumed


# ---------------------------------------------------------------------------
# Address encoding (A.2.2.28.1): 4 bytes
#   Bits [31:29] = size (3 bits)
#   Bit  [28]    = group_flag
#   Bits [27:0]  = address (28 bits)
# Stored as 4 bytes, big-endian.
# ---------------------------------------------------------------------------

def encode_address(address: int, size: int = 7, group: bool = False) -> bytes:
    """Encode STANAG address as 4 bytes (A.2.2.28.1)."""
    val = ((size & 0x07) << 29) | (int(group) << 28) | (address & 0x0FFFFFFF)
    return struct.pack('>I', val)


def decode_address(data: bytes, offset: int = 0) -> tuple[int, int, bool]:
    """Decode address from 4 bytes. Returns (address, size, group_flag)."""
    val = struct.unpack_from('>I', data, offset)[0]
    size = (val >> 29) & 0x07
    group = bool((val >> 28) & 0x01)
    address = val & 0x0FFFFFFF
    return address, size, group


# ---------------------------------------------------------------------------
# Delivery Mode encoding (A.2.2.28.2): 1 byte
#   Bits [7:4] = tx_mode (4 bits)
#   Bits [3:2] = delivery_confirm (2 bits)
#   Bit  [1]   = delivery_order
#   Bit  [0]   = extension
# ---------------------------------------------------------------------------

def encode_delivery_mode(tx_mode: int, delivery_confirm: int = 0,
                         delivery_order: bool = False, ext: bool = False) -> bytes:
    """Encode delivery mode as 1 byte (A.2.2.28.2)."""
    val = ((tx_mode & 0x0F) << 4) | ((delivery_confirm & 0x03) << 2) | \
          (int(delivery_order) << 1) | int(ext)
    return bytes([val])


def decode_delivery_mode(data: bytes, offset: int = 0) -> dict:
    """Decode 1-byte delivery mode. Returns dict with tx_mode, delivery_confirm, etc."""
    val = data[offset]
    return {
        'tx_mode': (val >> 4) & 0x0F,
        'delivery_confirm': (val >> 2) & 0x03,
        'delivery_order': bool((val >> 1) & 0x01),
        'extension': bool(val & 0x01),
    }


# ---------------------------------------------------------------------------
# Service Type encoding (Fig A-3): 2 bytes (16 bits)
#   Bits [15:14] = Transmission Mode
#   Bits [13:12] = Delivery Confirmation
#   Bit  [11]    = Delivery Order
#   Bit  [10]    = Extended Field
#   Bits [9:6]   = Min Retransmissions
#   Bits [5:0]   = Reserved (0)
# ---------------------------------------------------------------------------

def encode_service_type(transmission_mode: int = 2, delivery_confirmation: int = 0,
                        delivery_order: bool = False, extended: bool = False,
                        min_retransmissions: int = 0) -> bytes:
    """Encode SERVICE_TYPE as 2 bytes (Fig A-3)."""
    val = ((transmission_mode & 0x03) << 14) | \
          ((delivery_confirmation & 0x03) << 12) | \
          (int(delivery_order) << 11) | \
          (int(extended) << 10) | \
          ((min_retransmissions & 0x0F) << 6)
    return struct.pack('>H', val)


def decode_service_type(data: bytes, offset: int = 0) -> dict:
    """Decode 2-byte SERVICE_TYPE. Returns dict with subfields."""
    val = struct.unpack_from('>H', data, offset)[0]
    return {
        'transmission_mode': (val >> 14) & 0x03,
        'delivery_confirmation': (val >> 12) & 0x03,
        'delivery_order': bool((val >> 11) & 0x01),
        'extended': bool((val >> 10) & 0x01),
        'min_retransmissions': (val >> 6) & 0x0F,
    }


# ---------------------------------------------------------------------------
# Hard link packed byte helper:
#   Bits [7:6] = LINK_TYPE (2 bits)
#   Bits [5:4] = LINK_PRIORITY (2 bits)
#   Bits [3:0] = REMOTE_SAP_ID (4 bits)
# ---------------------------------------------------------------------------

def _pack_hard_link_byte(link_type: int, link_priority: int, remote_sap: int) -> int:
    return ((link_type & 0x03) << 6) | ((link_priority & 0x03) << 4) | (remote_sap & 0x0F)


def _unpack_hard_link_byte(val: int) -> tuple[int, int, int]:
    link_type = (val >> 6) & 0x03
    link_priority = (val >> 4) & 0x03
    remote_sap = val & 0x0F
    return link_type, link_priority, remote_sap


# ---------------------------------------------------------------------------
# Per-primitive encoders / decoders
# ---------------------------------------------------------------------------

# TYPE 1: S_BIND_REQUEST (Fig A-2)
#   Byte0 = SAP_ID[7:4] | RANK[3:0]
#   Bytes1-2 = SERVICE_TYPE (2 bytes)
def encode_bind_request(sap_id: int, rank: int = 0,
                        service_type: int | None = None, *,
                        transmission_mode: int = 2, delivery_confirmation: int = 0,
                        delivery_order: bool = False, extended: bool = False,
                        min_retransmissions: int = 0) -> bytes:
    """Encode S_BIND_REQUEST per Fig A-2.

    service_type: legacy int ignored (kept for call-compat). Use keyword args for subfields.
    """
    byte0 = ((sap_id & 0x0F) << 4) | (rank & 0x0F)
    payload = bytes([byte0])
    payload += encode_service_type(transmission_mode, delivery_confirmation,
                                   delivery_order, extended, min_retransmissions)
    return encode_s_primitive(SPrimitiveType.S_BIND_REQUEST, payload)


def decode_bind_request(payload: bytes) -> dict:
    """Decode S_BIND_REQUEST payload (after type byte)."""
    byte0 = payload[0]
    sap_id = (byte0 >> 4) & 0x0F
    rank = byte0 & 0x0F
    st = decode_service_type(payload, 1)
    return {
        'sap_id': sap_id,
        'rank': rank,
        'service_type': st,
    }


# TYPE 2: S_UNBIND_REQUEST — 0 bytes payload
def encode_unbind_request() -> bytes:
    return encode_s_primitive(SPrimitiveType.S_UNBIND_REQUEST, b'')


def decode_unbind_request(payload: bytes) -> dict:
    return {}


# TYPE 3: S_BIND_ACCEPTED (Fig A-5)
#   Byte0 = SAP_ID[7:4] | NOT_USED[3:0]
#   Bytes1-2 = MTU (16 bits, LE)
def encode_bind_accepted(sap_id: int, mtu: int = 2048) -> bytes:
    byte0 = (sap_id & 0x0F) << 4
    payload = bytes([byte0]) + struct.pack('<H', mtu)
    return encode_s_primitive(SPrimitiveType.S_BIND_ACCEPTED, payload)


def decode_bind_accepted(payload: bytes) -> dict:
    sap_id = (payload[0] >> 4) & 0x0F
    mtu = struct.unpack_from('<H', payload, 1)[0]
    return {
        'sap_id': sap_id,
        'mtu': mtu,
    }


# TYPE 4: S_BIND_REJECTED — 1 byte: reason
def encode_bind_rejected(reason: int) -> bytes:
    return encode_s_primitive(SPrimitiveType.S_BIND_REJECTED, bytes([reason & 0xFF]))


def decode_bind_rejected(payload: bytes) -> dict:
    return {'reason': payload[0]}


# TYPE 5: S_UNBIND_INDICATION — 1 byte: reason
def encode_unbind_indication(reason: int) -> bytes:
    return encode_s_primitive(SPrimitiveType.S_UNBIND_INDICATION, bytes([reason & 0xFF]))


def decode_unbind_indication(payload: bytes) -> dict:
    return {'reason': payload[0]}


# TYPE 6: S_HARD_LINK_ESTABLISH (Fig A-8)
#   Byte0 = LINK_TYPE[7:6] | LINK_PRIORITY[5:4] | REMOTE_SAP_ID[3:0]
#   Bytes1-4 = REMOTE_NODE_ADDRESS (4 bytes, A.2.2.28.1)
def encode_hard_link_establish(link_type: int, link_priority: int,
                               remote_sap: int, remote_node: int) -> bytes:
    byte0 = _pack_hard_link_byte(link_type, link_priority, remote_sap)
    payload = bytes([byte0]) + encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_ESTABLISH, payload)


def decode_hard_link_establish(payload: bytes) -> dict:
    link_type, link_priority, remote_sap = _unpack_hard_link_byte(payload[0])
    remote_node, addr_size, addr_group = decode_address(payload, 1)
    return {
        'link_type': link_type,
        'link_priority': link_priority,
        'remote_sap': remote_sap,
        'remote_node': remote_node,
    }


# TYPE 7: S_HARD_LINK_TERMINATE (Fig A-9)
#   Bytes0-3 = REMOTE_NODE_ADDRESS (4 bytes)
def encode_hard_link_terminate(remote_node: int) -> bytes:
    payload = encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_TERMINATE, payload)


def decode_hard_link_terminate(payload: bytes) -> dict:
    remote_node, addr_size, addr_group = decode_address(payload, 0)
    return {
        'remote_node': remote_node,
    }


# TYPE 8: S_HARD_LINK_ESTABLISHED (Fig A-10)
#   Byte0 = REMOTE_NODE_STATUS
#   Byte1 = LINK_TYPE[7:6] | LINK_PRIORITY[5:4] | REMOTE_SAP_ID[3:0]
#   Bytes2-5 = REMOTE_NODE_ADDRESS (4 bytes)
def encode_hard_link_established(remote_node_status: int,
                                 link_type: int, link_priority: int,
                                 remote_sap: int, remote_node: int) -> bytes:
    byte1 = _pack_hard_link_byte(link_type, link_priority, remote_sap)
    payload = bytes([remote_node_status & 0xFF, byte1])
    payload += encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_ESTABLISHED, payload)


def decode_hard_link_established(payload: bytes) -> dict:
    remote_node_status = payload[0]
    link_type, link_priority, remote_sap = _unpack_hard_link_byte(payload[1])
    remote_node, _, _ = decode_address(payload, 2)
    return {
        'remote_node_status': remote_node_status,
        'link_type': link_type,
        'link_priority': link_priority,
        'remote_sap': remote_sap,
        'remote_node': remote_node,
    }


# TYPE 9: S_HARD_LINK_REJECTED (Fig A-11)
#   Byte0 = REASON
#   Byte1 = LINK_TYPE[7:6] | LINK_PRIORITY[5:4] | REMOTE_SAP_ID[3:0]
#   Bytes2-5 = REMOTE_NODE_ADDRESS (4 bytes)
def encode_hard_link_rejected(reason: int, link_type: int, link_priority: int,
                              remote_sap: int, remote_node: int) -> bytes:
    byte1 = _pack_hard_link_byte(link_type, link_priority, remote_sap)
    payload = bytes([reason & 0xFF, byte1])
    payload += encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_REJECTED, payload)


def decode_hard_link_rejected(payload: bytes) -> dict:
    reason = payload[0]
    link_type, link_priority, remote_sap = _unpack_hard_link_byte(payload[1])
    remote_node, _, _ = decode_address(payload, 2)
    return {
        'reason': reason,
        'link_type': link_type,
        'link_priority': link_priority,
        'remote_sap': remote_sap,
        'remote_node': remote_node,
    }


# TYPE 10: S_HARD_LINK_TERMINATED (Fig A-12)
#   Byte0 = REASON
#   Byte1 = LINK_TYPE[7:6] | LINK_PRIORITY[5:4] | REMOTE_SAP_ID[3:0]
#   Bytes2-5 = REMOTE_NODE_ADDRESS (4 bytes)
def encode_hard_link_terminated(reason: int, link_type: int, link_priority: int,
                                remote_sap: int, remote_node: int) -> bytes:
    byte1 = _pack_hard_link_byte(link_type, link_priority, remote_sap)
    payload = bytes([reason & 0xFF, byte1])
    payload += encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_TERMINATED, payload)


def decode_hard_link_terminated(payload: bytes) -> dict:
    reason = payload[0]
    link_type, link_priority, remote_sap = _unpack_hard_link_byte(payload[1])
    remote_node, _, _ = decode_address(payload, 2)
    return {
        'reason': reason,
        'link_type': link_type,
        'link_priority': link_priority,
        'remote_sap': remote_sap,
        'remote_node': remote_node,
    }


# TYPE 11: S_HARD_LINK_INDICATION (Fig A-13)
#   Byte0 = REMOTE_NODE_STATUS
#   Byte1 = LINK_TYPE[7:6] | LINK_PRIORITY[5:4] | REMOTE_SAP_ID[3:0]
#   Bytes2-5 = REMOTE_NODE_ADDRESS (4 bytes)
def encode_hard_link_indication(remote_node_status: int,
                                link_type: int, link_priority: int,
                                remote_sap: int, remote_node: int) -> bytes:
    byte1 = _pack_hard_link_byte(link_type, link_priority, remote_sap)
    payload = bytes([remote_node_status & 0xFF, byte1])
    payload += encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_INDICATION, payload)


def decode_hard_link_indication(payload: bytes) -> dict:
    remote_node_status = payload[0]
    link_type, link_priority, remote_sap = _unpack_hard_link_byte(payload[1])
    remote_node, _, _ = decode_address(payload, 2)
    return {
        'remote_node_status': remote_node_status,
        'link_type': link_type,
        'link_priority': link_priority,
        'remote_sap': remote_sap,
        'remote_node': remote_node,
    }


# TYPE 12: S_HARD_LINK_ACCEPT (Fig A-14)
#   Byte0 = LINK_TYPE[7:6] | LINK_PRIORITY[5:4] | REMOTE_SAP_ID[3:0]
#   Bytes1-4 = REMOTE_NODE_ADDRESS (4 bytes)
def encode_hard_link_accept(link_type: int, link_priority: int,
                            remote_sap: int, remote_node: int) -> bytes:
    byte0 = _pack_hard_link_byte(link_type, link_priority, remote_sap)
    payload = bytes([byte0]) + encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_ACCEPT, payload)


def decode_hard_link_accept(payload: bytes) -> dict:
    link_type, link_priority, remote_sap = _unpack_hard_link_byte(payload[0])
    remote_node, _, _ = decode_address(payload, 1)
    return {
        'link_type': link_type,
        'link_priority': link_priority,
        'remote_sap': remote_sap,
        'remote_node': remote_node,
    }


# TYPE 13: S_HARD_LINK_REJECT (Fig A-15)
#   Byte0 = REASON
#   Byte1 = LINK_TYPE[7:6] | LINK_PRIORITY[5:4] | REMOTE_SAP_ID[3:0]
#   Bytes2-5 = REMOTE_NODE_ADDRESS (4 bytes)
def encode_hard_link_reject(reason: int, link_type: int, link_priority: int,
                            remote_sap: int, remote_node: int) -> bytes:
    byte1 = _pack_hard_link_byte(link_type, link_priority, remote_sap)
    payload = bytes([reason & 0xFF, byte1])
    payload += encode_address(remote_node)
    return encode_s_primitive(SPrimitiveType.S_HARD_LINK_REJECT, payload)


def decode_hard_link_reject(payload: bytes) -> dict:
    reason = payload[0]
    link_type, link_priority, remote_sap = _unpack_hard_link_byte(payload[1])
    remote_node, _, _ = decode_address(payload, 2)
    return {
        'reason': reason,
        'link_type': link_type,
        'link_priority': link_priority,
        'remote_sap': remote_sap,
        'remote_node': remote_node,
    }


# TYPE 14: S_SUBNET_AVAILABILITY — 2 bytes: status(1) + reason(1)
def encode_subnet_availability(status: int, reason: int = 0) -> bytes:
    payload = struct.pack('BB', status, reason)
    return encode_s_primitive(SPrimitiveType.S_SUBNET_AVAILABILITY, payload)


def decode_subnet_availability(payload: bytes) -> dict:
    return {'status': payload[0], 'reason': payload[1]}


# TYPE 15: S_DATA_FLOW_ON — 0 bytes payload
def encode_data_flow_on() -> bytes:
    return encode_s_primitive(SPrimitiveType.S_DATA_FLOW_ON, b'')


def decode_data_flow_on(payload: bytes) -> dict:
    return {}


# TYPE 16: S_DATA_FLOW_OFF — 0 bytes payload
def encode_data_flow_off() -> bytes:
    return encode_s_primitive(SPrimitiveType.S_DATA_FLOW_OFF, b'')


def decode_data_flow_off(payload: bytes) -> dict:
    return {}


# TYPE 17: S_KEEP_ALIVE — 0 bytes payload
def encode_keep_alive() -> bytes:
    return encode_s_primitive(SPrimitiveType.S_KEEP_ALIVE, b'')


def decode_keep_alive(payload: bytes) -> dict:
    return {}


# TYPE 18: S_MANAGEMENT_MSG_REQUEST — variable: message(N)
def encode_management_msg_request(message: bytes) -> bytes:
    return encode_s_primitive(SPrimitiveType.S_MANAGEMENT_MSG_REQUEST, message)


def decode_management_msg_request(payload: bytes) -> dict:
    return {'message': payload}


# TYPE 19: S_MANAGEMENT_MSG_INDICATION — variable: message(N)
def encode_management_msg_indication(message: bytes) -> bytes:
    return encode_s_primitive(SPrimitiveType.S_MANAGEMENT_MSG_INDICATION, message)


def decode_management_msg_indication(payload: bytes) -> dict:
    return {'message': payload}


# TYPE 20: S_UNIDATA_REQUEST (Fig A-20)
#   Byte0 = PRIORITY[7:4] | DEST_SAP_ID[3:0]
#   Bytes1-4 = DEST_NODE_ADDRESS (4 bytes)
#   Byte5 = DELIVERY_MODE (1 byte)
#   Bytes6-7 = TTL (2 bytes, LE)
#   Bytes8-11 = SOURCE_NODE_ADDRESS (4 bytes)
#   Bytes12-13 = SIZE_OF_U_PDU (2 bytes, LE)
#   Bytes14+ = U_PDU
def encode_unidata_request(priority: int, dest_sap: int, dest_addr: int,
                           delivery_mode_byte: int, ttl: int,
                           updu: bytes, *, src_addr: int = 0) -> bytes:
    byte0 = ((priority & 0x0F) << 4) | (dest_sap & 0x0F)
    payload = bytes([byte0])
    payload += encode_address(dest_addr)
    payload += bytes([delivery_mode_byte])
    payload += struct.pack('<H', ttl)
    payload += encode_address(src_addr)
    payload += struct.pack('<H', len(updu))
    payload += updu
    return encode_s_primitive(SPrimitiveType.S_UNIDATA_REQUEST, payload)


def decode_unidata_request(payload: bytes) -> dict:
    byte0 = payload[0]
    priority = (byte0 >> 4) & 0x0F
    dest_sap = byte0 & 0x0F
    dest_addr, addr_size, addr_group = decode_address(payload, 1)
    delivery_mode = decode_delivery_mode(payload, 5)
    ttl = struct.unpack_from('<H', payload, 6)[0]
    src_addr, _, _ = decode_address(payload, 8)
    updu_size = struct.unpack_from('<H', payload, 12)[0]
    updu = payload[14:14 + updu_size]
    return {
        'priority': priority,
        'dest_sap': dest_sap,
        'dest_addr': dest_addr,
        'addr_size': addr_size,
        'addr_group': addr_group,
        'delivery_mode': delivery_mode,
        'ttl': ttl,
        'src_addr': src_addr,
        'updu': updu,
    }


# TYPE 21: S_UNIDATA_INDICATION (Fig A-21)
#   Byte0 = PRIORITY[7:4] | DEST_SAP_ID[3:0]
#   Bytes1-4 = DEST_NODE_ADDRESS (4 bytes)
#   Byte5 = TRANSMISSION_MODE
#   Byte6 = NOT_USED[7:4] | SOURCE_SAP_ID[3:0]
#   Bytes7-10 = SOURCE_NODE_ADDRESS (4 bytes)
#   Bytes11-12 = SIZE_OF_U_PDU (2 bytes, LE)
#   Bytes13+ = U_PDU
#   (conditional error fields follow U_PDU if TX_MODE = Non-ARQ w/ Errors)
#   Bytes N+0-1 = NUMBER_OF_BLOCKS_IN_ERROR (2 bytes, LE)
#   Bytes N+2.. = ARRAY_OF_BLOCK_ERROR_POINTERS (2 bytes each, LE)
#   Bytes M+0-1 = NUMBER_OF_NON_RECEIVED_BLOCKS (2 bytes, LE)
#   Bytes M+2.. = ARRAY_OF_NON_RECEIVED_BLOCK_POINTERS (2 bytes each, LE)
def encode_unidata_indication(priority: int, dest_sap: int, dest_addr: int,
                              tx_mode: int, src_sap: int, src_addr: int,
                              updu: bytes, *,
                              blocks_in_error: list[int] | None = None,
                              non_received_blocks: list[int] | None = None) -> bytes:
    byte0 = ((priority & 0x0F) << 4) | (dest_sap & 0x0F)
    byte_src_sap = src_sap & 0x0F  # upper nibble NOT_USED = 0
    payload = bytes([byte0])
    payload += encode_address(dest_addr)
    payload += bytes([tx_mode & 0xFF, byte_src_sap])
    payload += encode_address(src_addr)
    payload += struct.pack('<H', len(updu))
    payload += updu
    if blocks_in_error is not None or non_received_blocks is not None:
        bie = blocks_in_error or []
        payload += struct.pack('<H', len(bie))
        for ptr in bie:
            payload += struct.pack('<H', ptr)
        nrb = non_received_blocks or []
        payload += struct.pack('<H', len(nrb))
        for ptr in nrb:
            payload += struct.pack('<H', ptr)
    return encode_s_primitive(SPrimitiveType.S_UNIDATA_INDICATION, payload)


def decode_unidata_indication(payload: bytes) -> dict:
    byte0 = payload[0]
    priority = (byte0 >> 4) & 0x0F
    dest_sap = byte0 & 0x0F
    dest_addr, _, _ = decode_address(payload, 1)
    tx_mode = payload[5]
    src_sap = payload[6] & 0x0F
    src_addr, _, _ = decode_address(payload, 7)
    updu_size = struct.unpack_from('<H', payload, 11)[0]
    updu = payload[13:13 + updu_size]
    result = {
        'priority': priority,
        'dest_sap': dest_sap,
        'dest_addr': dest_addr,
        'tx_mode': tx_mode,
        'src_sap': src_sap,
        'src_addr': src_addr,
        'updu': updu,
    }
    # Conditional error fields
    off = 13 + updu_size
    if off < len(payload):
        n_bie = struct.unpack_from('<H', payload, off)[0]
        off += 2
        bie = []
        for _ in range(n_bie):
            bie.append(struct.unpack_from('<H', payload, off)[0])
            off += 2
        result['blocks_in_error'] = bie
        if off < len(payload):
            n_nrb = struct.unpack_from('<H', payload, off)[0]
            off += 2
            nrb = []
            for _ in range(n_nrb):
                nrb.append(struct.unpack_from('<H', payload, off)[0])
                off += 2
            result['non_received_blocks'] = nrb
    return result


# TYPE 22: S_UNIDATA_REQUEST_CONFIRM
#   Byte0 = NOT_USED[7:4] | DEST_SAP_ID[3:0]
#   Bytes1-4 = DEST_NODE_ADDRESS (4 bytes)
#   Byte5 = NOT_USED[7:4] | SOURCE_SAP_ID[3:0]
#   Bytes6-9 = SOURCE_NODE_ADDRESS (4 bytes)
#   Bytes10-11 = SIZE_OF_U_PDU (2 bytes, LE)
#   Bytes12+ = U_PDU_FIRST_BYTES
def encode_unidata_request_confirm(dest_sap: int, dest_addr: int,
                                   src_sap: int, updu_frag: bytes, *,
                                   src_addr: int = 0) -> bytes:
    payload = bytes([dest_sap & 0x0F])
    payload += encode_address(dest_addr)
    payload += bytes([src_sap & 0x0F])
    payload += encode_address(src_addr)
    payload += struct.pack('<H', len(updu_frag))
    payload += updu_frag
    return encode_s_primitive(SPrimitiveType.S_UNIDATA_REQUEST_CONFIRM, payload)


def decode_unidata_request_confirm(payload: bytes) -> dict:
    dest_sap = payload[0] & 0x0F
    dest_addr, _, _ = decode_address(payload, 1)
    src_sap = payload[5] & 0x0F
    src_addr, _, _ = decode_address(payload, 6)
    frag_size = struct.unpack_from('<H', payload, 10)[0]
    updu_frag = payload[12:12 + frag_size]
    return {
        'dest_sap': dest_sap,
        'dest_addr': dest_addr,
        'src_sap': src_sap,
        'src_addr': src_addr,
        'updu_frag': updu_frag,
    }


# TYPE 23: S_UNIDATA_REQUEST_REJECTED
#   Byte0 = NOT_USED[7:4] | DEST_SAP_ID[3:0]
#   Bytes1-4 = DEST_NODE_ADDRESS (4 bytes)
#   Byte5 = NOT_USED[7:4] | SOURCE_SAP_ID[3:0]
#   Bytes6-9 = SOURCE_NODE_ADDRESS (4 bytes)
#   Byte10 = REASON
#   Bytes11-12 = SIZE_OF_U_PDU (2 bytes, LE)
#   Bytes13+ = U_PDU_FIRST_BYTES
def encode_unidata_request_rejected(dest_sap: int, dest_addr: int,
                                    src_sap: int, reason: int,
                                    updu_frag: bytes, *,
                                    src_addr: int = 0) -> bytes:
    payload = bytes([dest_sap & 0x0F])
    payload += encode_address(dest_addr)
    payload += bytes([src_sap & 0x0F])
    payload += encode_address(src_addr)
    payload += bytes([reason & 0xFF])
    payload += struct.pack('<H', len(updu_frag))
    payload += updu_frag
    return encode_s_primitive(SPrimitiveType.S_UNIDATA_REQUEST_REJECTED, payload)


def decode_unidata_request_rejected(payload: bytes) -> dict:
    dest_sap = payload[0] & 0x0F
    dest_addr, _, _ = decode_address(payload, 1)
    src_sap = payload[5] & 0x0F
    src_addr, _, _ = decode_address(payload, 6)
    reason = payload[10]
    frag_size = struct.unpack_from('<H', payload, 11)[0]
    updu_frag = payload[13:13 + frag_size]
    return {
        'dest_sap': dest_sap,
        'dest_addr': dest_addr,
        'src_sap': src_sap,
        'src_addr': src_addr,
        'reason': reason,
        'updu_frag': updu_frag,
    }


# TYPE 24: S_EXPEDITED_UNIDATA_REQUEST (Sec A.2.1.10)
#   NO PRIORITY field. Layout:
#   Byte0 = DEST_SAP_ID[7:4] | NOT_USED[3:0]
#   Bytes1-4 = DEST_NODE_ADDRESS (4 bytes)
#   Byte5 = DELIVERY_MODE (1 byte)
#   Bytes6-7 = TTL (2 bytes, LE)
#   Bytes8-11 = SOURCE_NODE_ADDRESS (4 bytes)
#   Bytes12-13 = SIZE_OF_U_PDU (2 bytes, LE)
#   Bytes14+ = U_PDU
def encode_expedited_unidata_request(dest_sap: int, dest_addr: int,
                                     delivery_mode_byte: int, ttl: int,
                                     updu: bytes, *, src_addr: int = 0) -> bytes:
    byte0 = (dest_sap & 0x0F) << 4
    payload = bytes([byte0])
    payload += encode_address(dest_addr)
    payload += bytes([delivery_mode_byte])
    payload += struct.pack('<H', ttl)
    payload += encode_address(src_addr)
    payload += struct.pack('<H', len(updu))
    payload += updu
    return encode_s_primitive(SPrimitiveType.S_EXPEDITED_UNIDATA_REQUEST, payload)


def decode_expedited_unidata_request(payload: bytes) -> dict:
    byte0 = payload[0]
    dest_sap = (byte0 >> 4) & 0x0F
    dest_addr, addr_size, addr_group = decode_address(payload, 1)
    delivery_mode = decode_delivery_mode(payload, 5)
    ttl = struct.unpack_from('<H', payload, 6)[0]
    src_addr, _, _ = decode_address(payload, 8)
    updu_size = struct.unpack_from('<H', payload, 12)[0]
    updu = payload[14:14 + updu_size]
    return {
        'dest_sap': dest_sap,
        'dest_addr': dest_addr,
        'addr_size': addr_size,
        'addr_group': addr_group,
        'delivery_mode': delivery_mode,
        'ttl': ttl,
        'src_addr': src_addr,
        'updu': updu,
    }


# TYPE 25: S_EXPEDITED_UNIDATA_REQUEST_CONFIRM — same layout as unidata_request_confirm
def encode_expedited_unidata_request_confirm(dest_sap: int, dest_addr: int,
                                             src_sap: int, updu_frag: bytes, *,
                                             src_addr: int = 0) -> bytes:
    payload = bytes([dest_sap & 0x0F])
    payload += encode_address(dest_addr)
    payload += bytes([src_sap & 0x0F])
    payload += encode_address(src_addr)
    payload += struct.pack('<H', len(updu_frag))
    payload += updu_frag
    return encode_s_primitive(SPrimitiveType.S_EXPEDITED_UNIDATA_REQUEST_CONFIRM, payload)


def decode_expedited_unidata_request_confirm(payload: bytes) -> dict:
    return decode_unidata_request_confirm(payload)


# TYPE 26: S_EXPEDITED_UNIDATA_REQUEST_REJECTED — same layout as unidata_request_rejected
def encode_expedited_unidata_request_rejected(dest_sap: int, dest_addr: int,
                                              src_sap: int, reason: int,
                                              updu_frag: bytes, *,
                                              src_addr: int = 0) -> bytes:
    payload = bytes([dest_sap & 0x0F])
    payload += encode_address(dest_addr)
    payload += bytes([src_sap & 0x0F])
    payload += encode_address(src_addr)
    payload += bytes([reason & 0xFF])
    payload += struct.pack('<H', len(updu_frag))
    payload += updu_frag
    return encode_s_primitive(SPrimitiveType.S_EXPEDITED_UNIDATA_REQUEST_REJECTED, payload)


def decode_expedited_unidata_request_rejected(payload: bytes) -> dict:
    return decode_unidata_request_rejected(payload)


# TYPE 27: S_EXPEDITED_UNIDATA_INDICATION — same layout as unidata_indication
def encode_expedited_unidata_indication(priority: int, dest_sap: int, dest_addr: int,
                                        tx_mode: int, src_sap: int, src_addr: int,
                                        updu: bytes, *,
                                        blocks_in_error: list[int] | None = None,
                                        non_received_blocks: list[int] | None = None) -> bytes:
    byte0 = ((priority & 0x0F) << 4) | (dest_sap & 0x0F)
    byte_src_sap = src_sap & 0x0F
    payload = bytes([byte0])
    payload += encode_address(dest_addr)
    payload += bytes([tx_mode & 0xFF, byte_src_sap])
    payload += encode_address(src_addr)
    payload += struct.pack('<H', len(updu))
    payload += updu
    if blocks_in_error is not None or non_received_blocks is not None:
        bie = blocks_in_error or []
        payload += struct.pack('<H', len(bie))
        for ptr in bie:
            payload += struct.pack('<H', ptr)
        nrb = non_received_blocks or []
        payload += struct.pack('<H', len(nrb))
        for ptr in nrb:
            payload += struct.pack('<H', ptr)
    return encode_s_primitive(SPrimitiveType.S_EXPEDITED_UNIDATA_INDICATION, payload)


def decode_expedited_unidata_indication(payload: bytes) -> dict:
    return decode_unidata_indication(payload)


# ---------------------------------------------------------------------------
# Decoder dispatch table
# ---------------------------------------------------------------------------

DECODERS: dict[int, callable] = {
    SPrimitiveType.S_BIND_REQUEST: decode_bind_request,
    SPrimitiveType.S_UNBIND_REQUEST: decode_unbind_request,
    SPrimitiveType.S_BIND_ACCEPTED: decode_bind_accepted,
    SPrimitiveType.S_BIND_REJECTED: decode_bind_rejected,
    SPrimitiveType.S_UNBIND_INDICATION: decode_unbind_indication,
    SPrimitiveType.S_HARD_LINK_ESTABLISH: decode_hard_link_establish,
    SPrimitiveType.S_HARD_LINK_TERMINATE: decode_hard_link_terminate,
    SPrimitiveType.S_HARD_LINK_ESTABLISHED: decode_hard_link_established,
    SPrimitiveType.S_HARD_LINK_REJECTED: decode_hard_link_rejected,
    SPrimitiveType.S_HARD_LINK_TERMINATED: decode_hard_link_terminated,
    SPrimitiveType.S_HARD_LINK_INDICATION: decode_hard_link_indication,
    SPrimitiveType.S_HARD_LINK_ACCEPT: decode_hard_link_accept,
    SPrimitiveType.S_HARD_LINK_REJECT: decode_hard_link_reject,
    SPrimitiveType.S_SUBNET_AVAILABILITY: decode_subnet_availability,
    SPrimitiveType.S_DATA_FLOW_ON: decode_data_flow_on,
    SPrimitiveType.S_DATA_FLOW_OFF: decode_data_flow_off,
    SPrimitiveType.S_KEEP_ALIVE: decode_keep_alive,
    SPrimitiveType.S_MANAGEMENT_MSG_REQUEST: decode_management_msg_request,
    SPrimitiveType.S_MANAGEMENT_MSG_INDICATION: decode_management_msg_indication,
    SPrimitiveType.S_UNIDATA_REQUEST: decode_unidata_request,
    SPrimitiveType.S_UNIDATA_INDICATION: decode_unidata_indication,
    SPrimitiveType.S_UNIDATA_REQUEST_CONFIRM: decode_unidata_request_confirm,
    SPrimitiveType.S_UNIDATA_REQUEST_REJECTED: decode_unidata_request_rejected,
    SPrimitiveType.S_EXPEDITED_UNIDATA_REQUEST: decode_expedited_unidata_request,
    SPrimitiveType.S_EXPEDITED_UNIDATA_REQUEST_CONFIRM: decode_expedited_unidata_request_confirm,
    SPrimitiveType.S_EXPEDITED_UNIDATA_REQUEST_REJECTED: decode_expedited_unidata_request_rejected,
    SPrimitiveType.S_EXPEDITED_UNIDATA_INDICATION: decode_expedited_unidata_indication,
}


def decode_primitive_auto(stream: bytes) -> tuple[int, dict, int]:
    """Decode one S_PRIMITIVE from stream, auto-dispatching to the right decoder.

    Returns (prim_type, decoded_dict, bytes_consumed).
    """
    prim_type, payload, consumed = decode_s_primitive(stream)
    decoder = DECODERS.get(prim_type)
    if decoder is None:
        raise ValueError(f"Unknown S_PRIMITIVE type {prim_type}")
    return prim_type, decoder(payload), consumed
