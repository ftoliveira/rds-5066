"""
F.10.2 — Extended Clients: BFTP, FRAP e FRAPv2 (via RCOP, SAP 6).

BFTP PDU (F.10.2.2.1, Figura F-12):
  [RCOP header 6 bytes, APP_ID=0x1002]
  Byte  0:   SIZE_OF_FILENAME = n (1 byte)
  Bytes 1..n:  FILENAME (n bytes)
  Bytes n+1..n+4: SIZE_OF_FILE = p (4 bytes big-endian)
  Bytes n+5..:  FILE_DATA (p bytes)

FRAP (F.10.2.3): envia RCOP PDU com APP_ID=0x100B, body nulo.
  O CONNECTION_ID e U_PDU_ID devem coincidir com os do arquivo reconhecido.

FRAPv2 (F.10.2.4): envia RCOP PDU com APP_ID=0x100C,
  body = BFTP Header (SIZE_OF_FILENAME + FILENAME + SIZE_OF_FILE) do arquivo reconhecido.

Todos os clientes usam RcopClient como base (SAP 6, ARQ).
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Callable

from .rcop import APP_ID_BFTP, APP_ID_FRAP, APP_ID_FRAPV2, RcopClient

logger = logging.getLogger(__name__)


# ─── Codec BFTP PDU ───────────────────────────────────────────────────────────


def _build_bftp_header(filename: str | bytes, file_size: int) -> bytes:
    """Monta apenas o cabeçalho BFTP (SIZE_OF_FILENAME + FILENAME + SIZE_OF_FILE)."""
    if isinstance(filename, str):
        filename = filename.encode("ascii", errors="replace")
    n = len(filename)
    if n > 255:
        raise ValueError(f"Nome de arquivo muito longo: {n} bytes (máximo 255)")
    return struct.pack(">B", n) + filename + struct.pack(">I", file_size)


def _encode_bftp(filename: str | bytes, file_data: bytes) -> bytes:
    """Codifica BFTP PDU completo (cabeçalho + dados)."""
    header = _build_bftp_header(filename, len(file_data))
    return header + file_data


def _decode_bftp(raw: bytes) -> tuple[str, bytes]:
    """Decodifica BFTP PDU. Retorna (filename, file_data)."""
    if len(raw) < 5:  # 1 (size) + 0 (filename) + 4 (size_of_file) mínimo
        raise ValueError(f"BFTP PDU truncado: {len(raw)} bytes")
    n = raw[0]
    if len(raw) < 1 + n + 4:
        raise ValueError(f"BFTP PDU truncado: nome={n} bytes mas raw={len(raw)} bytes")
    filename = raw[1 : 1 + n].decode("ascii", errors="replace")
    (file_size,) = struct.unpack_from(">I", raw, 1 + n)
    data_start = 1 + n + 4
    file_data = raw[data_start : data_start + file_size]
    if len(file_data) < file_size:
        raise ValueError(
            f"BFTP: dados truncados, esperado {file_size} bytes, recebeu {len(file_data)}"
        )
    return filename, file_data


# ─── BftpClient ───────────────────────────────────────────────────────────────


class BftpClient(RcopClient):
    """F.10.2.2 — Basic File Transfer Protocol Client (RCOP, APP_ID=0x1002).

    Envio:
        client.send_file(dest_addr, "arquivo.bin", data)

    Recepção:
        client.on_file_received = callback(src_addr, src_sap, filename, file_data)

    ACK via FRAP/FRAPv2: use FrapClient ou FrapV2Client separado.
    """

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_file_received: Callable[[int, int, str, bytes], None] | None = None
        self.register_app_handler(APP_ID_BFTP, self._on_bftp_data)

    def send_file(
        self,
        dest_addr: int,
        filename: str | bytes,
        file_data: bytes,
        conn_id: int | None = None,
        priority: int = 5,
        ttl_seconds: float = 300.0,
    ) -> int:
        """Envia arquivo via BFTP. Retorna updu_id alocado."""
        bftp_pdu = _encode_bftp(filename, file_data)
        updu_id = self.send(
            dest_addr=dest_addr,
            app_id=APP_ID_BFTP,
            data=bftp_pdu,
            conn_id=conn_id,
            priority=priority,
            ttl_seconds=ttl_seconds,
        )
        fn = filename if isinstance(filename, str) else filename.decode("ascii", errors="replace")
        logger.info(
            "BFTP → addr=%d arquivo=%r %d bytes (updu_id=%d)",
            dest_addr, fn, len(file_data), updu_id,
        )
        return updu_id

    def _on_bftp_data(self, src_addr: int, src_sap: int, app_id: int, data: bytes):
        try:
            filename, file_data = _decode_bftp(data)
        except ValueError as exc:
            logger.warning("BFTP ← addr=%d: PDU inválido: %s", src_addr, exc)
            return
        logger.info(
            "BFTP ← addr=%d arquivo=%r %d bytes", src_addr, filename, len(file_data)
        )
        if self.on_file_received:
            self.on_file_received(src_addr, src_sap, filename, file_data)


# ─── FrapClient ───────────────────────────────────────────────────────────────


class FrapClient(RcopClient):
    """F.10.2.3 — File-Receipt Acknowledgement Protocol (APP_ID=0x100B).

    Reconhecimento de arquivos recebidos via BFTP.
    FRAP PDU = RCOP header com APP_ID=0x100B e body nulo.
    O conn_id e updu_id devem coincidir com os do arquivo reconhecido.

    Uso:
        frap.ack(dest_addr, conn_id=conn_id_recebido, updu_id=updu_id_recebido)
    """

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_ack_received: Callable[[int, int, int], None] | None = None
        # conn_id, updu_id do arquivo reconhecido
        self.register_app_handler(APP_ID_FRAP, self._on_frap)

    def ack(
        self,
        dest_addr: int,
        conn_id: int,
        updu_id: int,
        priority: int = 5,
        ttl_seconds: float = 60.0,
    ):
        """Envia FRAP ACK para arquivo recebido com (conn_id, updu_id)."""
        # Força conn_id e updu_id específicos para coincidir com o arquivo
        old_conn = self.connection_id
        old_uid = self._rcop_updu_id
        self.connection_id = conn_id
        self._rcop_updu_id = updu_id
        self.send(
            dest_addr=dest_addr,
            app_id=APP_ID_FRAP,
            data=b"",  # body nulo conforme F.10.2.3
            conn_id=conn_id,
            priority=priority,
            ttl_seconds=ttl_seconds,
        )
        # Restaura estado
        self.connection_id = old_conn
        self._rcop_updu_id = old_uid
        logger.debug(
            "FRAP ACK → addr=%d conn_id=%d updu_id=%d",
            dest_addr, conn_id, updu_id,
        )

    def _on_frap(self, src_addr: int, src_sap: int, app_id: int, data: bytes):
        logger.info("FRAP ACK ← addr=%d", src_addr)
        if self.on_ack_received:
            self.on_ack_received(src_addr, src_sap, app_id)


# ─── FrapV2Client ─────────────────────────────────────────────────────────────


class FrapV2Client(RcopClient):
    """F.10.2.4 — File-Receipt Acknowledgement Protocol v2 (APP_ID=0x100C).

    Evita ambiguidade do FRAP original incluindo o cabeçalho BFTP do arquivo
    reconhecido no body do PDU.

    Uso:
        frapv2.ack(dest_addr, filename, file_size, conn_id=..., updu_id=...)
    """

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_ack_received: Callable[[int, int, str, int], None] | None = None
        # (src_addr, src_sap, filename, file_size)
        self.register_app_handler(APP_ID_FRAPV2, self._on_frapv2)

    def ack(
        self,
        dest_addr: int,
        filename: str | bytes,
        file_size: int,
        conn_id: int,
        updu_id: int,
        priority: int = 5,
        ttl_seconds: float = 60.0,
    ):
        """Envia FRAPv2 ACK com BFTP Header do arquivo reconhecido."""
        body = _build_bftp_header(filename, file_size)
        old_conn = self.connection_id
        old_uid = self._rcop_updu_id
        self.connection_id = conn_id
        self._rcop_updu_id = updu_id
        self.send(
            dest_addr=dest_addr,
            app_id=APP_ID_FRAPV2,
            data=body,
            conn_id=conn_id,
            priority=priority,
            ttl_seconds=ttl_seconds,
        )
        self.connection_id = old_conn
        self._rcop_updu_id = old_uid
        fn = filename if isinstance(filename, str) else filename.decode("ascii", errors="replace")
        logger.debug("FRAPv2 ACK → addr=%d arquivo=%r", dest_addr, fn)

    def _on_frapv2(self, src_addr: int, src_sap: int, app_id: int, data: bytes):
        # FRAPv2 body = BFTP Header only (SIZE_OF_FILENAME + FILENAME + SIZE_OF_FILE),
        # without FILE_DATA (F.10.2.4).
        try:
            n = data[0]
            filename = data[1 : 1 + n].decode("ascii", errors="replace")
            (file_size,) = struct.unpack_from(">I", data, 1 + n)
        except Exception as exc:
            logger.warning("FRAPv2 ← addr=%d: body inválido: %s", src_addr, exc)
            return
        logger.info("FRAPv2 ACK ← addr=%d arquivo=%r size=%d", src_addr, filename, file_size)
        if self.on_ack_received:
            self.on_ack_received(src_addr, src_sap, filename, file_size)
