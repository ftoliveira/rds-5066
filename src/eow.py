"""EOW (Engineering Orderwire) message parser/builder per STANAG 5066 Edition 3 Annex C.5.

The 12-bit EOW field is present in every D_PDU common header.  The lower
4 bits define the *message type* and the upper 8 bits carry the *message
content* whose meaning depends on the type:

    Type 0 — Capability advertising (bitmap, Table C-4)
    Type 1 — Data Rate Change (DRC) Request: rate(4) | interleave(2) | other(2)
    Type 2 — DRC Response: response(3) | reason(5)
    Type 3 — Unrecognized Type Error: unrecognized type in lower 4 bits
    Type 4 — Capability Advertisement: bitmap 8 bits
    Types 5/6 — Frequency Change (reserved for Annex I)
    Type 7 — HDR Change Request (Extended EOW, embedded in MGMT D_PDU)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# EOW message types (C.5, Table C-4, Edition 3)
# ---------------------------------------------------------------------------

class EOWType(IntEnum):
    """EOW message types (Annex C.5, Table C-4)."""

    CAPABILITY = 0
    DRC_REQUEST = 1           # Data Rate Change Request (Type 1)
    DRC_RESPONSE = 2          # Data Rate Change Response (Type 2)
    UNRECOGNIZED_TYPE = 3     # Unrecognized Type Error (Type 3)
    CAPABILITY_ADVERTISEMENT = 4  # Capability Advertisement (Type 4)
    FREQUENCY_CHANGE_REQ = 5  # Reserved for Annex I
    FREQUENCY_CHANGE_RSP = 6  # Reserved for Annex I
    HDR_CHANGE_REQUEST = 7    # Extended EOW in MGMT D_PDU (Type 7)


# ---------------------------------------------------------------------------
# Data Rate Codes (Table C-18 / C-6)
# ---------------------------------------------------------------------------

class DRCDataRate(IntEnum):
    """HF modem data rate codes used in DRC EOW (Annex C Table C-6)."""

    BPS_75 = 0
    BPS_150 = 1
    BPS_300 = 2
    BPS_600 = 3
    BPS_1200 = 4
    BPS_2400 = 5
    BPS_3200 = 6
    BPS_3600 = 7
    BPS_4800 = 8
    BPS_6400 = 9
    BPS_8000 = 10
    BPS_9600 = 11


DRC_RATE_TO_BPS: dict[int, int] = {
    0: 75, 1: 150, 2: 300, 3: 600, 4: 1200,
    5: 2400, 6: 3200, 7: 3600, 8: 4800, 9: 6400, 10: 8000, 11: 9600,
}


# ---------------------------------------------------------------------------
# Interleave modes (Table C-6): 2-bit field
# ---------------------------------------------------------------------------

class InterleaveMode(IntEnum):
    """Interleaver parameter (Table C-6, 2 bits)."""
    NONE = 0
    SHORT = 1
    LONG = 2
    RESERVED = 3


# ---------------------------------------------------------------------------
# DRC Response codes (Table C-7, Type 2)
# ---------------------------------------------------------------------------

class DRCResponseCode(IntEnum):
    """DRC Response field (3 bits, Table C-7)."""
    ACCEPT = 0b000
    REFUSE = 0b001
    CANCEL = 0b010
    CONFIRM = 0b011


class DRCRefuseReason(IntEnum):
    """DRC Refuse reason codes (5 bits, Table C-8)."""
    UNKNOWN = 0
    BUSY = 1
    RATE_NOT_SUPPORTED = 2
    INTERLEAVE_NOT_SUPPORTED = 3
    OTHER_PARAMS_NOT_SUPPORTED = 4
    INCOMPATIBLE_CONFIGURATION = 5


# ---------------------------------------------------------------------------
# Capability Advertisement bits (Type 4, 8-bit bitmap)
# ---------------------------------------------------------------------------

CAP_DRC_CAPABLE = 0x01
CAP_STANAG_4529 = 0x02
CAP_MIL_STD_188_110A = 0x04
CAP_MIL_STD_188_110B = 0x08


# ---------------------------------------------------------------------------
# Parsed EOW dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DRCRequestParams:
    """Decoded DRC Request parameters from EOW Type 1."""

    data_rate_code: int
    data_rate_bps: int
    interleave_mode: int  # InterleaveMode (0-3)
    other_params: int     # 2 bits reserved

    @property
    def long_interleave(self) -> bool:
        """Backward-compat: True if interleave_mode == LONG."""
        return self.interleave_mode == InterleaveMode.LONG


@dataclass(frozen=True)
class DRCResponseParams:
    """Decoded DRC Response parameters from EOW Type 2."""

    response: int   # DRCResponseCode (3 bits)
    reason: int     # DRCRefuseReason (5 bits)


@dataclass(frozen=True)
class DRCParams:
    """Backward-compatible DRC params (wraps DRCRequestParams)."""

    data_rate_code: int
    data_rate_bps: int
    long_interleave: bool
    interleave_mode: int = 2  # default LONG for compat


@dataclass(frozen=True)
class EOWMessage:
    """Parsed EOW message from the D_PDU common header."""

    msg_type: int       # lower 4 bits
    msg_content: int    # upper 8 bits
    drc_request: Optional[DRCRequestParams] = None
    drc_response: Optional[DRCResponseParams] = None
    unrecognized_type: Optional[int] = None
    capability_bitmap: Optional[int] = None
    # Backward compat alias
    drc: Optional[DRCParams] = None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def parse_eow(eow: int) -> EOWMessage:
    """Parse the 12-bit EOW field into a typed message."""
    msg_type = eow & 0x00F
    msg_content = (eow >> 4) & 0x0FF

    drc_request: Optional[DRCRequestParams] = None
    drc_response: Optional[DRCResponseParams] = None
    drc_compat: Optional[DRCParams] = None
    unrecognized: Optional[int] = None
    cap_bitmap: Optional[int] = None

    if msg_type == EOWType.DRC_REQUEST:
        rate_code = (msg_content >> 4) & 0x0F
        interleave = (msg_content >> 2) & 0x03
        other = msg_content & 0x03
        drc_request = DRCRequestParams(
            data_rate_code=rate_code,
            data_rate_bps=DRC_RATE_TO_BPS.get(rate_code, 0),
            interleave_mode=interleave,
            other_params=other,
        )
        # Backward compat
        drc_compat = DRCParams(
            data_rate_code=rate_code,
            data_rate_bps=DRC_RATE_TO_BPS.get(rate_code, 0),
            long_interleave=(interleave == InterleaveMode.LONG),
            interleave_mode=interleave,
        )

    elif msg_type == EOWType.DRC_RESPONSE:
        response = (msg_content >> 5) & 0x07
        reason = msg_content & 0x1F
        drc_response = DRCResponseParams(response=response, reason=reason)

    elif msg_type == EOWType.UNRECOGNIZED_TYPE:
        unrecognized = msg_content & 0x0F  # lower 4 bits = unrecognized type

    elif msg_type == EOWType.CAPABILITY_ADVERTISEMENT:
        cap_bitmap = msg_content

    return EOWMessage(
        msg_type=msg_type,
        msg_content=msg_content,
        drc_request=drc_request,
        drc_response=drc_response,
        unrecognized_type=unrecognized,
        capability_bitmap=cap_bitmap,
        drc=drc_compat,
    )


def build_eow_drc(data_rate_code: int, long_interleave: bool = False,
                   interleave_mode: Optional[int] = None) -> int:
    """Build a 12-bit EOW field for DRC Request (Type 1).

    If interleave_mode is given, it takes precedence over long_interleave.
    """
    if interleave_mode is None:
        interleave_mode = InterleaveMode.LONG if long_interleave else InterleaveMode.SHORT
    content = ((data_rate_code & 0x0F) << 4) | ((interleave_mode & 0x03) << 2)
    return (content << 4) | EOWType.DRC_REQUEST


def build_eow_drc_response(response: int, reason: int = 0) -> int:
    """Build a 12-bit EOW field for DRC Response (Type 2).

    response: DRCResponseCode (3 bits)
    reason: DRCRefuseReason (5 bits)
    """
    content = ((response & 0x07) << 5) | (reason & 0x1F)
    return (content << 4) | EOWType.DRC_RESPONSE


def build_eow_unrecognized(unrecognized_type: int) -> int:
    """Build a 12-bit EOW field for Unrecognized Type Error (Type 3)."""
    content = unrecognized_type & 0x0F
    return (content << 4) | EOWType.UNRECOGNIZED_TYPE


def build_eow_capability(bitmap: int) -> int:
    """Build a 12-bit EOW field for Capability Advertisement (Type 4)."""
    return ((bitmap & 0xFF) << 4) | EOWType.CAPABILITY_ADVERTISEMENT


def build_eow_version(version: int = 0) -> int:
    """Build a 12-bit EOW for version (uses Type 3 slot — legacy compat)."""
    return ((version & 0xFF) << 4) | EOWType.UNRECOGNIZED_TYPE


# ---------------------------------------------------------------------------
# EOW Type 7 — HDR Change Request (Annex C.5.5, Table C-9-1 / C-9-2 / C-9-4)
# ---------------------------------------------------------------------------

class HDRWaveform(IntEnum):
    """Modem waveform codes for the HDR Change Request (Table C-9-2)."""

    MS110A = 0
    MS110B = 1
    STANAG_4285 = 2
    STANAG_4539 = 3
    STANAG_4529 = 4
    STANAG_4415 = 5
    STANAG_4481_FSK = 6
    USER_CONFIGURATION_OPTION_1 = 7
    USER_CONFIGURATION_OPTION_2 = 8
    USER_CONFIGURATION_OPTION_3 = 9
    # 10..31 unspecified


# Tamanho fixo do Extended Message field (Table C-9-4):
#  bytes 0-3 = Data rate (32 bits, MSB at offset 0)
#  bytes 4-5 = Interleaver length (16 bits, hundredths of seconds)
HDR_EXTENDED_MESSAGE_SIZE = 6


@dataclass(slots=True, frozen=True)
class HDRChangeRequestEOW:
    """Conteúdo decodificado de um EOW Type 7 (Tabela C-9-1)."""

    waveform: int          # 5 bits (HDRWaveform)
    number_of_channels: int  # 1..8 — em wire: 1..7 + 0 representando 8


def build_eow_hdr_change_request(
    waveform: int,
    number_of_channels: int,
) -> int:
    """Constrói o campo EOW (12 bits) para Type 7 HDR Change Request.

    Layout (Tabela C-9-1):
      bits 11-8 : TYPE = 7
      bits 7-3  : MODEM WAVEFORM (5 bits, Tabela C-9-2)
      bits 2-0  : NUMBER OF CHANNELS (3 bits; 1..7 + 0=8)

    ``number_of_channels`` deve estar em 1..8; o valor 8 é codificado como
    binário 000 conforme C.5.5 §5.
    """
    if not (0 <= waveform <= 0x1F):
        raise ValueError(f"waveform deve estar em 0-31 (5 bits), got {waveform}")
    if not (1 <= number_of_channels <= 8):
        raise ValueError(
            f"number_of_channels deve estar em 1-8, got {number_of_channels}"
        )
    channels_wire = 0 if number_of_channels == 8 else number_of_channels
    type_field = int(EOWType.HDR_CHANGE_REQUEST) & 0x0F  # 7 = 0b0111
    return (type_field << 8) | ((waveform & 0x1F) << 3) | (channels_wire & 0x07)


def parse_eow_hdr_change_request(eow: int) -> HDRChangeRequestEOW:
    """Decodifica um EOW Type 7 (12 bits). Levanta ValueError se TYPE != 7."""
    type_field = (eow >> 8) & 0x0F
    if type_field != int(EOWType.HDR_CHANGE_REQUEST):
        raise ValueError(
            f"EOW não é Type 7 (got TYPE={type_field}, esperado 7)"
        )
    waveform = (eow >> 3) & 0x1F
    channels_wire = eow & 0x07
    number_of_channels = 8 if channels_wire == 0 else channels_wire
    return HDRChangeRequestEOW(
        waveform=waveform, number_of_channels=number_of_channels,
    )


def is_eow_hdr_change_request(eow: int) -> bool:
    """True se o EOW de 12 bits codifica um Type 7 (HDR Change Request)."""
    return ((eow >> 8) & 0x0F) == int(EOWType.HDR_CHANGE_REQUEST)


@dataclass(slots=True, frozen=True)
class HDRExtendedMessage:
    """Campo Extended Message do MGMT D_PDU para HDR (Tabela C-9-4)."""

    data_rate_bps: int              # 32 bits, MSB at offset 0
    interleaver_centiseconds: int   # 16 bits


def build_hdr_extended_message(
    data_rate_bps: int,
    interleaver_centiseconds: int,
) -> bytes:
    """Constrói os 6 bytes do Extended Message field para HDR (Tabela C-9-4)."""
    if not (0 <= data_rate_bps <= 0xFFFFFFFF):
        raise ValueError(
            f"data_rate_bps deve caber em 32 bits, got {data_rate_bps}"
        )
    if not (0 <= interleaver_centiseconds <= 0xFFFF):
        raise ValueError(
            "interleaver_centiseconds deve caber em 16 bits, got "
            f"{interleaver_centiseconds}"
        )
    return data_rate_bps.to_bytes(4, "big") + interleaver_centiseconds.to_bytes(2, "big")


def parse_hdr_extended_message(payload: bytes) -> HDRExtendedMessage:
    """Decodifica os 6 bytes do Extended Message do HDR Change Request."""
    if len(payload) != HDR_EXTENDED_MESSAGE_SIZE:
        raise ValueError(
            f"HDR Extended Message deve ter {HDR_EXTENDED_MESSAGE_SIZE} bytes, "
            f"got {len(payload)}"
        )
    data_rate = int.from_bytes(payload[:4], "big")
    interleaver = int.from_bytes(payload[4:6], "big")
    return HDRExtendedMessage(
        data_rate_bps=data_rate, interleaver_centiseconds=interleaver,
    )
