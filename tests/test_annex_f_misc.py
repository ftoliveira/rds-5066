"""Tests for src/annex_f/fab.py and src/annex_f/subnet_mgmt.py."""

from src.annex_f.fab import FABGenerator, FABReceiver, DEFAULT_FAB_SAP
from src.annex_f.subnet_mgmt import SubnetMgmtClient

from tests.annex_f_helpers import MockNode, deliver


# ===========================================================================
# FABGenerator (SAP 15)
# ===========================================================================

class TestFABGenerator:
    def test_first_tick_broadcasts(self):
        node = MockNode()
        g = FABGenerator(node, broadcast_addr=255, update_interval_s=30.0)
        g.update_fai(b"FAI_DATA")
        g.tick_broadcast(current_time_ms=1000)
        assert len(node.sent) == 1
        assert node.sent[0]["updu"] == b"FAI_DATA"
        assert node.sent[0]["dest_addr"] == 255

    def test_within_interval_no_broadcast(self):
        node = MockNode()
        g = FABGenerator(node, broadcast_addr=255, update_interval_s=30.0)
        g.update_fai(b"FAI")
        g.tick_broadcast(current_time_ms=1000)
        node.sent.clear()
        g.tick_broadcast(current_time_ms=2000)  # only 1s later
        assert len(node.sent) == 0

    def test_after_interval_broadcasts_again(self):
        node = MockNode()
        g = FABGenerator(node, broadcast_addr=255, update_interval_s=10.0)
        g.update_fai(b"FAI")
        g.tick_broadcast(current_time_ms=1000)
        node.sent.clear()
        g.tick_broadcast(current_time_ms=12000)  # 11s later, interval=10s
        assert len(node.sent) == 1

    def test_no_fai_data_no_broadcast(self):
        node = MockNode()
        g = FABGenerator(node, broadcast_addr=255)
        g.tick_broadcast(current_time_ms=1000)
        assert len(node.sent) == 0

    def test_update_fai_changes_data(self):
        node = MockNode()
        g = FABGenerator(node, broadcast_addr=255, update_interval_s=1.0)
        g.update_fai(b"OLD")
        g.tick_broadcast(current_time_ms=1000)
        assert node.sent[0]["updu"] == b"OLD"
        g.update_fai(b"NEW")
        g.tick_broadcast(current_time_ms=3000)
        assert node.sent[1]["updu"] == b"NEW"

    def test_non_arq_mode(self):
        node = MockNode()
        g = FABGenerator(node, broadcast_addr=255)
        g.update_fai(b"X")
        g.tick_broadcast(current_time_ms=1000)
        assert node.sent[0]["mode"].arq_mode is False

    def test_custom_sap(self):
        node = MockNode()
        g = FABGenerator(node, broadcast_addr=255, sap_id=10)
        assert g.SAP_ID == 10


# ===========================================================================
# FABReceiver (SAP 15)
# ===========================================================================

class TestFABReceiver:
    def test_callback_invoked(self):
        node = MockNode()
        r = FABReceiver(node)
        received = []
        r.on_fai_received = lambda addr, data: received.append((addr, data))
        deliver(r, src_addr=42, data=b"FAI_PAYLOAD")
        assert len(received) == 1
        assert received[0] == (42, b"FAI_PAYLOAD")

    def test_custom_sap(self):
        node = MockNode()
        r = FABReceiver(node, sap_id=10)
        assert r.SAP_ID == 10

    def test_default_sap_is_15(self):
        assert DEFAULT_FAB_SAP == 15


# ===========================================================================
# SubnetMgmtClient (SAP 0)
# ===========================================================================

class TestSubnetMgmtClient:
    def test_send_mgmt_remote(self):
        node = MockNode()
        c = SubnetMgmtClient(node)
        c.send_mgmt(dest_addr=10, payload=b"CMD")
        assert len(node.sent) == 1
        assert node.sent[0]["updu"] == b"CMD"
        assert node.sent[0]["dest_addr"] == 10

    def test_send_mgmt_arq_default(self):
        node = MockNode()
        c = SubnetMgmtClient(node)
        c.send_mgmt(dest_addr=1, payload=b"X")
        assert node.sent[0]["mode"].arq_mode is True

    def test_send_mgmt_non_arq(self):
        node = MockNode()
        c = SubnetMgmtClient(node)
        c.send_mgmt(dest_addr=1, payload=b"X", arq=False)
        assert node.sent[0]["mode"].arq_mode is False

    def test_bind_rank_15_default(self):
        node = MockNode()
        c = SubnetMgmtClient(node)
        c.bind()
        assert node.binds[0][1] == 15  # rank

    def test_send_local_mgmt_rank15_accepted(self):
        node = MockNode()
        c = SubnetMgmtClient(node)
        c.bind(rank=15)
        received = []
        c.on_mgmt_received = lambda addr, data: received.append((addr, data))
        result = c.send_local_mgmt(b"LOCAL_CMD")
        assert result is True
        assert received == [(0, b"LOCAL_CMD")]

    def test_send_local_mgmt_low_rank_rejected(self):
        node = MockNode()
        c = SubnetMgmtClient(node)
        c.bind(rank=5)
        result = c.send_local_mgmt(b"LOCAL_CMD")
        assert result is False

    def test_receive_callback(self):
        node = MockNode()
        c = SubnetMgmtClient(node)
        received = []
        c.on_mgmt_received = lambda addr, data: received.append((addr, data))
        deliver(c, src_addr=77, data=b"MGMT_PAYLOAD")
        assert received == [(77, b"MGMT_PAYLOAD")]

    def test_sap_id_is_0(self):
        assert SubnetMgmtClient.SAP_ID == 0
