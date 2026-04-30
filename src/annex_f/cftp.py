"""
F.14 — CFTP (Compressed File Transfer Protocol, SAP 12).

CFTP usa:
  - RCOPv1 (formato original da Edição 1 — 4 bytes de cabeçalho, SEM APPLICATION_IDENTIFIER)
  - BFTPv1 (com bytes de sincronização 0x10 0x02 no início)
  - Compressão gzip (RFC 1950, 1951, 1952)

RCOPv1 PDU (F.14.3, Figura F-23):
  Byte 0: CONNECTION_ID_NUMBER[7:4] | RESERVED[3:0]
  Byte 1: U_PDU_ID_NUMBER (8 bits)
  Bytes 2-3: U_PDU_SEGMENT_NUMBER (16 bits big-endian)
  Bytes 4+: DATA (BFTPv1 PDU)

BFTPv1 PDU (F.14.3.1.1, Figura F-25):
  Bytes 0-1: SYNC = 0x10 0x02 (DLE STX)
  Byte 2:    SIZE_OF_FILENAME = n
  Bytes 3..2+n: FILENAME
  Bytes 3+n..6+n: SIZE_OF_FILE (4 bytes big-endian)
  Bytes 7+n..: FILE_DATA (compressed CFTP message)

Formato do arquivo comprimido (F.14.5, Tabela F-10):
  <MessageID>\\n
  <RecipientList>\\n   (e-mails separados por ",")
  <MessageSize>\\n
  <Message SMTP raw>

Message ACK (F.14.3.2, Figura F-26):
  RCOPv1 body = 0x10 0x0B
"""

from __future__ import annotations

import gzip
import logging
import struct
from dataclasses import dataclass
from typing import Callable

from src.stypes import DeliveryMode, SAP_ID_CFTP, SisUnidataIndication

from .base_client import SubnetClient
from .updu import DEFAULT_MTU

logger = logging.getLogger(__name__)

# ─── Constantes ───────────────────────────────────────────────────────────────

BFTPV1_SYNC = b"\x10\x02"          # DLE STX — sincronização BFTPv1
CFTP_MSG_ACK = b"\x10\x0B"         # ACK de mensagem CFTP (F.14.3.2)
RCOP_V1_HEADER_SIZE = 4             # sem APP_ID
RCOP_V1_MAX_DATA = DEFAULT_MTU - RCOP_V1_HEADER_SIZE  # 2044 bytes

# ─── RCOPv1 PDU (sem APP_ID) ──────────────────────────────────────────────────


@dataclass(slots=True)
class RcopV1PDU:
    """RCOPv1 PDU — formato original da Edição 1 (F.14.3, Figura F-23).

    Idêntico ao RCOP PDU mas SEM o campo APPLICATION_IDENTIFIER.
    """

    connection_id: int   # 0-15 (4 bits)
    updu_id: int         # 0-255 (8 bits)
    segment_number: int  # 0-65535 (16 bits)
    data: bytes          # DATA[] (comprimento variável)


def _encode_rcopv1(pdu: RcopV1PDU) -> bytes:
    byte0 = (pdu.connection_id & 0x0F) << 4
    return struct.pack(">BBH", byte0, pdu.updu_id, pdu.segment_number) + pdu.data


def _decode_rcopv1(raw: bytes) -> RcopV1PDU:
    if len(raw) < RCOP_V1_HEADER_SIZE:
        raise ValueError(f"RCOPv1 PDU truncado: {len(raw)} bytes")
    byte0, updu_id, segment_number = struct.unpack_from(">BBH", raw)
    connection_id = (byte0 >> 4) & 0x0F
    return RcopV1PDU(connection_id, updu_id, segment_number, raw[RCOP_V1_HEADER_SIZE:])


# ─── BFTPv1 PDU ───────────────────────────────────────────────────────────────


def _encode_bftpv1(filename: str | bytes, file_data: bytes) -> bytes:
    """Monta BFTPv1 PDU: SYNC + SIZE_OF_FILENAME + FILENAME + SIZE_OF_FILE + FILE_DATA."""
    if isinstance(filename, str):
        filename = filename.encode("ascii", errors="replace")
    n = len(filename)
    if n > 255:
        raise ValueError(f"Nome de arquivo BFTPv1 muito longo: {n} bytes")
    return (
        BFTPV1_SYNC
        + struct.pack(">B", n)
        + filename
        + struct.pack(">I", len(file_data))
        + file_data
    )


