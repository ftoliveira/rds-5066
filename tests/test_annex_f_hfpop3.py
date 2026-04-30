"""Tests for src/annex_f/hf_pop3.py — HF-POP3 client and server."""

import hashlib

from src.annex_f.hf_pop3 import (
    HFPOP3Client,
    HFPOP3Server,
    POP3State,
    StoredMessage,
)

from tests.annex_f_helpers import MockNode, deliver


# ===========================================================================
# HFPOP3Client (SAP 4)
# ===========================================================================

class TestHFPOP3Client:
    def test_apop_sends_md5_digest(self):
        node = MockNode()
        c = HFPOP3Client(node)
        ts = "<123.456@hfpop3>"
        secret = "mysecret"
        c.apop(dest_addr=1, name="user", shared_secret=secret, timestamp=ts)
        expected_digest = hashlib.md5(f"{ts}{secret}".encode()).hexdigest()
        updu = node.sent[0]["updu"]
        assert expected_digest.encode() in updu
        assert b"APOP user " in updu

    def test_apop_uses_stored_timestamp(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c._server_timestamp = "<stored@ts>"
        c.apop(dest_addr=1, name="u", shared_secret="s")
        expected = hashlib.md5(b"<stored@ts>s").hexdigest()
        assert expected.encode() in node.sent[0]["updu"]

    def test_list_all(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c.list_messages(dest_addr=1)
        assert node.sent[0]["updu"] == b"LIST\r\n"

    def test_list_single(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c.list_messages(dest_addr=1, msg_number=3)
        assert node.sent[0]["updu"] == b"LIST 3\r\n"

    def test_retrieve(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c.retrieve(dest_addr=1, msg_number=2)
        assert node.sent[0]["updu"] == b"RETR 2\r\n"

    def test_delete(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c.delete(dest_addr=1, msg_number=5)
        assert node.sent[0]["updu"] == b"DELE 5\r\n"

    def test_quit(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c.quit(dest_addr=1)
        assert node.sent[0]["updu"] == b"QUIT\r\n"

    def test_all_use_node_delivery_confirm(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c.apop(dest_addr=1, name="u", shared_secret="s", timestamp="<t>")
        c.list_messages(dest_addr=1)
        c.retrieve(dest_addr=1)
        c.delete(dest_addr=1, msg_number=1)
        c.quit(dest_addr=1)
        for s in node.sent:
            assert s["mode"].node_delivery_confirm is True

    def test_connect_sends_noop(self):
        node = MockNode()
        c = HFPOP3Client(node)
        c.connect(dest_addr=1)
        assert node.sent[0]["updu"] == b"NOOP\r\n"
        assert node.sent[0]["mode"].node_delivery_confirm is True

    def test_sap_id_is_4(self):
        assert HFPOP3Client.SAP_ID == 4


# ===========================================================================
# HFPOP3Server (SAP 4)
# ===========================================================================

class TestHFPOP3Server:
    def _make_server(self, messages=None, secrets=None):
        node = MockNode()
        maildrop = {}
        if messages:
            maildrop["testuser"] = [StoredMessage(body=m) for m in messages]
        secrets = secrets or {"testuser": "secret123"}
        return node, HFPOP3Server(
            node, maildrop=maildrop, shared_secrets=secrets
        )

    def test_greeting_format(self):
        _, s = self._make_server()
        greeting = s.get_greeting()
        assert greeting.startswith(b"+OK POP3 server ready <")
        assert b"@hfpop3>" in greeting

    def test_apop_valid_auth(self):
        node, s = self._make_server(messages=["Hello", "World"])
        ts = s._timestamp
        digest = hashlib.md5(f"{ts}secret123".encode()).hexdigest()
        deliver(s, src_addr=1, data=f"APOP testuser {digest}\r\n".encode())
        assert s._state == POP3State.TRANSACTION
        response = node.sent[0]["updu"]
        assert b"+OK" in response
        assert b"maildrop" in response

    def test_apop_invalid_digest(self):
        node, s = self._make_server()
        deliver(s, src_addr=1, data=b"APOP testuser wrongdigest\r\n")
        response = node.sent[0]["updu"]
        assert b"-ERR" in response
        assert b"permission denied" in response

    def test_apop_unknown_user(self):
        node, s = self._make_server()
        deliver(s, src_addr=1, data=b"APOP unknown abc123\r\n")
        response = node.sent[0]["updu"]
        assert b"-ERR" in response

    def _auth_server(self, messages=None):
        """Create server and authenticate."""
        node, s = self._make_server(messages=messages or ["msg1", "msg2"])
        ts = s._timestamp
        digest = hashlib.md5(f"{ts}secret123".encode()).hexdigest()
        deliver(s, src_addr=1, data=f"APOP testuser {digest}\r\n".encode())
        node.sent.clear()  # Clear auth response
        return node, s

    def test_list_all_returns_listing(self):
        node, s = self._auth_server(["msg1", "msg2"])
        deliver(s, src_addr=1, data=b"LIST\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK" in response
        assert b"2 messages" in response

    def test_list_single(self):
        node, s = self._auth_server(["msg1"])
        deliver(s, src_addr=1, data=b"LIST 1\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK" in response

    def test_retr_single(self):
        node, s = self._auth_server(["Hello World"])
        deliver(s, src_addr=1, data=b"RETR 1\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK" in response
        assert b"Hello World" in response

    def test_dele_marks_deleted(self):
        node, s = self._auth_server(["msg1"])
        deliver(s, src_addr=1, data=b"DELE 1\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK" in response
        assert b"deleted" in response

    def test_dele_already_deleted(self):
        node, s = self._auth_server(["msg1"])
        deliver(s, src_addr=1, data=b"DELE 1\r\n")
        node.sent.clear()
        deliver(s, src_addr=1, data=b"DELE 1\r\n")
        response = node.sent[0]["updu"]
        assert b"-ERR" in response
        assert b"already deleted" in response

    def test_quit_applies_deletions(self):
        node, s = self._auth_server(["keep", "delete_me"])
        deliver(s, src_addr=1, data=b"DELE 2\r\n")
        node.sent.clear()
        deliver(s, src_addr=1, data=b"QUIT\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK" in response
        # Maildrop should have 1 message left
        assert len(s._maildrop["testuser"]) == 1

    def test_command_in_wrong_state(self):
        node, s = self._make_server()
        # LIST before APOP should fail
        deliver(s, src_addr=1, data=b"LIST\r\n")
        response = node.sent[0]["updu"]
        assert b"-ERR" in response

    def test_noop_in_auth_returns_greeting(self):
        node, s = self._make_server()
        deliver(s, src_addr=1, data=b"NOOP\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK POP3 server ready <" in response
        assert b"@hfpop3>" in response

    def test_noop_in_transaction_returns_ok(self):
        node, s = self._auth_server(["msg1"])
        deliver(s, src_addr=1, data=b"NOOP\r\n")
        response = node.sent[0]["updu"]
        assert b"+OK" in response

    def test_server_uses_node_delivery_confirm(self):
        node, s = self._make_server()
        deliver(s, src_addr=1, data=b"APOP testuser wrong\r\n")
        assert node.sent[0]["mode"].node_delivery_confirm is True
