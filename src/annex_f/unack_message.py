"""
Mensagem Não Reconhecida (SAP 14) — Protocolo de demonstração.

O cliente mais simples — non-ARQ, sem relação cliente-servidor.
SAP_ID 14: porta não atribuída pela norma ("UNASSIGNED – available for arbitrary use",
Tabela F-1, Anexo F, STANAG 5066 Ed.3).
Suporta endereço individual ou de grupo.
"""

from __future__ import annotations

import logging
from typing import Callable

from src.stypes import DeliveryMode

from .base_client import SubnetClient

logger = logging.getLogger(__name__)


class UnackMessageClient(SubnetClient):
    """Cliente de mensagem não reconhecida, SAP 14 (porta não atribuída)."""
    SAP_ID = 14

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_message_received: Callable[[int, bytes], None] | None = None

    def send_message(self, dest_addr: int, message: bytes,
                     group_address: bool = False,
                     priority: int = 5, ttl_seconds: float = 60.0) -> None:
        """Envia mensagem non-ARQ (broadcast ou individual)."""
        mode = DeliveryMode(arq_mode=False)
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=message,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=mode,
        )

    def _on_data_received(self, src_addr: int, data: bytes):
        """Repassa mensagem recebida ao callback."""
        logger.debug("F.15 mensagem de addr=%d, %d bytes", src_addr, len(data))
        if self.on_message_received:
            self.on_message_received(src_addr, data)