def _decode_bftpv1(raw: bytes) -> tuple[str, bytes]:
    """Decodifica BFTPv1 PDU. Retorna (filename, file_data)."""
    if len(raw) < 2 or raw[:2] != BFTPV1_SYNC:
        raise ValueError("BFTPv1: bytes de sincronização ausentes ou incorretos")
    if len(raw) < 7:  # 2 sync + 1 size + 0 name + 4 file_size
        raise ValueError(f"BFTPv1 PDU truncado: {len(raw)} bytes")
    n = raw[2]
    if len(raw) < 3 + n + 4:
        raise ValueError(f"BFTPv1: cabeçalho truncado (n={n}, raw={len(raw)})")
    filename = raw[3 : 3 + n].decode("ascii", errors="replace")
    (file_size,) = struct.unpack_from(">I", raw, 3 + n)
    data_start = 3 + n + 4
    file_data = raw[data_start : data_start + file_size]
    if len(file_data) < file_size:
        raise ValueError(f"BFTPv1: dados truncados (esperado {file_size}, recebeu {len(file_data)})")
    return filename, file_data


# ─── CFTP Message (arquivo comprimido) ────────────────────────────────────────


@dataclass
class CftpMessage:
    """Mensagem CFTP decomprimida (F.14.5, Tabela F-10)."""

    message_id: str        # raiz do nome de arquivo; máx 255 chars alfanuméricos
    recipients: list[str]  # lista de e-mails; primeiro é Return-Path
    message: bytes         # payload SMTP raw (incluindo <CRLF>.<CRLF> final)


def _encode_cftp_message(msg: CftpMessage) -> bytes:
    """Monta arquivo CFTP antes da compressão."""
    recipient_list = ",".join(msg.recipients)
    header = (
        msg.message_id + "\n"
        + recipient_list + "\n"
        + str(len(msg.message)) + "\n"
    ).encode("ascii")
    return header + msg.message


def _decode_cftp_message(raw: bytes) -> CftpMessage:
    """Decomprime e parseia mensagem CFTP."""
    # Três primeiras linhas são MessageID, RecipientList, MessageSize
    lines = raw.split(b"\n", 3)
    if len(lines) < 4:
        raise ValueError(f"CFTP: formato inválido, esperava 3 linhas de cabeçalho")
    message_id = lines[0].decode("ascii").strip()
    recipients = [r.strip() for r in lines[1].decode("ascii").split(",")]
    try:
        message_size = int(lines[2].decode("ascii").strip())
    except ValueError as exc:
        raise ValueError(f"CFTP: MessageSize inválido: {exc}") from exc
    body = lines[3]
    if len(body) > message_size:
        logger.warning(
            "CFTP: MessageSize=%d mas body tem %d bytes; descartando %d bytes "
            "extras (possivelmente padding da compressão).",
            message_size, len(body), len(body) - message_size,
        )
    elif len(body) < message_size:
        logger.warning(
            "CFTP: MessageSize=%d declarado mas body tem apenas %d bytes "
            "(mensagem truncada).",
            message_size, len(body),
        )
    message = body[:message_size]
    return CftpMessage(message_id, recipients, message)


# ─── Remontagem RCOPv1 ────────────────────────────────────────────────────────


class _RcopV1ReassemblyContext:
    """Remonta segmentos RCOPv1 por (src_addr, conn_id, updu_id)."""

    def __init__(self):
        self._buffers: dict[tuple, dict[int, bytes]] = {}

    def feed(self, src_addr: int, pdu: RcopV1PDU) -> bytes | None:
        key = (src_addr, pdu.connection_id, pdu.updu_id)

        if pdu.segment_number == 0 and len(pdu.data) < RCOP_V1_MAX_DATA:
            self._buffers.pop(key, None)
            return pdu.data

        buf = self._buffers.setdefault(key, {})
        buf[pdu.segment_number] = pdu.data

        if len(pdu.data) >= RCOP_V1_MAX_DATA:
            return None

        max_seg = max(buf)
        if len(buf) != max_seg + 1:
            return None

        reassembled = b"".join(buf[i] for i in range(max_seg + 1))
        del self._buffers[key]
        return reassembled

    def clear(self):
        self._buffers.clear()


# ─── CftpClient ───────────────────────────────────────────────────────────────


