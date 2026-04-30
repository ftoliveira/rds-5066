"""
F.2 — Subnet Management Client/Server (SAP 0).

Fornece interface mínima para gerenciamento da sub-rede HF local e remota,
usando os primitivos S_MANAGEMENT_MSG_REQUEST / S_MANAGEMENT_MSG_INDICATION
e S_UNIDATA_* para comunicação peer-to-peer (F.2).

Requisitos (F.2):
  - SAP_ID = 0
  - Rank = 15 para submeter comandos que alteram configuração
  - Comunicação peer-to-peer via S_UNIDATA_REQUEST/INDICATION
  - Para gerenciamento via IP: usar IP Client (F.12) com SNMP (recomendado)
  - MIB atualmente indefinida pelo STANAG

Esta implementação fornece:
  - Bind e envio de mensagens de gerenciamento (payload arbitrário)
  - Callback para mensagens de gerenciamento recebidas
  - Wrapper para S_MANAGEMENT_MSG_REQUEST de gerenciamento local

Nota: Implementações interoperáveis de peer-to-peer devem usar SNMP via IP Client.
"""

from __future__ import annotations

import logging
from typing import Callable

from src.stypes import (
    DeliveryMode,
    SAP_ID_SUBNET_MANAGEMENT,
    ServiceType,
    SisUnidataIndication,
)

from .base_client import SubnetClient

logger = logging.getLogger(__name__)


class SubnetMgmtClient(SubnetClient):
    """F.2 — Subnet Management Client, SAP 0.

    Bind com Rank=15 para emitir comandos de configuração.

    Envio de mensagem de gerenciamento para nó remoto:
        client.send_mgmt(dest_addr, payload)

    Recepção:
        client.on_mgmt_received = callback(src_addr, payload: bytes)

    Gerenciamento local (via S_MANAGEMENT_MSG_REQUEST):
        Disponível diretamente via node.management_request(...)
        quando suportado pelo StanagNode.
    """

    SAP_ID = SAP_ID_SUBNET_MANAGEMENT  # 0

    def __init__(self, node, connection_id: int = 0):
        super().__init__(node, connection_id)
        self.on_mgmt_received: Callable[[int, bytes], None] | None = None

    def bind(self, rank: int = 15, service: ServiceType | None = None):
        """Bind com rank=15 por padrão (necessário para comandos de configuração)."""
        super().bind(rank=rank, service=service)

    def send_mgmt(
        self,
        dest_addr: int,
        payload: bytes,
        priority: int = 14,
        ttl_seconds: float = 60.0,
        arq: bool = True,
    ) -> None:
        """Envia payload de gerenciamento para nó remoto via S_UNIDATA_REQUEST.

        Para comunicação interoperável, o payload deve seguir SNMP ou outro
        protocolo de gerenciamento de rede padronizado (recomendação F.2).
        """
        self._send_data(
            dest_addr=dest_addr,
            dest_sap=self.SAP_ID,
            data=payload,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=DeliveryMode(arq_mode=arq),
        )
        logger.debug(
            "SubnetMgmt → addr=%d %d bytes",
            dest_addr, len(payload),
        )

    def send_local_mgmt(self, message: bytes) -> bool:
        """Envia S_MANAGEMENT_MSG_REQUEST para gerenciamento do nó local.

        Requer rank=15 (A.2.1.15§3). Usa o primitivo S_MANAGEMENT_MSG_REQUEST
        que opera diretamente no nó local sem transmissão remota.

        Returns True se aceito, False se rejeitado (rank insuficiente).
        """
        if not self.node.validate_management_msg_rank(self.SAP_ID):
            logger.warning(
                "SubnetMgmt: S_MANAGEMENT_MSG_REQUEST rejeitado — rank != 15"
            )
            return False
        # Despacha como mensagem de gerenciamento local
        logger.debug("SubnetMgmt: local management msg %d bytes", len(message))
        if self.on_mgmt_received:
            self.on_mgmt_received(0, message)  # src_addr=0 indica local
        return True

    def _on_data_received(self, src_addr: int, data: bytes):
        """Processa mensagem de gerenciamento recebida via S_UNIDATA_INDICATION."""
        logger.debug(
            "SubnetMgmt ← addr=%d %d bytes", src_addr, len(data)
        )
        if self.on_mgmt_received:
            self.on_mgmt_received(src_addr, data)
        else:
            logger.info(
                "SubnetMgmt: mensagem de gerenciamento de addr=%d ignorada "
                "(nenhum handler registrado)",
                src_addr,
            )
