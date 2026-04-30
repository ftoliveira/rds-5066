"""Tests for unack_message.py, ack_message.py, orderwire.py."""

from src.annex_f.unack_message import UnackMessageClient
from src.annex_f.ack_message import AckMessageClient, AckMessageServer, SendMessage
from src.annex_f.orderwire import OrderwireClient
from src.stypes import DeliveryMode

from tests.annex_f_helpers import MockNode, deliver


# ===========================================================================
# UnackMessageClient (SAP 14)
# ===========================================================================

class TestUnackMessageClient:
    def test_send_uses_non_arq_mode(self):
        node = MockNode()
        c = UnackMessageClient(node)
        c.send_message(dest_addr=1, message=b"hi")
        assert node.sent[0]["mode"].arq_mode is False

    def test_send_uses_sap_14(self):
        node = MockNode()
        c = UnackMessageClient(node)
        c.send_message(dest_addr=1, message=b"hi")
        assert node.sent[0]["sap_id"] == 14
        assert node.sent[0]["dest_sap"] == 14

    def test_send_data_matches_message(self):
        node = MockNode()
        c = UnackMessageClient(node)
        c.send_message(dest_addr=1, message=b"hello world")
        assert node.sent[0]["updu"] == b"hello world"

    def test_receive_invokes_callback(self):
        node = MockNode()
        c = UnackMessageClient(node)
        received = []
        c.on_message_received = lambda addr, data: received.append((addr, data))
        deliver(c, src_addr=42, data=b"incoming")
        assert received == [(42, b"incoming")]

    def test_receive_no_callback_no_crash(self):
        node = MockNode()
        c = UnackMessageClient(node)
        deliver(c, src_addr=1, data=b"no crash")


# ===========================================================================
# AckMessageClient (SAP 13)
# ===========================================================================

class TestAckMessageClient:
    def test_send_single_message_format(self):
        node = MockNode()
        c = AckMessageClient(node)
        c.send_message(dest_addr=1, from_user="alice", to_users=["bob"],
                        body="Hello Bob")
        updu = node.sent[0]["updu"]
        assert b"SEND FROM:<alice>\r\n" in updu
        assert b"RCPT TO:<bob>\r\n" in updu
        assert b"DATA\r\n" in updu
        assert b"Hello Bob" in updu
        assert updu.endswith(b"\r\n.\r\n")

    def test_send_multiple_has_prefix(self):
        node = MockNode()
        c = AckMessageClient(node)
        msgs = [
            SendMessage("alice", ["bob"], "msg1"),
            SendMessage("alice", ["carol"], "msg2"),
        ]
        c.send_multiple(dest_addr=1, messages=msgs)
        updu = node.sent[0]["updu"]
        assert updu.startswith(b"SEND MULTIPLE\r\n")

    def test_send_multiple_ends_with_sequence_terminator(self):
        node = MockNode()
        c = AckMessageClient(node)
        msgs = [
            SendMessage("a", ["b"], "1"),
            SendMessage("a", ["c"], "2"),
        ]
        c.send_multiple(dest_addr=1, messages=msgs)
        updu = node.sent[0]["updu"]
        # Should end with extra terminator for sequence
        assert updu.endswith(b"\r\n.\r\n")

    def test_send_quit(self):
        node = MockNode()
        c = AckMessageClient(node)
        c.send_quit(dest_addr=1)
        assert node.sent[0]["updu"] == b"QUIT\r\n"

    def test_send_reset(self):
        node = MockNode()
        c = AckMessageClient(node)
        c.send_reset(dest_addr=1)
        assert node.sent[0]["updu"] == b"RSET\r\n"

    def test_send_uses_arq_mode(self):
        node = MockNode()
        c = AckMessageClient(node)
        c.send_message(dest_addr=1, from_user="a", to_users=["b"], body="x")
        assert node.sent[0]["mode"].arq_mode is True

    def test_receive_response_invokes_callback(self):
        node = MockNode()
        c = AckMessageClient(node)
        responses = []
        c.on_response = lambda code, line: responses.append((code, line))
        deliver(c, src_addr=1, data=b"250 OK\r\n")
        assert len(responses) == 1
        assert responses[0][0] == 250


# ===========================================================================
# AckMessageServer (SAP 13)
# ===========================================================================

