"""
F.5 — HMTP (HF Mail Transfer Protocol) — SAP 3.

Adaptação do SMTP para HF. Otimização: batch de múltiplas mensagens
em uma única transmissão (MAIL MULTIPLE).

Comandos: HELO, MAIL MULTIPLE, MAIL FROM, RCPT TO, DATA, QUIT
Respostas: 250 (OK), 550 (not known)
Terminação: <CRLF>.<CRLF> por mensagem, <CRLF>.<CRLF><CRLF>.<CRLF> para sequência.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from src.stypes import DeliveryMode

from .base_client import SubnetClient
from .text_protocol import (
    CommandResponse,
    encode_multiline_data,
    format_response,
    parse_command,
)

logger = logging.getLogger(__name__)


@dataclass
class MailMessage:
    """Mensagem de correio HMTP."""
    sender: str          # user@host
    recipients: list[str]  # [user@host, ...]
    body: str


class HMTPClient(SubnetClient):
    """Lado cliente HMTP — Anexo F.5, SAP 3."""
    SAP_ID = 3

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_response: Callable[[list[CommandResponse]], None] | None = None

    def ehlo(self, dest_addr: int, hostname: str,
             priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Envia comando EHLO (F.5 — enforced command pipelining)."""
        data = f"EHLO {hostname}\r\n".encode("utf-8")
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def send_batch(self, dest_addr: int, hostname: str,
                   messages: list[MailMessage],
                   priority: int = 10, ttl_seconds: float = 300.0) -> None:
        """Envia EHLO + batch de mensagens HMTP em uma única transmissão (F.5)."""
        buf = f"EHLO {hostname}\r\n".encode("utf-8")

        if len(messages) > 1:
            buf += b"MAIL MULTIPLE\r\n"

        for msg in messages:
            buf += f"MAIL FROM:<{msg.sender}>\r\n".encode("utf-8")
            for rcpt in msg.recipients:
                buf += f"RCPT TO:<{rcpt}>\r\n".encode("utf-8")
            buf += b"DATA\r\n"
            buf += encode_multiline_data(msg.body)

        # Terminador de sequência para múltiplas: <CRLF>.<CRLF> adicional
        # (o último encode_multiline_data já terminou com <CRLF>.<CRLF>,
        #  resultando em <CRLF>.<CRLF><CRLF>.<CRLF> conforme spec)
        if len(messages) > 1:
            buf += b"\r\n.\r\n"

        buf += b"QUIT\r\n"

        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=buf,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def _on_data_received(self, src_addr: int, data: bytes):
        """Parse respostas do servidor HMTP."""
        text = data.decode("utf-8", errors="replace")
        responses = []
        for line in text.split("\r\n"):
            line = line.strip()
            if line:
                responses.append(parse_command(line))

        logger.debug("HMTP respostas de addr=%d: %d linhas", src_addr, len(responses))
        if self.on_response and responses:
            self.on_response(responses)


class _HMTPServerState(Enum):
    IDLE = auto()
    READY = auto()
    GOT_MAIL = auto()
    READING_BODY = auto()


