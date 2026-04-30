"""
Mensagem Reconhecida (SAP 13) — Protocolo de demonstração.

Protocolo SEND tipo SMTP simplificado (uso interno / testes).
SAP_ID 13: porta não atribuída pela norma ("UNASSIGNED – available for arbitrary use",
Tabela F-1, Anexo F, STANAG 5066 Ed.3).

Comandos: SEND FROM:<user>, RCPT TO:<user>, DATA, SEND MULTIPLE, RSET, QUIT
Respostas: 250 (OK), 450 (temp fail), 550 (not known)
Terminação: <CRLF>.<CRLF> para dados, <CRLF>.<CRLF><CRLF>.<CRLF> para sequência.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable

from src.stypes import DeliveryMode

from .base_client import SubnetClient
from .text_protocol import (
    CRLF,
    CommandResponse,
    TextProtocolParser,
    encode_multiline_data,
    format_response,
    parse_command,
)

logger = logging.getLogger(__name__)


@dataclass
class SendMessage:
    """Uma mensagem SEND completa."""
    from_user: str
    to_users: list[str]
    body: str


class AckMessageClient(SubnetClient):
    """Lado cliente — envia mensagens reconhecidas, SAP 13 (porta não atribuída)."""
    SAP_ID = 13

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_response: Callable[[int, str], None] | None = None
        self._parser = TextProtocolParser()

    def send_message(self, dest_addr: int, from_user: str,
                     to_users: list[str], body: str,
                     priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Envia uma mensagem SEND única."""
        self.send_multiple(dest_addr, [SendMessage(from_user, to_users, body)],
                           priority=priority, ttl_seconds=ttl_seconds)

    def send_multiple(self, dest_addr: int, messages: list[SendMessage],
                      priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Envia uma ou mais mensagens SEND em batch."""
        buf = b""

        if len(messages) > 1:
            buf += b"SEND MULTIPLE\r\n"

        for msg in messages:
            buf += f"SEND FROM:<{msg.from_user}>\r\n".encode("utf-8")
            for rcpt in msg.to_users:
                buf += f"RCPT TO:<{rcpt}>\r\n".encode("utf-8")
            buf += b"DATA\r\n"
            buf += encode_multiline_data(msg.body)

        # Terminação de sequência
        if len(messages) > 1:
            buf += b"\r\n.\r\n"  # Segundo terminador para sequência

        mode = DeliveryMode(arq_mode=True)
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=buf,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=mode,
        )

    def send_quit(self, dest_addr: int, priority: int = 10,
                  ttl_seconds: float = 30.0) -> None:
        """Envia comando QUIT."""
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=b"QUIT\r\n",
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True),
        )

    def send_reset(self, dest_addr: int, priority: int = 10,
                   ttl_seconds: float = 30.0) -> None:
        """Envia comando RSET."""
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=b"RSET\r\n",
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True),
        )

    def _on_data_received(self, src_addr: int, data: bytes):
        """Parse respostas do servidor."""
        lines = self._parser.feed(data)
        for line in lines:
            resp = parse_command(line)
            logger.debug("F.15 resposta de addr=%d: %d %s", src_addr, resp.code, line)
            if self.on_response and resp.code > 0:
                self.on_response(resp.code, line)


class _ServerState(Enum):
    IDLE = auto()
    GOT_SEND = auto()
    GOT_DATA = auto()
    READING_BODY = auto()


class AckMessageServer(SubnetClient):
    """Lado servidor — recebe e processa mensagens SEND, SAP 13 (porta não atribuída)."""
    SAP_ID = 13

    def __init__(self, node, connection_id: int = 0,
                 mailbox_handler: Callable[[str, list[str], str], bool] | None = None):
        """
        Args:
            mailbox_handler: Callable(from_user, to_users, body) -> bool.
                Retorna True se entrega bem-sucedida. Se None, aceita tudo.
        """
        super().__init__(node, connection_id)
        self._mailbox_handler = mailbox_handler
        self.on_message_received: Callable[[str, list[str], str], None] | None = None

        self._state = _ServerState.IDLE
        self._multiple = False
        self._from_user = ""
        self._to_users: list[str] = []
        self._body_lines: list[str] = []
        self._responses: list[bytes] = []
        self._known_users: set[str] | None = None  # None = aceita todos

    def set_known_users(self, users: set[str]):
        """Define usuários conhecidos. Desconhecidos recebem 550."""
        self._known_users = users

    def _on_data_received(self, src_addr: int, data: bytes):
        """Parse e processa comandos SEND do cliente."""
        self._responses.clear()
        self._process_data(data)

        # Envia respostas acumuladas
        if self._responses:
            response_data = b"".join(self._responses)
            self._send_data(
                dest_addr=src_addr,
                dest_sap=self.SAP_ID,
                data=response_data,
                priority=10,
                ttl_seconds=120.0,
                mode=DeliveryMode(arq_mode=True),
            )

    def _process_data(self, data: bytes):
        """Processa dados recebidos linha por linha."""
        # Divide em linhas
        text = data.decode("utf-8", errors="replace")
        # Processa estado de leitura de corpo primeiro
        if self._state == _ServerState.READING_BODY:
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

            if cmd.keyword == "SEND" and cmd.args.upper() == "MULTIPLE":
                self._multiple = True
                self._reset_transaction()
                i += 1
                continue

            if cmd.keyword == "SEND" and cmd.args.upper().startswith("FROM:"):
                self._reset_transaction()
                self._from_user = self._extract_bracket(cmd.args[5:])
                self._responses.append(
                    format_response(250, f"Source {self._from_user} OK")
                )
                self._state = _ServerState.GOT_SEND
                i += 1
                continue

            if cmd.keyword == "RCPT" and cmd.args.upper().startswith("TO:"):
                user = self._extract_bracket(cmd.args[3:])
                if self._known_users is not None and user not in self._known_users:
                    self._responses.append(
                        format_response(550, f"Destination {user} not known")
                    )
                else:
                    self._to_users.append(user)
                    self._responses.append(
                        format_response(250, f"Destination {user} OK")
                    )
                i += 1
                continue

            if cmd.keyword == "DATA":
                self._responses.append(format_response(250, "DATA OK"))
                self._state = _ServerState.READING_BODY
                # O restante das linhas é corpo
                remaining = "\r\n".join(lines[i + 1:])
                if remaining:
                    self._process_body(remaining)
                return

            if cmd.keyword == "RSET":
                self._reset_transaction()
                self._responses.append(format_response(250, "OK"))
                i += 1
                continue

            if cmd.keyword == "QUIT":
                self._reset_transaction()
                self._responses.append(format_response(250, "OK"))
                i += 1
                continue

            # Comando desconhecido
            logger.warning("F.15 servidor: comando desconhecido: %s", line)
            i += 1

    def _process_body(self, text: str):
        """Processa corpo de mensagem, detectando terminadores."""
        # Procura terminador <CRLF>.<CRLF>
        parts = text.split("\r\n")
        for i, part in enumerate(parts):
            if part == ".":
                # Fim do corpo
                body = "\r\n".join(self._body_lines)
                self._deliver_message(body)
                self._state = _ServerState.IDLE
                # Re-processar linhas restantes como comandos
                remaining = "\r\n".join(parts[i + 1:])
                if remaining.strip():
                    self._process_data(remaining.encode("utf-8"))
                return
            # Byte-unstuffing
            if part.startswith(".."):
                part = part[1:]
            self._body_lines.append(part)

    def _deliver_message(self, body: str):
        """Entrega mensagem ao handler."""
        if self._mailbox_handler:
            self._mailbox_handler(self._from_user, self._to_users, body)
        if self.on_message_received:
            self.on_message_received(self._from_user, list(self._to_users), body)
        logger.info(
            "F.15 mensagem entregue: from=%s to=%s body_len=%d",
            self._from_user, self._to_users, len(body),
        )
        self._from_user = ""
        self._to_users = []
        self._body_lines = []

    def _reset_transaction(self):
        """Limpa buffers da transação atual."""
        self._from_user = ""
        self._to_users.clear()
        self._body_lines.clear()
        self._state = _ServerState.IDLE

    @staticmethod
    def _extract_bracket(s: str) -> str:
        """Extrai conteúdo entre < e >."""
        s = s.strip()
        if s.startswith("<") and ">" in s:
            return s[1:s.index(">")]
        return s
