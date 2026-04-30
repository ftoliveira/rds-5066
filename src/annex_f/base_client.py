"""
Classe base SubnetClient + AnnexFDispatcher — Anexo F.

SubnetClient encapsula a interação com SIS (bind, unidata_request).

Clientes padronizados (COSS, HMTP, HFPOP, HFCHAT, IP, ETHER, SubnetMgmt)
colocam seus dados diretamente no campo U_PDU do S_UNIDATA_REQUEST, sem
cabeçalho UPDU intermediário. Usam _send_data() e _on_data_received().

RCOP/UDOP têm seu próprio cabeçalho PDU de 6 bytes e fazem override direto.

AnnexFDispatcher roteia indicações SIS para o cliente correto por SAP ID.
"""

from __future__ import annotations

import logging
from typing import Callable

from src.stanag_node import StanagNode
from src.stypes import DeliveryMode, ServiceType, SisUnidataIndication

logger = logging.getLogger(__name__)


class SubnetClient:
    """Classe base para clientes de sub-rede Anexo F.

    Subclasses devem definir SAP_ID e implementar _on_data_received().

    Dados são colocados diretamente no U_PDU field do S_UNIDATA_REQUEST
    conforme a especificação (sem cabeçalho UPDU intermediário).
    """
    SAP_ID: int = -1  # Override por subclasse (Tabela F-1)

    def __init__(self, node: StanagNode, connection_id: int = 0):
        self.node = node
        self.connection_id = connection_id

    def bind(self, rank: int = 0, service: ServiceType | None = None):
        """Faz bind do SAP ID deste cliente na SIS."""
        self.node.bind(self.SAP_ID, rank=rank, service=service)

    def _send_data(self, dest_addr: int, dest_sap: int, data: bytes,
                   priority: int = 5, ttl_seconds: float = 120.0,
                   mode: DeliveryMode | None = None) -> None:
        """Envia dados diretamente no U_PDU via S_UNIDATA_REQUEST.

        Os dados são colocados diretamente no campo U_PDU, alinhados ao
        primeiro byte, conforme especificado para cada cliente no Anexo F.
        """
        self.node.unidata_request(
            sap_id=self.SAP_ID,
            dest_addr=dest_addr,
            dest_sap=dest_sap,
            priority=priority,
            ttl_seconds=ttl_seconds,
            mode=mode,
            updu=data,
        )

        logger.debug(
            "SubnetClient SAP=%d enviou %d bytes para addr=%d sap=%d",
            self.SAP_ID, len(data), dest_addr, dest_sap,
        )

    def on_unidata_indication(self, indication: SisUnidataIndication):
        """Chamado pelo dispatcher. Entrega dados raw ao cliente."""
        self._on_data_received(indication.src_addr, indication.updu)

    def _on_data_received(self, src_addr: int, data: bytes):
        """Override em subclasse para processar dados recebidos."""
        pass

    def on_request_rejected(self, sap_id: int, reason):
        """Chamado quando um request é rejeitado pela SIS."""
        logger.warning("SubnetClient SAP=%d request rejeitado: %s", sap_id, reason)


class AnnexFDispatcher:
    """Roteia indicações SIS para o cliente correto por SAP ID."""

    def __init__(self, node: StanagNode):
        self.node = node
        self._clients: dict[int, SubnetClient] = {}

    def register(self, client: SubnetClient, rank: int = 0,
                 service: ServiceType | None = None):
        """Faz bind do cliente e registra para roteamento."""
        sap_id = client.SAP_ID
        if sap_id in self._clients:
            logger.warning("SAP %d já registrado, substituindo", sap_id)
        self._clients[sap_id] = client
        client.bind(rank=rank, service=service)
        logger.debug("Registrado cliente SAP=%d (%s)", sap_id, type(client).__name__)

    def install_callbacks(self):
        """Instala callbacks SIS para roteamento.

        Deve ser chamado após todos os clientes serem registrados.
        Não sobrescreve callbacks de hard_link — esses devem ser
        registrados separadamente pelo código de teste.
        """
        self.node.register_callbacks(
            unidata_indication=self._on_unidata,
            request_rejected=self._on_rejected,
        )

    def _on_unidata(self, indication: SisUnidataIndication):
        """Despacha indicação para o cliente correto por dest_sap."""
        client = self._clients.get(indication.dest_sap)
        if client is not None:
            client.on_unidata_indication(indication)
        else:
            logger.warning(
                "Indicação para SAP %d sem cliente registrado (src=%d)",
                indication.dest_sap, indication.src_addr,
            )

    def _on_rejected(self, sap_id, reason):
        """Repassa rejeição ao cliente."""
        client = self._clients.get(sap_id)
        if client is not None:
            client.on_request_rejected(sap_id, reason)
        else:
            logger.warning("Rejeição para SAP %d sem cliente: %s", sap_id, reason)