class HMTPServer(SubnetClient):
    """Lado servidor HMTP — Anexo F.5, SAP 3."""
    SAP_ID = 3

    def __init__(self, node, connection_id: int = 0,
                 relay_handler: Callable[[MailMessage], bool] | None = None):
        """
        Args:
            relay_handler: Callable(MailMessage) -> bool.
                Se None, aceita todas as mensagens localmente.
        """
        super().__init__(node, connection_id)
        self._relay_handler = relay_handler
        self.on_mail_received: Callable[[MailMessage], None] | None = None
        self._hostname = ""

        self._state = _HMTPServerState.IDLE
        self._multiple = False
        self._sender = ""
        self._recipients: list[str] = []
        self._body_lines: list[str] = []
        self._responses: list[bytes] = []
        self._known_domains: set[str] | None = None

    def set_known_domains(self, domains: set[str]):
        """Define domínios conhecidos. Desconhecidos recebem 550."""
        self._known_domains = domains

    def _on_data_received(self, src_addr: int, data: bytes):
        """Parse e processa comandos HMTP do cliente."""
        self._responses.clear()
        self._process_data(data)

        if self._responses:
            response_data = b"".join(self._responses)
            self._send_data(
                dest_addr=src_addr,
                dest_sap=self.SAP_ID,
                data=response_data,
                priority=10,
                ttl_seconds=120.0,
                mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
            )

    def _process_data(self, data: bytes):
        """Processa dados recebidos."""
        text = data.decode("utf-8", errors="replace")

        if self._state == _HMTPServerState.READING_BODY:
            self._process_body(text)
            return

        lines = text.split("\r\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            cmd = parse_command(line)

            if cmd.keyword in ("EHLO", "HELO"):
                self._hostname = cmd.args
                self._state = _HMTPServerState.READY
                # F.5: EHLO response with capabilities (enforced pipelining)
                self._responses.append(
                    f"250-{cmd.args} Hello\r\n".encode("utf-8")
                )
                self._responses.append(
                    b"250-PIPELINING\r\n"
                )
                self._responses.append(
                    b"250 8BITMIME\r\n"
                )
                i += 1
                continue

            if cmd.keyword == "MAIL" and cmd.args.upper() == "MULTIPLE":
                self._multiple = True
                i += 1
                continue

            if cmd.keyword == "MAIL" and cmd.args.upper().startswith("FROM:"):
                self._reset_transaction()
                self._sender = self._extract_bracket(cmd.args[5:])
                self._responses.append(
                    format_response(250, f"Source {self._sender} OK")
                )
                self._state = _HMTPServerState.GOT_MAIL
                i += 1
                continue

            if cmd.keyword == "RCPT" and cmd.args.upper().startswith("TO:"):
                rcpt = self._extract_bracket(cmd.args[3:])
                if self._known_domains is not None:
                    domain = rcpt.split("@")[-1] if "@" in rcpt else ""
                    if domain not in self._known_domains:
                        self._responses.append(
                            format_response(550, f"Destination {rcpt} not known")
                        )
                        i += 1
                        continue
                self._recipients.append(rcpt)
                self._responses.append(
                    format_response(250, f"Destination {rcpt} OK")
                )
                i += 1
                continue

            if cmd.keyword == "DATA":
                self._responses.append(format_response(250, "DATA OK"))
                self._state = _HMTPServerState.READING_BODY
                remaining = "\r\n".join(lines[i + 1:])
                if remaining:
                    self._process_body(remaining)
                return

            if cmd.keyword == "QUIT":
                self._responses.append(format_response(250, "OK"))
                self._state = _HMTPServerState.IDLE
                i += 1
                continue

            logger.warning("HMTP servidor: comando desconhecido: %s", line)
            i += 1

    def _process_body(self, text: str):
        """Processa corpo de mensagem."""
        parts = text.split("\r\n")
        for i, part in enumerate(parts):
            if part == ".":
                body = "\r\n".join(self._body_lines)
                self._deliver_mail(body)
                self._state = _HMTPServerState.READY
                # Re-processar linhas restantes como comandos
                remaining = "\r\n".join(parts[i + 1:])
                if remaining.strip():
                    self._process_data(remaining.encode("utf-8"))
                return
            if part.startswith(".."):
                part = part[1:]
            self._body_lines.append(part)

    def _deliver_mail(self, body: str):
        """Entrega mensagem."""
        msg = MailMessage(
            sender=self._sender,
            recipients=list(self._recipients),
            body=body,
        )

        if self._relay_handler:
            self._relay_handler(msg)
        if self.on_mail_received:
            self.on_mail_received(msg)

        logger.info(
            "HMTP mail entregue: from=%s to=%s body_len=%d",
            msg.sender, msg.recipients, len(body),
        )
        self._sender = ""
        self._recipients = []
        self._body_lines = []

    def _reset_transaction(self):
        """Limpa buffers."""
        self._sender = ""
        self._recipients.clear()
        self._body_lines.clear()

    @staticmethod
    def _extract_bracket(s: str) -> str:
        s = s.strip()
        if s.startswith("<") and ">" in s:
            return s[1:s.index(">")]
        return s
