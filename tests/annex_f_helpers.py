"""Shared test helpers for Annex F unit tests."""

from src.stypes import SisUnidataIndication


class MockNode:
    """Lightweight mock replacing StanagNode for Annex F unit tests."""

    def __init__(self):
        self.sent: list[dict] = []
        self.binds: list[tuple] = []
        self._mgmt_ranks: dict[int, int] = {}
        self._callbacks: dict = {}

    def unidata_request(self, sap_id, dest_addr, dest_sap, priority,
                        ttl_seconds, mode=None, updu=b""):
        self.sent.append(dict(
            sap_id=sap_id, dest_addr=dest_addr, dest_sap=dest_sap,
            priority=priority, ttl_seconds=ttl_seconds, mode=mode, updu=updu,
        ))

    def bind(self, sap_id, rank=0, service=None):
        self.binds.append((sap_id, rank, service))
        self._mgmt_ranks[sap_id] = rank
        return sap_id

    def validate_management_msg_rank(self, sap_id):
        return self._mgmt_ranks.get(sap_id, 0) == 15

    def register_callbacks(self, **kwargs):
        self._callbacks = kwargs


def deliver(client, src_addr, data, src_sap=0):
    """Simulate SIS delivering data to a client."""
    ind = SisUnidataIndication(
        dest_sap=client.SAP_ID,
        src_addr=src_addr,
        src_sap=src_sap,
        priority=0,
        updu=data,
    )
    client.on_unidata_indication(ind)
