"""
F.7 — Operator Orderwire (Edition 3: HFCHAT, SAP 5).

Conforme STANAG 5066 Ed.3, Anexo F, Seção F.7:
- Payload é ASCII puro (ITA5) terminado com CRLF (0x0D 0x0A).
- Nenhum prefixo de controle proprietário no payload.
- ARQ (ponto-a-ponto) vs non-ARQ (broadcast) é definido pelo DeliveryMode.
"""

from __future__ import annotations

import logging
from typing import Callable

from src.stypes import DeliveryMode

from .base_client import SubnetClient

logger = logging.getLogger(__name__)


class OrderwireClient(SubnetClient):
    """Cliente Operator Orderwire — Edition 3: HFCHAT, SAP 5."""
    SAP_ID = 5

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_message_received: Callable[[int, str], None] | None = None

    def send_acknowledged(self, dest_addr: int, text: str,
                          priority: int = 10, ttl_seconds: float = 60.0):
        """Envia orderwire reconhecido (ARQ, ponto-a-ponto). F.7.2."""
        data = text.encode("ascii", errors="replace") + b"\r\n"
        # F.7.2: MSB de cada byte ITA5 deve ser zero
        data = bytes(b & 0x7F for b in data)
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=True, node_delivery_confirm=True),
        )

    def send_broadcast(self, dest_addr: int, text: str,
                       priority: int = 5, ttl_seconds: float = 60.0):
        """Envia orderwire broadcast (non-ARQ, ponto-a-multiponto). F.7.2."""
        data = text.encode("ascii", errors="replace") + b"\r\n"
        # F.7.2: MSB de cada byte ITA5 deve ser zero
        data = bytes(b & 0x7F for b in data)
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=data,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=False),
        )

    def _on_data_received(self, src_addr: int, data: bytes):
        """Processa orderwire recebido — ASCII puro terminado em CRLF."""
        text = data.decode("ascii", errors="replace").rstrip("\r\n")

        logger.debug(
            "F.7 orderwire de addr=%d: text=%r",
            src_addr, text[:80],
        )
        if self.on_message_received:
            self.on_message_received(src_addr, text)
