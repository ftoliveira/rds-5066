"""Tests for src/annex_f/base_client.py — SubnetClient and AnnexFDispatcher."""

from src.annex_f.base_client import SubnetClient, AnnexFDispatcher
from src.stypes import DeliveryMode, SisUnidataIndication

from tests.annex_f_helpers import MockNode, deliver


# ---------------------------------------------------------------------------
# Concrete subclass for testing
# ---------------------------------------------------------------------------

class _TestClient(SubnetClient):
    SAP_ID = 99

    def __init__(self, node):
        super().__init__(node)
        self.received: list[tuple[int, bytes]] = []

    def _on_data_received(self, src_addr: int, data: bytes):
        self.received.append((src_addr, data))


# ---------------------------------------------------------------------------
# SubnetClient
# ---------------------------------------------------------------------------

class TestSubnetClient:
    def test_send_data_calls_unidata_request(self):
        node = MockNode()
        client = _TestClient(node)
        client._send_data(dest_addr=42, dest_sap=99, data=b"payload",
                          priority=7, ttl_seconds=60.0)
        assert len(node.sent) == 1
        s = node.sent[0]
        assert s["sap_id"] == 99
        assert s["dest_addr"] == 42
        assert s["dest_sap"] == 99
        assert s["updu"] == b"payload"
        assert s["priority"] == 7
        assert s["ttl_seconds"] == 60.0

    def test_send_data_default_mode_none(self):
        node = MockNode()
        client = _TestClient(node)
        client._send_data(dest_addr=1, dest_sap=99, data=b"x")
        assert node.sent[0]["mode"] is None

    def test_send_data_custom_delivery_mode(self):
        node = MockNode()
        client = _TestClient(node)
        mode = DeliveryMode(arq_mode=True, in_order=True)
        client._send_data(dest_addr=1, dest_sap=99, data=b"x", mode=mode)
        assert node.sent[0]["mode"] is mode
        assert node.sent[0]["mode"].in_order is True

    def test_on_unidata_indication_routes_to_on_data_received(self):
        node = MockNode()
        client = _TestClient(node)
        deliver(client, src_addr=10, data=b"hello")
        assert len(client.received) == 1
        assert client.received[0] == (10, b"hello")

    def test_bind_calls_node_bind(self):
        node = MockNode()
        client = _TestClient(node)
        client.bind(rank=5)
        assert node.binds == [(99, 5, None)]


# ---------------------------------------------------------------------------
# AnnexFDispatcher
# ---------------------------------------------------------------------------

class _ClientA(SubnetClient):
    SAP_ID = 10

    def __init__(self, node):
        super().__init__(node)
        self.received = []

    def _on_data_received(self, src_addr, data):
        self.received.append((src_addr, data))


class _ClientB(SubnetClient):
    SAP_ID = 20

    def __init__(self, node):
        super().__init__(node)
        self.received = []

    def _on_data_received(self, src_addr, data):
        self.received.append((src_addr, data))


class TestAnnexFDispatcher:
    def test_register_and_dispatch(self):
        node = MockNode()
        disp = AnnexFDispatcher(node)
        client = _ClientA(node)
        disp.register(client)
        # Simulate indication
        ind = SisUnidataIndication(dest_sap=10, src_addr=99, src_sap=0,
                                   priority=0, updu=b"data")
        disp._on_unidata(ind)
        assert client.received == [(99, b"data")]

    def test_unknown_sap_ignored(self):
        node = MockNode()
        disp = AnnexFDispatcher(node)
        ind = SisUnidataIndication(dest_sap=77, src_addr=1, src_sap=0,
                                   priority=0, updu=b"x")
        # Should not raise
        disp._on_unidata(ind)

    def test_multiple_clients_routed_correctly(self):
        node = MockNode()
        disp = AnnexFDispatcher(node)
        a = _ClientA(node)
        b = _ClientB(node)
        disp.register(a)
        disp.register(b)

        disp._on_unidata(SisUnidataIndication(
            dest_sap=10, src_addr=1, src_sap=0, priority=0, updu=b"for_a"))
        disp._on_unidata(SisUnidataIndication(
            dest_sap=20, src_addr=2, src_sap=0, priority=0, updu=b"for_b"))

        assert a.received == [(1, b"for_a")]
        assert b.received == [(2, b"for_b")]

    def test_install_callbacks_registers_on_node(self):
        node = MockNode()
        disp = AnnexFDispatcher(node)
        disp.install_callbacks()
        assert "unidata_indication" in node._callbacks
        assert "request_rejected" in node._callbacks

    def test_rejected_dispatched_to_client(self):
        node = MockNode()
        disp = AnnexFDispatcher(node)
        client = _ClientA(node)
        disp.register(client)
        # Should not raise even for unregistered SAP
        disp._on_rejected(77, "test reason")
        # For registered SAP, should not raise
        disp._on_rejected(10, "test reason")
