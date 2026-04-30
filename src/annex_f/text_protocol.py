"""
Parser de protocolo texto CRLF compartilhado — usado por F.1, F.3 e F.4.

Terminadores:
  CRLF           — fim de linha/comando
  <CRLF>.<CRLF>  — fim de dados/mensagem
  <CRLF>.<CRLF><CRLF>.<CRLF> — fim de sequência múltipla
"""

from __future__ import annotations

from dataclasses import dataclass

CRLF = b"\r\n"
DOT_CRLF = b"\r\n.\r\n"              # Fim de dados/mensagem
DOUBLE_DOT_CRLF = b"\r\n.\r\n\r\n.\r\n"  # Fim de sequência múltipla


@dataclass(slots=True)
class CommandResponse:
    """Comando ou resposta parseado."""
    code: int       # 250, 450, 550 (0 para comandos)
    keyword: str    # SEND, MAIL, +OK, -ERR
    args: str       # Restante da linha


class TextProtocolParser:
    """Parser incremental para protocolos texto CRLF."""

    def __init__(self):
        self._buffer = b""

    def feed(self, data: bytes) -> list[str]:
        """Alimenta dados e retorna linhas completas (sem CRLF)."""
        self._buffer += data
        lines = []
        while CRLF in self._buffer:
            idx = self._buffer.index(CRLF)
            line = self._buffer[:idx].decode("utf-8", errors="replace")
            self._buffer = self._buffer[idx + 2:]
            lines.append(line)
        return lines

    def feed_multiline(self, data: bytes) -> tuple[list[str], bool]:
        """Alimenta dados multi-linha. Retorna (linhas, completo).

        Completo quando <CRLF>.<CRLF> é encontrado.
        Faz byte-unstuffing (remove '.' duplicado no início de linhas).
        """
        self._buffer += data
        lines = []
        complete = False

        while CRLF in self._buffer:
            idx = self._buffer.index(CRLF)
            raw_line = self._buffer[:idx]
            self._buffer = self._buffer[idx + 2:]

            # Linha de terminação: apenas '.'
            if raw_line == b".":
                complete = True
                break

            # Byte-unstuffing: '..' no início -> '.'
            line_str = raw_line.decode("utf-8", errors="replace")
            if line_str.startswith(".."):
                line_str = line_str[1:]
            lines.append(line_str)

        return lines, complete

    def reset(self):
        """Limpa buffer interno."""
        self._buffer = b""


def parse_command(line: str) -> CommandResponse:
    """Parse uma linha de comando ou resposta.

    Detecta respostas numéricas (250, 450, 550) e indicadores POP3 (+OK, -ERR).
    """
    parts = line.strip().split(None, 1)
    if not parts:
        return CommandResponse(0, "", "")

    keyword = parts[0].upper()
    args = parts[1] if len(parts) > 1 else ""

    # Resposta numérica (SMTP/HMTP)
    if keyword.isdigit() and len(keyword) == 3:
        return CommandResponse(int(keyword), keyword, args)

    # Indicadores POP3
    if keyword in ("+OK", "-ERR"):
        return CommandResponse(0, keyword, args)

    # Comando
    return CommandResponse(0, keyword, args)


def format_response(code: int, text: str) -> bytes:
    """Formata uma resposta com código numérico."""
    return f"{code} {text}\r\n".encode("utf-8")


def format_pop3_ok(text: str) -> bytes:
    """Formata resposta POP3 positiva."""
    return f"+OK {text}\r\n".encode("utf-8")


def format_pop3_err(text: str) -> bytes:
    """Formata resposta POP3 negativa."""
    return f"-ERR {text}\r\n".encode("utf-8")


def byte_stuff(data: str) -> str:
    """Byte-stuffing: linhas que começam com '.' recebem '.' extra."""
    lines = data.split("\r\n")
    stuffed = []
    for line in lines:
        if line.startswith("."):
            stuffed.append("." + line)
        else:
            stuffed.append(line)
    return "\r\n".join(stuffed)


def encode_multiline_data(body: str) -> bytes:
    """Codifica corpo de dados com byte-stuffing e terminador <CRLF>.<CRLF>."""
    stuffed = byte_stuff(body)
    return (stuffed + "\r\n.\r\n").encode("utf-8")


def encode_end_of_sequence() -> bytes:
    """Codifica terminador de sequência múltipla <CRLF>.<CRLF><CRLF>.<CRLF>."""
    return b"\r\n.\r\n\r\n.\r\n"