class TestAckMessageServer:
    def test_send_from_returns_250(self):
        node = MockNode()
        s = AckMessageServer(node)
        deliver(s, src_addr=1, data=b"SEND FROM:<alice>\r\n")
        assert len(node.sent) == 1
        assert b"250" in node.sent[0]["updu"]

    def test_rcpt_to_all_accepted_by_default(self):
        node = MockNode()
        s = AckMessageServer(node)
        deliver(s, src_addr=1, data=b"SEND FROM:<alice>\r\nRCPT TO:<bob>\r\n")
        response = node.sent[0]["updu"]
        # Both SEND FROM and RCPT TO should get 250
        assert response.count(b"250") == 2

    def test_rcpt_to_unknown_user_returns_550(self):
        node = MockNode()
        s = AckMessageServer(node)
        s.set_known_users({"carol"})
        deliver(s, src_addr=1, data=b"SEND FROM:<alice>\r\nRCPT TO:<bob>\r\n")
        response = node.sent[0]["updu"]
        assert b"550" in response

    def test_data_and_body_delivers_message(self):
        node = MockNode()
        delivered = []
        s = AckMessageServer(node,
            mailbox_handler=lambda f, t, b: delivered.append((f, t, b)) or True)
        payload = (
            b"SEND FROM:<alice>\r\n"
            b"RCPT TO:<bob>\r\n"
            b"DATA\r\n"
            b"Hello World\r\n"
            b".\r\n"
        )
        deliver(s, src_addr=1, data=payload)
        assert len(delivered) == 1
        assert delivered[0][0] == "alice"
        assert delivered[0][1] == ["bob"]
        assert "Hello World" in delivered[0][2]

    def test_quit_returns_250(self):
        node = MockNode()
        s = AckMessageServer(node)
        deliver(s, src_addr=1, data=b"QUIT\r\n")
        assert b"250" in node.sent[0]["updu"]

    def test_rset_resets_state(self):
        node = MockNode()
        delivered = []
        s = AckMessageServer(node,
            mailbox_handler=lambda f, t, b: delivered.append((f, t, b)) or True)
        # Start a transaction then reset
        deliver(s, src_addr=1, data=b"SEND FROM:<alice>\r\nRSET\r\n")
        # Now start fresh and complete
        deliver(s, src_addr=1, data=(
            b"SEND FROM:<bob>\r\n"
            b"RCPT TO:<carol>\r\n"
            b"DATA\r\n"
            b"body\r\n"
            b".\r\n"
        ))
        assert len(delivered) == 1
        assert delivered[0][0] == "bob"

    def test_body_byte_unstuffing(self):
        node = MockNode()
        delivered = []
        s = AckMessageServer(node,
            mailbox_handler=lambda f, t, b: delivered.append((f, t, b)) or True)
        payload = (
            b"SEND FROM:<a>\r\n"
            b"RCPT TO:<b>\r\n"
            b"DATA\r\n"
            b"..leading dot\r\n"
            b".\r\n"
        )
        deliver(s, src_addr=1, data=payload)
        assert ".leading dot" in delivered[0][2]

    def test_extract_bracket(self):
        assert AckMessageServer._extract_bracket("<alice>") == "alice"
        assert AckMessageServer._extract_bracket("plain") == "plain"

    def test_full_roundtrip(self):
        """Client sends → capture updu → feed to server → message delivered."""
        client_node = MockNode()
        server_node = MockNode()
        client = AckMessageClient(client_node)
        delivered = []
        server = AckMessageServer(server_node,
            mailbox_handler=lambda f, t, b: delivered.append((f, t, b)) or True)
        client.send_message(dest_addr=1, from_user="alice",
                             to_users=["bob"], body="roundtrip test")
        # Feed client's output to server
        deliver(server, src_addr=1, data=client_node.sent[0]["updu"])
        assert len(delivered) == 1
        assert delivered[0][0] == "alice"
        assert "roundtrip test" in delivered[0][2]


# ===========================================================================
# OrderwireClient (SAP 5)
# ===========================================================================

class TestOrderwireClient:
    def test_send_acknowledged_arq(self):
        node = MockNode()
        c = OrderwireClient(node)
        c.send_acknowledged(dest_addr=1, text="hello")
        mode = node.sent[0]["mode"]
        assert mode.arq_mode is True
        assert mode.node_delivery_confirm is True

    def test_send_broadcast_non_arq(self):
        node = MockNode()
        c = OrderwireClient(node)
        c.send_broadcast(dest_addr=1, text="hello")
        assert node.sent[0]["mode"].arq_mode is False

    def test_msb_masking(self):
        node = MockNode()
        c = OrderwireClient(node)
        # Send text with chars that would have high bit set after encode
        c.send_acknowledged(dest_addr=1, text="test\x80")
        updu = node.sent[0]["updu"]
        for byte in updu:
            assert byte & 0x80 == 0, f"Byte 0x{byte:02X} has MSB set"

    def test_crlf_appended(self):
        node = MockNode()
        c = OrderwireClient(node)
        c.send_acknowledged(dest_addr=1, text="hello")
        assert node.sent[0]["updu"].endswith(b"\r\n")

    def test_receive_strips_crlf(self):
        node = MockNode()
        c = OrderwireClient(node)
        received = []
        c.on_message_received = lambda addr, text: received.append((addr, text))
        deliver(c, src_addr=42, data=b"hello\r\n")
        assert received == [(42, "hello")]

    def test_sap_id_is_5(self):
        assert OrderwireClient.SAP_ID == 5
