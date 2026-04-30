"""Tests for src/annex_f/text_protocol.py — CRLF parser, command parsing, formatters."""

from src.annex_f.text_protocol import (
    TextProtocolParser,
    parse_command,
    format_response,
    format_pop3_ok,
    format_pop3_err,
    byte_stuff,
    encode_multiline_data,
)


# ---------------------------------------------------------------------------
# TextProtocolParser.feed
# ---------------------------------------------------------------------------

class TestTextProtocolParserFeed:
    def test_single_complete_line(self):
        p = TextProtocolParser()
        assert p.feed(b"HELLO\r\n") == ["HELLO"]

    def test_multiple_lines_one_chunk(self):
        p = TextProtocolParser()
        assert p.feed(b"A\r\nB\r\n") == ["A", "B"]

    def test_partial_then_complete(self):
        p = TextProtocolParser()
        assert p.feed(b"HEL") == []
        assert p.feed(b"LO\r\n") == ["HELLO"]

    def test_no_terminator_returns_empty(self):
        p = TextProtocolParser()
        assert p.feed(b"HELLO") == []

    def test_empty_line(self):
        p = TextProtocolParser()
        assert p.feed(b"\r\n") == [""]

    def test_reset_clears_buffer(self):
        p = TextProtocolParser()
        p.feed(b"partial")
        p.reset()
        assert p.feed(b"NEW\r\n") == ["NEW"]


# ---------------------------------------------------------------------------
# TextProtocolParser.feed_multiline
# ---------------------------------------------------------------------------

class TestTextProtocolParserMultiline:
    def test_complete_with_dot_terminator(self):
        p = TextProtocolParser()
        lines, complete = p.feed_multiline(b"line1\r\n.\r\n")
        assert lines == ["line1"]
        assert complete is True

    def test_incomplete_without_terminator(self):
        p = TextProtocolParser()
        lines, complete = p.feed_multiline(b"line1\r\n")
        assert lines == ["line1"]
        assert complete is False

    def test_byte_unstuffing(self):
        p = TextProtocolParser()
        lines, complete = p.feed_multiline(b"..test\r\n.\r\n")
        assert lines == [".test"]
        assert complete is True

    def test_multiple_lines_then_terminator(self):
        p = TextProtocolParser()
        lines, complete = p.feed_multiline(b"A\r\nB\r\n.\r\n")
        assert lines == ["A", "B"]
        assert complete is True


# ---------------------------------------------------------------------------
# parse_command
# ---------------------------------------------------------------------------

class TestParseCommand:
    def test_numeric_response(self):
        r = parse_command("250 OK")
        assert r.code == 250
        assert r.keyword == "250"
        assert r.args == "OK"

    def test_pop3_ok(self):
        r = parse_command("+OK ready")
        assert r.keyword == "+OK"
        assert r.args == "ready"

    def test_pop3_err(self):
        r = parse_command("-ERR fail")
        assert r.keyword == "-ERR"
        assert r.args == "fail"

    def test_plain_command(self):
        r = parse_command("EHLO myhost")
        assert r.code == 0
        assert r.keyword == "EHLO"
        assert r.args == "myhost"

    def test_empty_string(self):
        r = parse_command("")
        assert r.code == 0
        assert r.keyword == ""

    def test_command_no_args(self):
        r = parse_command("QUIT")
        assert r.keyword == "QUIT"
        assert r.args == ""


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_format_response(self):
        assert format_response(250, "OK") == b"250 OK\r\n"

    def test_format_pop3_ok(self):
        assert format_pop3_ok("ready") == b"+OK ready\r\n"

    def test_format_pop3_err(self):
        assert format_pop3_err("fail") == b"-ERR fail\r\n"


# ---------------------------------------------------------------------------
# byte_stuff / encode_multiline_data
# ---------------------------------------------------------------------------

class TestByteStuff:
    def test_dot_at_start_gets_doubled(self):
        assert byte_stuff(".test") == "..test"

    def test_no_dot_unchanged(self):
        assert byte_stuff("test") == "test"

    def test_multiple_lines_with_dots(self):
        result = byte_stuff("normal\r\n.starts-with-dot\r\nok")
        assert result == "normal\r\n..starts-with-dot\r\nok"

    def test_encode_multiline_data_adds_terminator(self):
        result = encode_multiline_data("body text")
        assert result.endswith(b"\r\n.\r\n")

    def test_encode_multiline_data_stuffs_dots(self):
        result = encode_multiline_data(".leading dot")
        assert b"..leading dot" in result
