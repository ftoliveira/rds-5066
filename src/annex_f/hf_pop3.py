"""
F.6 — HF-POP3 (SAP 4).

Adaptação do POP3 (RFC 1939) para HF.
Estados: AUTHORIZATION → TRANSACTION → UPDATE.
Autenticação via APOP (MD5).
Otimização: LIST embutido na resposta APOP.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from src.stypes import DeliveryMode

from .base_client import SubnetClient
from .text_protocol import (
    byte_stuff,
    format_pop3_err,
    format_pop3_ok,
    parse_command,
)

logger = logging.getLogger(__name__)


class POP3State(Enum):
    AUTHORIZATION = auto()
    TRANSACTION = auto()
    UPDATE = auto()


@dataclass
class StoredMessage:
    """Mensagem armazenada no maildrop."""
    body: str
    size: int = 0
    deleted: bool = False

    def __post_init__(self):
        if self.size == 0:
            self.size = len(self.body.encode("utf-8", errors="replace"))


class HFPOP3Client(SubnetClient):
    """Lado cliente HF-POP3 — Anexo F.6, SAP 4."""
    SAP_ID = 4

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_authenticated: Callable[[list[tuple[int, int]]], None] | None = None
        self.on_message_retrieved: Callable[[int, str], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self._server_timestamp = ""

    def connect(self, dest_addr: int,
                priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Inicia sessão POP3 — solicita saudação com timestamp do servidor.

        Per RFC 1939 / F.6: o servidor envia saudação com timestamp ao
        aceitar a conexão. Em STANAG 5066, isso é acionado enviando NOOP
        após o estabelecimento do enlace ARQ.
        """
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=b"NOOP\r\n",
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def apop(self, dest_addr: int, name: str, shared_secret: str,
             timestamp: str = "",
             priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Autenticação APOP com digest MD5.

        Args:
            timestamp: Timestamp do servidor (da saudação). Se vazio, usa o
                       último recebido via _server_timestamp.
        """
        ts = timestamp or self._server_timestamp
        digest_input = f"{ts}{shared_secret}"
        digest = hashlib.md5(digest_input.encode("utf-8")).hexdigest()
        data = f"APOP {name} {digest}\r\n".encode("utf-8")
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def list_messages(self, dest_addr: int, msg_number: int | None = None,
                      priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Solicita listagem de mensagens."""
        if msg_number is not None:
            data = f"LIST {msg_number}\r\n".encode("utf-8")
        else:
            data = b"LIST\r\n"
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def retrieve(self, dest_addr: int, msg_number: int | None = None,
                 priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Solicita recuperação de mensagem(s)."""
        if msg_number is not None:
            data = f"RETR {msg_number}\r\n".encode("utf-8")
        else:
            data = b"RETR\r\n"
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def delete(self, dest_addr: int, msg_number: int,
               priority: int = 10, ttl_seconds: float = 120.0) -> None:
        """Marca mensagem para deleção."""
        data = f"DELE {msg_number}\r\n".encode("utf-8")
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def quit(self, dest_addr: int,
             priority: int = 10, ttl_seconds: float = 30.0) -> None:
        """Encerra sessão POP3 (estado UPDATE)."""
        data = b"QUIT\r\n"
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def _on_data_received(self, src_addr: int, data: bytes):
        """Parse respostas do servidor POP3."""
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\r\n")

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Extrai timestamp da saudação
            if "<" in line and ">" in line:
                start = line.index("<")
                end = line.index(">") + 1
                self._server_timestamp = line[start:end]

            cmd = parse_command(line)

            if cmd.keyword == "+OK":
                # Verifica se é resposta APOP com scan listing
                if "maildrop" in cmd.args.lower() or "message" in cmd.args.lower():
                    # Parse scan listing das linhas seguintes
                    scan_listing = []
                    for scan_line in lines[lines.index(line.strip()) + 1:] if line.strip() in lines else []:
                        parts = scan_line.strip().split()
                        if len(parts) == 2 and parts[0].isdigit():
                            scan_listing.append((int(parts[0]), int(parts[1])))
                    if self.on_authenticated:
                        self.on_authenticated(scan_listing)

                # Verifica se é resposta RETR
                if "message" in cmd.args.lower() and "follow" in cmd.args.lower():
                    # Corpo está nas linhas seguintes até "."
                    body_lines = []
                    capturing = False
                    for body_line in lines:
                        if capturing:
                            if body_line.strip() == ".":
                                break
                            if body_line.startswith(".."):
                                body_line = body_line[1:]
                            body_lines.append(body_line)
                        elif body_line == line:
                            capturing = True

                    if body_lines and self.on_message_retrieved:
                        # Extrai msg_number do "+OK message N follows"
                        parts = cmd.args.split()
                        msg_num = 0
                        for p in parts:
                            if p.isdigit():
                                msg_num = int(p)
                                break
                        self.on_message_retrieved(msg_num, "\r\n".join(body_lines))

            elif cmd.keyword == "-ERR":
                logger.warning("HF-POP3 erro de addr=%d: %s", src_addr, cmd.args)
                if self.on_error:
                    self.on_error(cmd.args)


class HFPOP3Server(SubnetClient):
    """Lado servidor HF-POP3 — Anexo F.6, SAP 4."""
    SAP_ID = 4

    def __init__(self, node, connection_id: int = 0,
                 maildrop: dict[str, list[StoredMessage]] | None = None,
                 shared_secrets: dict[str, str] | None = None):
        """
        Args:
            maildrop: {username: [StoredMessage, ...]}
            shared_secrets: {username: secret_string}
        """
        super().__init__(node, connection_id)
        self._maildrop = maildrop or {}
        self._shared_secrets = shared_secrets or {}

        # Estado por sessão (simplificado: uma sessão por vez)
        self._state = POP3State.AUTHORIZATION
        self._timestamp = f"<{int(time.time())}.{id(self)}@hfpop3>"
        self._current_user = ""
        self._user_messages: list[StoredMessage] = []

        # Conjunto de peers que já receberam o greeting espontâneo na sessão
        # corrente (RFC 1939 / F.6: greeting é enviado pelo servidor ao
        # estabelecer a conexão, não em resposta a um comando do cliente).
        self._greeted_peers: set[int] = set()

    def send_greeting_to(self, dest_addr: int,
                         priority: int = 10,
                         ttl_seconds: float = 120.0) -> None:
        """Envia o greeting POP3 espontaneamente para ``dest_addr``.

        Deve ser chamado pelo orquestrador imediatamente após o
        ``S_HARD_LINK_ESTABLISHED`` (ou no bind do SAP em testes). Marca o
        peer como "greeted" para evitar duplicação no primeiro UNIDATA.
        """
        greeting = self.get_greeting()
        self._greeted_peers.add(dest_addr)
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=greeting,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def _on_data_received(self, src_addr: int, data: bytes):
        """Parse e processa comandos POP3."""
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\r\n")
        response = b""

        # F.6 / RFC 1939: o servidor envia o greeting (com timestamp APOP)
        # espontaneamente ao aceitar a conexão. Se não fomos avisados pelo
        # orquestrador via ``send_greeting_to``, emitimos no primeiro
        # contato e marcamos o peer como cumprimentado.
        if src_addr not in self._greeted_peers \
                and self._state == POP3State.AUTHORIZATION:
            response += self.get_greeting()
            self._greeted_peers.add(src_addr)

        for line in lines:
            line = line.strip()
            if not line:
                continue
            cmd = parse_command(line)
            resp = self._handle_command(cmd, line)
            if resp:
                response += resp

        if response:
            self._send_data(
                dest_addr=src_addr,
                dest_sap=self.SAP_ID,
                data=response,
                priority=10,
                ttl_seconds=120.0,
                mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
            )

    def get_greeting(self) -> bytes:
        """Retorna saudação POP3 com timestamp (enviada na conexão ARQ).

        Per RFC 1939 / F.6: o servidor envia a saudação ao aceitar a conexão.
        """
        self._timestamp = f"<{int(time.time())}.{id(self)}@hfpop3>"
        self._state = POP3State.AUTHORIZATION
        return format_pop3_ok(f"POP3 server ready {self._timestamp}")

    def _handle_command(self, cmd: CommandResponse, raw_line: str) -> bytes:
        """Processa um comando e retorna resposta."""

        if cmd.keyword == "NOOP":
            # RFC 1939 §5: NOOP sempre responde com +OK simples. O greeting é
            # emitido espontaneamente pelo servidor ao iniciar a sessão (ver
            # ``send_greeting_to`` e o prepend em ``_on_data_received``); não
            # depende mais de NOOP como gatilho.
            return format_pop3_ok("OK")

        if cmd.keyword == "APOP":
            if self._state != POP3State.AUTHORIZATION:
                return format_pop3_err("command not valid in this state")
            return self._handle_apop(cmd.args)

        if cmd.keyword == "LIST":
            if self._state != POP3State.TRANSACTION:
                return format_pop3_err("command not valid in this state")
            return self._handle_list(cmd.args)

        if cmd.keyword == "RETR":
            if self._state != POP3State.TRANSACTION:
                return format_pop3_err("command not valid in this state")
            return self._handle_retr(cmd.args)

        if cmd.keyword == "DELE":
            if self._state != POP3State.TRANSACTION:
                return format_pop3_err("command not valid in this state")
            return self._handle_dele(cmd.args)

        if cmd.keyword == "QUIT":
            return self._handle_quit()

        return format_pop3_err(f"unknown command: {cmd.keyword}")

    def _handle_apop(self, args: str) -> bytes:
        """APOP name digest — autenticação MD5."""
        parts = args.strip().split()
        if len(parts) < 2:
            return format_pop3_err("syntax error in APOP")

        name = parts[0]
        digest = parts[1]

        secret = self._shared_secrets.get(name)
        if secret is None:
            return format_pop3_err("permission denied")

        expected = hashlib.md5(
            f"{self._timestamp}{secret}".encode("utf-8")
        ).hexdigest()

        if digest.lower() != expected.lower():
            return format_pop3_err("permission denied")

        # Autenticação OK — adquire maildrop
        self._current_user = name
        self._user_messages = list(self._maildrop.get(name, []))
        self._state = POP3State.TRANSACTION

        # Resposta com scan listing embutido (otimização HF)
        total_msgs = sum(1 for m in self._user_messages if not m.deleted)
        total_size = sum(m.size for m in self._user_messages if not m.deleted)

        response = format_pop3_ok(
            f"maildrop has {total_msgs} message(s) ({total_size} octets)"
        )

        # Scan listing
        for i, msg in enumerate(self._user_messages, 1):
            if not msg.deleted:
                response += f"{i} {msg.size}\r\n".encode("utf-8")
        response += b".\r\n"

        return response

    def _handle_list(self, args: str) -> bytes:
        """LIST [nn]."""
        args = args.strip()
        if args and args.isdigit():
            n = int(args)
            if n < 1 or n > len(self._user_messages):
                return format_pop3_err(
                    f"no such message, only {len(self._user_messages)} messages in maildrop"
                )
            msg = self._user_messages[n - 1]
            if msg.deleted:
                return format_pop3_err("message is deleted")
            return format_pop3_ok(f"{n} {msg.size}")
        else:
            active = [(i, m) for i, m in enumerate(self._user_messages, 1) if not m.deleted]
            total_size = sum(m.size for _, m in active)
            response = format_pop3_ok(
                f"{len(active)} messages ({total_size} octets)"
            )
            for i, msg in active:
                response += f"{i} {msg.size}\r\n".encode("utf-8")
            response += b".\r\n"
            return response

    def _handle_retr(self, args: str) -> bytes:
        """RETR [nn]."""
        args = args.strip()
        if args and args.isdigit():
            n = int(args)
            if n < 1 or n > len(self._user_messages):
                return format_pop3_err("no such message")
            msg = self._user_messages[n - 1]
            if msg.deleted:
                return format_pop3_err("message is deleted")
            response = format_pop3_ok(f"message {n} follows")
            response += (byte_stuff(msg.body) + "\r\n.\r\n").encode("utf-8")
            return response
        else:
            # Envia todas as mensagens não deletadas
            active = [(i, m) for i, m in enumerate(self._user_messages, 1) if not m.deleted]
            if not active:
                return format_pop3_err("no messages")
            response = format_pop3_ok(f"{len(active)} messages follow")
            for i, msg in active:
                response += f"--- message {i} ---\r\n".encode("utf-8")
                response += (byte_stuff(msg.body) + "\r\n").encode("utf-8")
            response += b".\r\n"
            return response

    def _handle_dele(self, args: str) -> bytes:
        """DELE nn — marca mensagem para deleção."""
        args = args.strip()
        if not args.isdigit():
            return format_pop3_err("syntax error")
        n = int(args)
        if n < 1 or n > len(self._user_messages):
            return format_pop3_err("no such message")
        msg = self._user_messages[n - 1]
        if msg.deleted:
            return format_pop3_err(f"message {n} already deleted")
        msg.deleted = True
        return format_pop3_ok(f"message {n} deleted")

    def _handle_quit(self) -> bytes:
        """QUIT — entra em estado UPDATE."""
        if self._state == POP3State.TRANSACTION:
            # Aplica deleções no maildrop real
            if self._current_user in self._maildrop:
                user_msgs = self._maildrop[self._current_user]
                for i, stored in enumerate(self._user_messages):
                    if stored.deleted and i < len(user_msgs):
                        user_msgs[i].deleted = True
                # Remove deletadas
                self._maildrop[self._current_user] = [
                    m for m in user_msgs if not m.deleted
                ]

        self._state = POP3State.AUTHORIZATION
        self._current_user = ""
        self._user_messages = []
        return format_pop3_ok("bye")
