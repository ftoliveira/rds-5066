"""Tests for src/annex_f/hmtp.py — HMTP client and server."""

from src.annex_f.hmtp import HMTPClient, HMTPServer, MailMessage

from tests.annex_f_helpers import MockNode, deliver


# ===========================================================================
# HMTPClient (SAP 3)
# ===========================================================================

class TestHMTPClient:
    def test_ehlo_format(self):
        node = MockNode()
        c = HMTPClient(node)
        c.ehlo(dest_addr=1, hostname="myhost")
        assert node.sent[0]["updu"] == b"EHLO myhost\r\n"

    def test_ehlo_delivery_mode(self):
        node = MockNode()
        c = HMTPClient(node)
        c.ehlo(dest_addr=1, hostname="h")
        mode = node.sent[0]["mode"]
        assert mode.arq_mode is True
        assert mode.node_delivery_confirm is True

    def test_send_batch_single_message(self):
        node = MockNode()
        c = HMTPClient(node)
        msgs = [MailMessage("alice@a.com", ["bob@b.com"], "Hello")]
        c.send_batch(dest_addr=1, hostname="myhost", messages=msgs)
        updu = node.sent[0]["updu"]
        assert updu.startswith(b"EHLO myhost\r\n")
        assert b"MAIL MULTIPLE" not in updu  # single message
        assert b"MAIL FROM:<alice@a.com>\r\n" in updu
        assert b"RCPT TO:<bob@b.com>\r\n" in updu
        assert b"DATA\r\n" in updu
        assert b"Hello" in updu
        assert updu.endswith(b"QUIT\r\n")

    def test_send_batch_multiple_messages(self):
        node = MockNode()
        c = HMTPClient(node)
        msgs = [
            MailMessage("alice@a.com", ["bob@b.com"], "msg1"),
            MailMessage("carol@c.com", ["dave@d.com"], "msg2"),
        ]
        c.send_batch(dest_addr=1, hostname="h", messages=msgs)
        updu = node.sent[0]["updu"]
        assert b"MAIL MULTIPLE\r\n" in updu
        assert b"MAIL FROM:<alice@a.com>\r\n" in updu
        assert b"MAIL FROM:<carol@c.com>\r\n" in updu
        assert updu.endswith(b"QUIT\r\n")

    def test_send_batch_ends_with_quit(self):
        node = MockNode()
        c = HMTPClient(node)
        c.send_batch(dest_addr=1, hostname="h",
                      messages=[MailMessage("a", ["b"], "x")])
        assert node.sent[0]["updu"].endswith(b"QUIT\r\n")

    def test_sap_id_is_3(self):
        assert HMTPClient.SAP_ID == 3

    def test_receive_response_callback(self):
        node = MockNode()
        c = HMTPClient(node)
        received = []
        c.on_response = lambda resps: received.extend(resps)
        deliver(c, src_addr=1, data=b"250 Hello\r\n250 OK\r\n")
        assert len(received) == 2
        assert received[0].code == 250
        assert received[1].code == 250


# ===========================================================================
# HMTPServer (SAP 3)
# ===========================================================================

class TestHMTPServer:
    def test_ehlo_response_capabilities(self):
        node = MockNode()
        s = HMTPServer(node)
        deliver(s, src_addr=1, data=b"EHLO myhost\r\n")
        response = node.sent[0]["updu"]
        assert b"250-PIPELINING\r\n" in response
        assert b"250 8BITMIME\r\n" in response

    def test_helo_also_accepted(self):
        node = MockNode()
        s = HMTPServer(node)
        deliver(s, src_addr=1, data=b"HELO myhost\r\n")
        response = node.sent[0]["updu"]
        assert b"250" in response
        assert b"PIPELINING" in response

    def test_mail_from_250(self):
        node = MockNode()
        s = HMTPServer(node)
        deliver(s, src_addr=1,
                data=b"EHLO h\r\nMAIL FROM:<alice@a.com>\r\n")
        response = node.sent[0]["updu"]
        assert b"alice@a.com" in response

    def test_rcpt_to_known_domain(self):
        node = MockNode()
        s = HMTPServer(node)
        s.set_known_domains({"b.com"})
        deliver(s, src_addr=1, data=(
            b"EHLO h\r\n"
            b"MAIL FROM:<a@a.com>\r\n"
            b"RCPT TO:<bob@b.com>\r\n"
        ))
        response = node.sent[0]["updu"]
        assert b"bob@b.com" in response
        assert b"550" not in response

    def test_rcpt_to_unknown_domain_550(self):
        node = MockNode()
        s = HMTPServer(node)
        s.set_known_domains({"b.com"})
        deliver(s, src_addr=1, data=(
            b"EHLO h\r\n"
            b"MAIL FROM:<a@a.com>\r\n"
            b"RCPT TO:<bob@unknown.com>\r\n"
        ))
        response = node.sent[0]["updu"]
        assert b"550" in response

    def test_data_body_delivery(self):
        node = MockNode()
        s = HMTPServer(node)
        delivered = []
        s.on_mail_received = lambda msg: delivered.append(msg)
        deliver(s, src_addr=1, data=(
            b"EHLO h\r\n"
            b"MAIL FROM:<alice@a.com>\r\n"
            b"RCPT TO:<bob@b.com>\r\n"
            b"DATA\r\n"
            b"Hello World\r\n"
            b".\r\n"
        ))
        assert len(delivered) == 1
        assert delivered[0].sender == "alice@a.com"
        assert delivered[0].recipients == ["bob@b.com"]
        assert "Hello World" in delivered[0].body

    def test_quit_resets_to_idle(self):
        node = MockNode()
        s = HMTPServer(node)
        deliver(s, src_addr=1, data=b"EHLO h\r\nQUIT\r\n")
        response = node.sent[0]["updu"]
        assert b"250" in response

    def test_server_response_uses_node_delivery_confirm(self):
        node = MockNode()
        s = HMTPServer(node)
        deliver(s, src_addr=1, data=b"EHLO h\r\n")
        assert node.sent[0]["mode"].node_delivery_confirm is True

    def test_full_roundtrip(self):
        """Client send_batch → capture updu → feed to server → messages delivered."""
        client_node = MockNode()
        server_node = MockNode()
        client = HMTPClient(client_node)
        delivered = []
        server = HMTPServer(server_node)
        server.on_mail_received = lambda msg: delivered.append(msg)

        msgs = [
            MailMessage("alice@a.com", ["bob@b.com"], "First mail"),
        ]
        client.send_batch(dest_addr=1, hostname="myhost", messages=msgs)

        # Feed client output to server
        deliver(server, src_addr=1, data=client_node.sent[0]["updu"])
        assert len(delivered) == 1
        assert delivered[0].sender == "alice@a.com"
        assert "First mail" in delivered[0].body

    def test_full_roundtrip_multiple_messages(self):
        """Client sends batch of 2 messages, server delivers both."""
        client_node = MockNode()
        server_node = MockNode()
        client = HMTPClient(client_node)
        delivered = []
        server = HMTPServer(server_node)
        server.on_mail_received = lambda msg: delivered.append(msg)

        msgs = [
            MailMessage("alice@a.com", ["bob@b.com"], "msg1"),
            MailMessage("carol@c.com", ["dave@d.com"], "msg2"),
        ]
        client.send_batch(dest_addr=1, hostname="h", messages=msgs)
        deliver(server, src_addr=1, data=client_node.sent[0]["updu"])
        assert len(delivered) == 2
        assert delivered[0].sender == "alice@a.com"
        assert delivered[1].sender == "carol@c.com"
