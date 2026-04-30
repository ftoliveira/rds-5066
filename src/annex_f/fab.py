"""
FAB (Frequency Availability Broadcast) — extensão **não-normativa**.

⚠️ Aviso: este módulo NÃO faz parte da norma STANAG 5066 Edição 3 e foi
mantido em ``src/annex_f/`` apenas por conveniência. Para evitar conflito
com clientes normativos, o FAB usa SAP 15 (faixa "UNASSIGNED – arbitrary
use", Tabela F-1). O SAP é configurável via ``sap_id`` no construtor.

Componentes:
  FABGenerator — broadcast non-ARQ periódico (lado shore).
  FABReceiver  — escuta FAI broadcasts (lado ship).

Implementações conformantes podem ignorar este módulo. Caso seja necessário
maior isolamento, mover para um package ``src/extras/`` é recomendado em
revisões futuras.
"""

from __future__ import annotations

import logging
from typing import Callable

from src.stypes import DeliveryMode, SAP_ID_UNASSIGNED_15

from .base_client import SubnetClient

logger = logging.getLogger(__name__)

# Default SAP para FAB — usa porta não atribuída (Table F-1)
DEFAULT_FAB_SAP = SAP_ID_UNASSIGNED_15  # 15


class FABGenerator(SubnetClient):
    """Gerador FAB — lado shore. Broadcast non-ARQ periódico."""
    SAP_ID = DEFAULT_FAB_SAP

    def __init__(self, node, broadcast_addr: int,
                 update_interval_s: float = 30.0,
                 connection_id: int = 0,
                 sap_id: int = DEFAULT_FAB_SAP):
        super().__init__(node, connection_id)
        self.SAP_ID = sap_id
        self._broadcast_addr = broadcast_addr
        self._update_interval_ms = int(update_interval_s * 1000)
        self._fai_data: bytes = b""
        self._last_broadcast_ms: int = 0

    def update_fai(self, fai_data: bytes):
        """Submete nova FAI (Frequency Availability Information)."""
        self._fai_data = fai_data
        logger.debug("FAB atualizado: %d bytes", len(fai_data))

    def tick_broadcast(self, current_time_ms: int):
        """Chamado do loop principal. Envia broadcast se intervalo expirou."""
        if not self._fai_data:
            return

        elapsed = current_time_ms - self._last_broadcast_ms
        if elapsed >= self._update_interval_ms or self._last_broadcast_ms == 0:
            self._do_broadcast()
            self._last_broadcast_ms = current_time_ms

    def _do_broadcast(self):
        """Envia FAI via non-ARQ broadcast."""
        ttl = self._update_interval_ms / 1000.0  # TTL não excede intervalo
        self._send_data(
            dest_addr=self._broadcast_addr,
            dest_sap=self.SAP_ID,
            data=self._fai_data,
            priority=5,
            ttl_seconds=ttl,
            mode=DeliveryMode(arq_mode=False),
        )
        logger.debug(
            "FAB broadcast enviado: %d bytes para addr=%d",
            len(self._fai_data), self._broadcast_addr,
        )


class FABReceiver(SubnetClient):
    """Receptor FAB — lado ship. Escuta FAI broadcasts."""
    SAP_ID = DEFAULT_FAB_SAP

    def __init__(self, node, connection_id: int = 0,
                 sap_id: int = DEFAULT_FAB_SAP):
        super().__init__(node, connection_id)
        self.SAP_ID = sap_id
        self.on_fai_received: Callable[[int, bytes], None] | None = None

    def _on_data_received(self, src_addr: int, data: bytes):
        """Repassa FAI recebido ao callback."""
        logger.debug("FAB recebido de addr=%d: %d bytes", src_addr, len(data))
        if self.on_fai_received:
            self.on_fai_received(src_addr, data)
