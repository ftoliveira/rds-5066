"""Core STANAG 5066 data structures.

Tipos usados pelo CAS (CASEngine/Phase3Node) e pelo DTS (ARQ/Non-ARQ).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


# --- Tipos do CAS (CASEngine, Phase3Node) ---


# --- Tipos do DTS (D_PDU framing, ARQ, Non-ARQ) ---

SYNC_BYTES = b"\x90\xEB"
MAX_ADDRESS_BITS = 28
MAX_DATA_BYTES = 1023


class DPDUType(IntEnum):
    DATA_ONLY = 0
    ACK_ONLY = 1
    DATA_ACK = 2
    RESETWIN_RESYNC = 3
    EXPEDITED_DATA_ONLY = 4
    EXPEDITED_ACK_ONLY = 5
    MANAGEMENT = 6
    NON_ARQ = 7
    EXPEDITED_NON_ARQ = 8
    WARNING = 15


class CPDUType(IntEnum):
    DATA = 0
    LINK_REQUEST = 1
    LINK_ACCEPTED = 2
    LINK_REJECTED = 3
    LINK_BREAK = 4
    LINK_BREAK_CONFIRM = 5


class CPDURejectReason(IntEnum):
    """Razões PHYSICAL_LINK_REJECTED (Annex B, Figure B-4)."""
    REASON_UNKNOWN = 0
    BROADCAST_ONLY_NODE = 1
    HIGHER_PRIORITY_LINK_REQUEST_PENDING = 2
    SUPPORTING_EXCLUSIVE_LINK = 3
    # 4-15: unspecified


class CPDUBreakReason(IntEnum):
    """Razões PHYSICAL_LINK_BREAK (Annex B, Figure B-5)."""
    REASON_UNKNOWN = 0
    HIGHER_LAYER_REQUEST = 1
    SWITCHING_TO_BROADCAST = 2
    HIGHER_PRIORITY_LINK_REQUEST_PENDING = 3
    NO_MORE_DATA = 4
    # 5-15: unspecified


class PhysicalLinkType(IntEnum):
    """Tipo de enlace físico (Annex B, B.3.1.2 LINK field)."""
    NONEXCLUSIVE = 0  # Soft Link Data Exchange
    EXCLUSIVE = 1     # Hard Link Data Exchange


class CasLinkState(StrEnum):
    IDLE = "IDLE"
    CALLING = "CALLING"
    MADE = "MADE"
    BREAKING = "BREAKING"
    FAILED = "FAILED"


class NonArqDeliveryKind(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


DATA_DPDU_TYPES = {
    DPDUType.DATA_ONLY,
    DPDUType.DATA_ACK,
    DPDUType.EXPEDITED_DATA_ONLY,
}
ACK_DPDU_TYPES = {
    DPDUType.ACK_ONLY,
    DPDUType.EXPEDITED_ACK_ONLY,
}
NON_ARQ_DPDU_TYPES = {
    DPDUType.NON_ARQ,
    DPDUType.EXPEDITED_NON_ARQ,
}
DATA_CRC_DPDU_TYPES = DATA_DPDU_TYPES | NON_ARQ_DPDU_TYPES


@dataclass(slots=True, frozen=True)
class Address:
    """Variable-size STANAG address pair."""

    destination: int = 0
    source: int = 0
    size: int = 1

    def __post_init__(self) -> None:
        if not (0 <= self.destination < (1 << MAX_ADDRESS_BITS)):
            raise ValueError("Destination address must fit in 28 bits")
        if not (0 <= self.source < (1 << MAX_ADDRESS_BITS)):
            raise ValueError("Source address must fit in 28 bits")
        if not (1 <= self.size <= 7):
            raise ValueError("Address size must be between 1 and 7")
        max_bits = self.bit_width_per_endpoint
        if self.destination >= (1 << max_bits):
            raise ValueError("Destination address does not fit selected size")
        if self.source >= (1 << max_bits):
            raise ValueError("Source address does not fit selected size")

    @property
    def nibble_width_per_endpoint(self) -> int:
        return self.size

    @property
    def bit_width_per_endpoint(self) -> int:
        return self.nibble_width_per_endpoint * 4

    @property
    def total_wire_bytes(self) -> int:
        return self.size

    @classmethod
    def auto(cls, destination: int, source: int) -> "Address":
        max_value = max(destination, source, 0)
        nibble_width = max(1, (max_value.bit_length() + 3) // 4)
        if nibble_width > 7:
            raise ValueError("Address exceeds 28-bit STANAG limit")
        return cls(destination=destination, source=source, size=nibble_width)


@dataclass(slots=True)
class DataHeader:
    pdu_start: bool = False
    pdu_end: bool = False
    deliver_in_order: bool = False
    drop_pdu: bool = False
    tx_uwe: bool = False
    tx_lwe: bool = False
    data_size: int = 0
    tx_frame_seq: int = 0
    cpdu_id: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.data_size <= MAX_DATA_BYTES):
            raise ValueError("Data size must be between 0 and 1023 bytes")
        if not (0 <= self.tx_frame_seq <= 0xFF):
            raise ValueError("Frame sequence must fit in 8 bits")
        if not (0 <= self.cpdu_id <= 0xFF):
            raise ValueError("C_PDU id must fit in 8 bits")


@dataclass(slots=True)
class AckHeader:
    rx_lwe: int = 0
    sel_acks: bytes = b""

    def __post_init__(self) -> None:
        if not (0 <= self.rx_lwe <= 0xFF):
            raise ValueError("RX LWE must fit in 8 bits")
        if len(self.sel_acks) > 16:
            raise ValueError("Selective ACK bitmap must be at most 16 bytes")


@dataclass(slots=True)
class ResetHeader:
    full_reset_cmd: bool = False
    reset_tx_win_req: bool = False
    reset_rx_win_cmd: bool = False
    reset_ack: bool = False
    new_rx_lwe: int = 0
    reset_frame_id: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.new_rx_lwe <= 0xFF):
            raise ValueError("New RX LWE must fit in 8 bits")
        if not (0 <= self.reset_frame_id <= 0xFF):
            raise ValueError("Reset frame id must fit in 8 bits")


@dataclass(slots=True)
class ManagementHeader:
    message_field: int = 0
    message_ack: bool = False
    valid_message: bool = True  # C.3.9: VALID MESSAGE bit (Edition 3)
    management_frame_id: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.message_field <= 0x0FFF):
            raise ValueError("Management message field must fit in 12 bits")
        if not (0 <= self.management_frame_id <= 0xFF):
            raise ValueError("Management frame id must fit in 8 bits")

    @property
    def msg_type(self) -> int:
        return self.message_field & 0x0F

    @property
    def message_contents(self) -> int:
        return (self.message_field >> 4) & 0xFF


@dataclass(slots=True)
class NonArqHeader:
    cpdu_reception_window: int = 0
    first_byte_position: int = 0
    cpdu_size: int = 0
    group_address: bool = False
    deliver_in_order: bool = False
    cpdu_id: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.cpdu_reception_window <= 0xFFFF):
            raise ValueError("C_PDU reception window must fit in 16 bits")
        if not (0 <= self.first_byte_position <= 0xFFFF):
            raise ValueError("First byte position must fit in 16 bits")
        if not (0 <= self.cpdu_size <= 0xFFFF):
            raise ValueError("C_PDU size must fit in 16 bits")
        if not (0 <= self.cpdu_id <= 0x0FFF):
            raise ValueError("C_PDU id must fit in 12 bits")


@dataclass(slots=True)
class WarningHeader:
    received_dpdu_type: int = 0
    reason: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.received_dpdu_type <= 0x0F):
            raise ValueError("Received DPDU type must fit in 4 bits")
        if not (0 <= self.reason <= 0x0F):
            raise ValueError("Warning reason must fit in 4 bits")


CAS_LOCAL_TIMEOUT = -1  # Sentinela interna, não é valor wire


@dataclass(slots=True, frozen=True)
class CPDU:
    """Channel Access PDU (Annex B). Byte 0: [TYPE(4)][FIELD(4)]."""

    cpdu_type: CPDUType
    payload: bytes = b""
    reason: int = 0
    link_type: int = 0  # 0=Nonexclusive, 1=Exclusive (LINK_REQUEST only)

    def __post_init__(self) -> None:
        if not (0 <= self.reason <= 0x0F):
            raise ValueError("CPDU reason must fit in 4 bits")


@dataclass(slots=True, frozen=True)
class NonArqDelivery:
    """Delivery event emitted by the non-ARQ engine."""

    dpdu_type: DPDUType
    source: int
    destination: int
    cpdu_id: int
    payload: bytes
    complete: bool
    error: bool
    kind: NonArqDeliveryKind
    first_byte_position: int = 0
    cpdu_size: int = 0


@dataclass(slots=True, frozen=True)
class CasEvent:
    """Observable CAS event for tests and integration."""

    state: CasLinkState
    remote: int | None = None
    cpdu: CPDU | None = None
    reason: int = 0


@dataclass(slots=True)
class DPDU:
    """Decoded or user-constructed DPDU."""

    dpdu_type: DPDUType
    eow: int = 0
    eot: int = 0
    address: Address = field(default_factory=Address)
    data: DataHeader | None = None
    ack: AckHeader | None = None
    reset: ResetHeader | None = None
    management: ManagementHeader | None = None
    non_arq: NonArqHeader | None = None
    warning: WarningHeader | None = None
    user_data: bytes = b""
    header_crc: int | None = None
    data_crc: int | None = None
    header_crc_ok: bool | None = None
    data_crc_ok: bool | None = None
    raw_bytes: bytes = b""

    def __post_init__(self) -> None:
        if not (0 <= self.eow <= 0x0FFF):
            raise ValueError("EOW must fit in 12 bits")
        if not (0 <= self.eot <= 0xFF):
            raise ValueError("EOT must fit in 8 bits")
        if len(self.user_data) > MAX_DATA_BYTES:
            raise ValueError("User data exceeds maximum phase 1 DPDU size")

    def requires_data_crc(self) -> bool:
        return self.dpdu_type in DATA_CRC_DPDU_TYPES


# --- Tipos do SIS (Subnetwork Interface Sublayer, Annex A) ---


class TxMode(IntEnum):
    """Modos de transmissão SIS (Annex A)."""

    ARQ = 0
    NON_ARQ = 1
    EXPEDITED_NON_ARQ = 2


class SisRejectReason(IntEnum):
    """Razões de rejeição de primitivas S_UNIDATA (Annex A Table A-1)."""

    TTL_EXPIRED = 1
    DEST_SAP_NOT_BOUND = 2
    DEST_NODE_NOT_RESPONDING = 3
    MTU_EXCEEDED = 4
    TX_MODE_NOT_SPECIFIED = 5
    # Extensões locais (não no Annex A)
    FLOW_CONTROL = 16
    SAP_NOT_BOUND = 17
    LINK_FAILED = 18
    DEST_UNKNOWN = 19


class SisHardLinkType(IntEnum):
    """Tipo de Hard Link (Annex A A.2, Link Type argument)."""

    NO_RESERVATION = 0  # Enlace mantido, todos os clientes usam
    PARTIAL = 1  # Apenas cliente solicitante <-> qualquer cliente remoto
    FULL = 2  # Apenas cliente solicitante <-> SAP remoto específico


class SisHardLinkRejectReason(IntEnum):
    """Razões S_HARD_LINK_REJECTED (Annex A)."""

    REMOTE_NODE_BUSY = 1
    HIGHER_PRIORITY_LINK_EXISTING = 2
    REMOTE_NODE_NOT_RESPONDING = 3
    DEST_SAP_NOT_BOUND = 4
    REQUESTED_TYPE0_EXISTS = 5  # Edition 3


class SisHardLinkTerminateReason(IntEnum):
    """Razões S_HARD_LINK_TERMINATED (Annex A)."""

    LINK_TERMINATED_BY_REMOTE = 1
    HIGHER_PRIORITY_LINK_REQUESTED = 2
    REMOTE_NODE_NOT_RESPONDING = 3
    DEST_SAP_UNBOUND = 4
    PHYSICAL_LINK_BROKEN = 5  # Edition 3


class SisDataDeliveryFailReason(IntEnum):
    """Razões DATA DELIVERY FAIL S_PDU tipo 2 (Annex A)."""

    DEST_SAP_NOT_BOUND = 1


class SisBindRejectReason(IntEnum):
    """Razões S_BIND_REJECTED (Annex A)."""

    NOT_ENOUGH_RESOURCES = 1
    INVALID_SAP_ID = 2
    SAP_ALREADY_ALLOCATED = 3
    ARQ_MODE_UNSUPPORTABLE = 4  # Edition 3


class LinkType(StrEnum):
    """Tipo de sessão de enlace SIS."""

    SOFT = "SOFT"
    HARD = "HARD"


class SisLinkSessionState(StrEnum):
    """Estado da sessão de enlace gerenciada pelo SIS."""

    IDLE = "IDLE"
    ESTABLISHING = "ESTABLISHING"
    ACTIVE = "ACTIVE"
    TERMINATING = "TERMINATING"


@dataclass(frozen=True)
class ServiceType:
    """Tipos de serviço suportados por um SAP (Annex A, Fig A-3).

    Wire format (2 bytes / 16 bits):
        Bits [15:14] = Transmission Mode (0=ARQ, 1=NON_ARQ, 2=both, 3=reserved)
        Bits [13:12] = Delivery Confirmation (0=none, 1=at node, 2=at client, 3=reserved)
        Bit  [11]    = Delivery Order
        Bit  [10]    = Extended Field
        Bits [9:6]   = Min Retransmissions (4 bits)
        Bits [5:0]   = Reserved / Not Used
    """

    transmission_mode: int = 2       # 0=ARQ only, 1=NON_ARQ only, 2=both
    delivery_confirmation: int = 0   # 0=none, 1=node, 2=client
    delivery_order: bool = False
    extended: bool = False
    min_retransmissions: int = 0     # 4-bit field

    # Convenience backward-compat properties
    @property
    def arq(self) -> bool:
        return self.transmission_mode in (0, 2)

    @property
    def non_arq(self) -> bool:
        return self.transmission_mode in (1, 2)

    @property
    def expedited(self) -> bool:
        return self.extended


@dataclass(frozen=True)
class DeliveryMode:
    """Modo de entrega para uma requisição S_UNIDATA (Annex A).

    Campos conforme Annex A, S_UNIDATA_REQUEST arguments:
      arq_mode: True=ARQ, False=non-ARQ
      node_delivery_confirm: NODE DELIVERY confirmation
      client_delivery_confirm: CLIENT DELIVERY confirmation
      in_order: IN-ORDER DELIVERY (deliver_in_order flag no D_PDU)
      expedited: EXPEDITED non-ARQ
    """

    arq_mode: bool = True
    node_delivery_confirm: bool = False
    client_delivery_confirm: bool = False
    in_order: bool = False
    expedited: bool = False


@dataclass(slots=True)
class SPDU:
    """S_PDU: unidade de dados do SIS encapsulando U_PDU (Annex A)."""

    version: int = 1
    src_sap: int = 0
    dest_sap: int = 0
    priority: int = 0
    ttd: float = 0.0  # absoluto (time.time())
    tx_mode: int = 0  # TxMode
    node_delivery_confirm_required: bool = False
    client_delivery_confirm_required: bool = False
    deliver_in_order: bool = False
    updu: bytes = b""


# --- Tipos S_PDU (Annex A Fig. A-3, A-4) ---
SPDU_TYPE_DATA = 0
SPDU_TYPE_DATA_DELIVERY_CONFIRM = 1
SPDU_TYPE_DATA_DELIVERY_FAIL = 2
SPDU_TYPE_HARD_LINK_ESTABLISH_REQUEST = 3
SPDU_TYPE_HARD_LINK_ESTABLISH_CONFIRM = 4
SPDU_TYPE_HARD_LINK_ESTABLISH_REJECTED = 5
SPDU_TYPE_HARD_LINK_TERMINATE = 6
SPDU_TYPE_HARD_LINK_TERMINATE_CONFIRM = 7


@dataclass(slots=True, frozen=True)
class SisUnidataIndication:
    """Indicação de dados recebidos entregue ao cliente SIS."""

    dest_sap: int
    src_addr: int
    src_sap: int
    priority: int
    updu: bytes


# --- S_PRIMITIVE types (Annex A §A.2.2, Edition 3) ---


class SPrimitiveType(IntEnum):
    """Tipos de S_PRIMITIVE para codificação wire (Annex A §A.2.2)."""

    S_BIND_REQUEST = 1
    S_UNBIND_REQUEST = 2
    S_BIND_ACCEPTED = 3
    S_BIND_REJECTED = 4
    S_UNBIND_INDICATION = 5
    S_HARD_LINK_ESTABLISH = 6
    S_HARD_LINK_TERMINATE = 7
    S_HARD_LINK_ESTABLISHED = 8
    S_HARD_LINK_REJECTED = 9
    S_HARD_LINK_TERMINATED = 10
    S_HARD_LINK_INDICATION = 11
    S_HARD_LINK_ACCEPT = 12
    S_HARD_LINK_REJECT = 13
    S_SUBNET_AVAILABILITY = 14
    S_DATA_FLOW_ON = 15
    S_DATA_FLOW_OFF = 16
    S_KEEP_ALIVE = 17
    S_MANAGEMENT_MSG_REQUEST = 18
    S_MANAGEMENT_MSG_INDICATION = 19
    S_UNIDATA_REQUEST = 20
    S_UNIDATA_INDICATION = 21
    S_UNIDATA_REQUEST_CONFIRM = 22
    S_UNIDATA_REQUEST_REJECTED = 23
    S_EXPEDITED_UNIDATA_REQUEST = 24
    S_EXPEDITED_UNIDATA_REQUEST_CONFIRM = 25
    S_EXPEDITED_UNIDATA_REQUEST_REJECTED = 26
    S_EXPEDITED_UNIDATA_INDICATION = 27


# --- SAP ID assignments (Table F-1 Edition 3) ---

SAP_ID_SUBNET_MANAGEMENT = 0
SAP_ID_COSS = 1
SAP_ID_TMMHS = 2
SAP_ID_HMTP = 3
SAP_ID_HFPOP = 4
SAP_ID_HFCHAT = 5
SAP_ID_RCOP = 6
SAP_ID_UDOP = 7
SAP_ID_ETHER = 8
SAP_ID_IP_CLIENT = 9
SAP_ID_CFTP = 12
# Portas não atribuídas — "UNASSIGNED – available for arbitrary use" (Tabela F-1)
SAP_ID_UNASSIGNED_13 = 13
SAP_ID_UNASSIGNED_14 = 14
SAP_ID_UNASSIGNED_15 = 15