class CftpClient(SubnetClient):
    """F.14 — CFTP Client, SAP 12.

    Envio:
        client.send_mail(dest_addr, message_id, recipients, smtp_message_bytes)

    Recepção:
        client.on_mail_received = callback(src_addr, msg: CftpMessage)

    ACK automático após recepção (stop-and-wait por padrão conforme F.14.3.1).
    """

    SAP_ID = SAP_ID_CFTP  # 12

    def __init__(self, node, connection_id: int = 0, auto_ack: bool = True):
        super().__init__(node, connection_id)
        self._updu_id: int = 0
        self._reassembly = _RcopV1ReassemblyContext()
        self.auto_ack = auto_ack
        self.on_mail_received: Callable[[int, CftpMessage], None] | None = None
        self.on_ack_received: Callable[[int], None] | None = None

    # ── Envio ─────────────────────────────────────────────────────────────────

    def _alloc_id(self) -> int:
        uid = self._updu_id
        self._updu_id = (self._updu_id + 1) & 0xFF
        return uid

    def send_mail(
        self,
        dest_addr: int,
        message_id: str,
        recipients: list[str],
        smtp_message: bytes,
        priority: int = 5,
        ttl_seconds: float = 300.0,
    ) -> int:
        """Comprime e envia mensagem SMTP via CFTP. Retorna updu_id alocado."""
        msg = CftpMessage(message_id, recipients, smtp_message)
        raw_cftp = _encode_cftp_message(msg)
        compressed = gzip.compress(raw_cftp)

        # Filename = message_id (convenção do repositório)
        bftpv1_pdu = _encode_bftpv1(message_id, compressed)

        updu_id = self._alloc_id()
        segments = self._segment_rcopv1(updu_id, bftpv1_pdu)

        for seg_bytes in segments:
            self.node.unidata_request(
                sap_id=self.SAP_ID,
                dest_addr=dest_addr,
                dest_sap=self.SAP_ID,
                priority=priority,
                ttl_seconds=ttl_seconds,
                mode=DeliveryMode(arq_mode=True),
                updu=seg_bytes,
            )

        logger.info(
            "CFTP → addr=%d message_id=%r %d bytes comprimidos (updu_id=%d)",
            dest_addr, message_id, len(compressed), updu_id,
        )
        return updu_id

    def _segment_rcopv1(self, updu_id: int, data: bytes) -> list[bytes]:
        """Segmenta dados em PDUs RCOPv1."""
        if not data:
            return [_encode_rcopv1(RcopV1PDU(self.connection_id, updu_id, 0, b""))]
        segs = []
        seg_num = 0
        for offset in range(0, len(data), RCOP_V1_MAX_DATA):
            chunk = data[offset : offset + RCOP_V1_MAX_DATA]
            segs.append(_encode_rcopv1(
                RcopV1PDU(self.connection_id, updu_id, seg_num, chunk)
            ))
            seg_num += 1
        return segs

    # ── Recepção ──────────────────────────────────────────────────────────────

    def on_unidata_indication(self, indication: SisUnidataIndication):
        """Override: parseia RCOPv1 PDU diretamente."""
        try:
            pdu = _decode_rcopv1(indication.updu)
        except ValueError as exc:
            logger.warning("CFTP PDU inválido de addr=%d: %s", indication.src_addr, exc)
            return

        # ACK recebido (CFTP_MSG_ACK = 0x10 0x0B)
        if pdu.data == CFTP_MSG_ACK:
            logger.info("CFTP ACK ← addr=%d", indication.src_addr)
            if self.on_ack_received:
                self.on_ack_received(indication.src_addr)
            return

        raw_data = self._reassembly.feed(indication.src_addr, pdu)
        if raw_data is None:
            return  # aguardando mais segmentos

        self._process_received(indication.src_addr, indication.src_sap, pdu, raw_data)

    def _process_received(
        self, src_addr: int, src_sap: int, pdu: RcopV1PDU, bftpv1_data: bytes
    ):
        """Decodifica BFTPv1, descomprime CFTP e entrega ao callback."""
        try:
            _, compressed = _decode_bftpv1(bftpv1_data)
        except ValueError as exc:
            logger.warning("CFTP ← addr=%d: BFTPv1 inválido: %s", src_addr, exc)
            return

        try:
            raw_cftp = gzip.decompress(compressed)
        except Exception as exc:
            logger.warning("CFTP ← addr=%d: falha ao descomprimir: %s", src_addr, exc)
            return

        try:
            msg = _decode_cftp_message(raw_cftp)
        except ValueError as exc:
            logger.warning("CFTP ← addr=%d: mensagem malformada: %s", src_addr, exc)
            return

        logger.info(
            "CFTP ← addr=%d message_id=%r destinatários=%s",
            src_addr, msg.message_id, msg.recipients,
        )

        if self.on_mail_received:
            self.on_mail_received(src_addr, msg)

        # ACK automático stop-and-wait (F.14.3.1)
        if self.auto_ack:
            self._send_ack(src_addr, pdu.connection_id, pdu.updu_id)

    def _send_ack(self, dest_addr: int, conn_id: int, updu_id: int):
        """Envia Message ACK (0x10 0x0B) via RCOPv1."""
        ack_pdu = _encode_rcopv1(RcopV1PDU(conn_id, updu_id, 0, CFTP_MSG_ACK))
        self.node.unidata_request(
            sap_id=self.SAP_ID,
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            priority=5,
            ttl_seconds=60.0,
            mode=DeliveryMode(arq_mode=True),
            updu=ack_pdu,
        )
        logger.debug("CFTP ACK → addr=%d", dest_addr)
