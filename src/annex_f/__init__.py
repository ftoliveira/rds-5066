"""
Anexo F — Clientes de Sub-rede STANAG 5066.

Re-exporta classes públicas para uso conveniente:
    from src.annex_f import AnnexFDispatcher, UnackMessageClient, ...
"""

from .updu import (
    UPDU_HEADER_SIZE,
    UPDUHeader,
    decode_updu,
    encode_updu,
    segment_updu,
    ReassemblyContext,
)
from .base_client import SubnetClient, AnnexFDispatcher
from .unack_message import UnackMessageClient
from .ack_message import AckMessageClient, AckMessageServer, SendMessage
from .orderwire import OrderwireClient
from .hmtp import HMTPClient, HMTPServer, MailMessage
from .hf_pop3 import HFPOP3Client, HFPOP3Server, StoredMessage
from .fab import FABGenerator, FABReceiver
from .ip_client import IPClient, QoSMode
from .rcop import (
    APP_ID_BFTP,
    APP_ID_FRAP,
    APP_ID_FRAPV2,
    APP_ID_TMMHS_TMI1,
    APP_ID_TMMHS_TMI2,
    APP_ID_TMMHS_TMI3,
    APP_ID_TMMHS_TMI4,
    APP_ID_TMMHS_TMI5,
    RCOP_HEADER_SIZE,
    RcopPDU,
    encode_rcop_pdu,
    decode_rcop_pdu,
    RcopClient,
    UdopClient,
)
from .bftp import BftpClient, FrapClient, FrapV2Client
from .cftp import CftpClient, CftpMessage
from .ether_client import (
    EtherFrame,
    EtherClient,
    encode_ec_frame,
    decode_ec_frame,
    stanag_addr_to_pseudo_ether,
    pseudo_ether_to_stanag_addr,
    ETHERTYPE_IPV4,
    ETHERTYPE_IPV6,
    ETHERTYPE_ARP,
    ETHERTYPE_PPP,
    ETHERTYPE_VJCOMP,
    ETHERTYPE_ROHC,
)
from .coss import CossClient, CossMode, CharacterEncoder
from .subnet_mgmt import SubnetMgmtClient

__all__ = [
    # UPDU
    "UPDU_HEADER_SIZE",
    "UPDUHeader",
    "decode_updu",
    "encode_updu",
    "segment_updu",
    "ReassemblyContext",
    # Base
    "SubnetClient",
    "AnnexFDispatcher",
    # F.15 — Mensagem Reconhecida (SAP 13 — porta não atribuída)
    "AckMessageClient",
    "AckMessageServer",
    "SendMessage",
    # F.15 — Mensagem Não Reconhecida (SAP 14 — porta não atribuída)
    "UnackMessageClient",
    # F.5 HMTP (SAP 3)
    "HMTPClient",
    "HMTPServer",
    "MailMessage",
    # F.6 HFPOP (SAP 4)
    "HFPOP3Client",
    "HFPOP3Server",
    "StoredMessage",
    # F.7 Orderwire / HFCHAT (SAP 5)
    "OrderwireClient",
    # FAB Generator/Receiver
    "FABGenerator",
    "FABReceiver",
    # F.12 IP Client (SAP 9) — MANDATORY
    "IPClient",
    "QoSMode",
    # F.8 RCOP (SAP 6)
    "APP_ID_BFTP",
    "APP_ID_FRAP",
    "APP_ID_FRAPV2",
    "APP_ID_TMMHS_TMI1",
    "APP_ID_TMMHS_TMI2",
    "APP_ID_TMMHS_TMI3",
    "APP_ID_TMMHS_TMI4",
    "APP_ID_TMMHS_TMI5",
    "RCOP_HEADER_SIZE",
    "RcopPDU",
    "encode_rcop_pdu",
    "decode_rcop_pdu",
    "RcopClient",
    # F.9 UDOP (SAP 7)
    "UdopClient",
    # F.10 Extended Clients: BFTP, FRAP, FRAPv2
    "BftpClient",
    "FrapClient",
    "FrapV2Client",
    # F.14 CFTP (SAP 12)
    "CftpClient",
    "CftpMessage",
    # F.11 ETHER Client (SAP 8)
    "EtherFrame",
    "EtherClient",
    "encode_ec_frame",
    "decode_ec_frame",
    "stanag_addr_to_pseudo_ether",
    "pseudo_ether_to_stanag_addr",
    "ETHERTYPE_IPV4",
    "ETHERTYPE_IPV6",
    "ETHERTYPE_ARP",
    "ETHERTYPE_PPP",
    "ETHERTYPE_VJCOMP",
    "ETHERTYPE_ROHC",
    # F.3 COSS (SAP 1)
    "CossClient",
    "CossMode",
    "CharacterEncoder",
    # F.2 Subnet Management (SAP 0)
    "SubnetMgmtClient",
]
