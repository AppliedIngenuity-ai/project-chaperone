"""Mail as it really arrives: each shape is read as far as it can be, nothing crashes, and text no reader sees is not read.
Made-up mail only: a framework that fails on a simple message reads as "not ready"."""

import base64
import quopri

import pytest

from chaperone.connect.mail_reader import _to_record

HEAD = b'1 (INTERNALDATE "01-Nov-2026 10:00:00 +0000" BODY[] {10}'
TOP = b"From: a@b.example\r\nSubject: s\r\nDate: Sun, 1 Nov 2026 10:00:00 +0000\r\nMIME-Version: 1.0\r\n"


def html(markup: bytes) -> bytes:
    return TOP + b"Content-Type: text/html; charset=utf-8\r\n\r\n" + markup + b"\r\n"


def body_of(raw: bytes) -> str:
    return _to_record("1", HEAD, raw)["body"]


@pytest.mark.parametrize("raw, reads", [
    (TOP + b"Content-Type: text/plain; charset=utf-8\r\n\r\nhello\r\n", "hello"),
    (TOP + b"Content-Type: text/plain; charset=nosuchcharset\r\n\r\nhello\r\n", "hello"),
    (TOP + b"Content-Type: text/plain; charset=windows-1252\r\n\r\ncaf\xe9\r\n", "café"),
    (TOP + b"Content-Type: text/plain; charset=iso-2022-jp\r\n\r\n" + "配達".encode("iso-2022-jp") + b"\r\n", "配達"),
    (TOP + b"Content-Type: text/plain; charset=utf-8\r\n\r\ncaf\xe9 hello\r\n", "hello"),                 # bytes wrong for the charset
    (b"From: a@b.example\r\nSubject: s\r\n\r\nhello\r\n", "hello"),                                       # no content type
    (b"From: =?nosuch?B?aGVsbG8=?= <a@b.example>\r\nSubject: =?nosuch?B?aGVsbG8=?=\r\n\r\nhello\r\n", "hello"),
    (TOP + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(b"hello") + b"\r\n", "hello"),
    (TOP + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n" + quopri.encodestring(b"hello=") + b"\r\n", "hello="),
    (TOP + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: x-foo\r\n\r\nhello\r\n", "hello"),
    (TOP + b"Content-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nplain hello\r\n--b\r\n"
     b"Content-Type: text/html\r\n\r\n<p>html hello</p>\r\n--b--\r\n", "plain hello"),
    (TOP + b"Content-Type: multipart/mixed; boundary=a\r\n\r\n--a\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\n"
     b"Content-Type: text/html\r\n\r\n<p>inner hello</p>\r\n--b--\r\n--a\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment\r\n\r\nx\r\n--a--\r\n", "inner hello"),
    (html(b"<HTML><BODY><P>hello</P></BODY></HTML>"), "hello"),
    (html(b"<p>caf&eacute; &amp; hello&nbsp;there</p>"), "café & hello there"),
    (html(b"<table><tr><td>Tracking</td><td>1Z999</td></tr></table>"), "Tracking 1Z999"),
    (html(b"<p>no closing tags<div>hello"), "hello"),
    (html(b'<p>before</p><img width=1 height=1 src="x"><p>hello</p>'), "hello"),
    (html(b'<p>before</p><img style="display:none" src="x"><p>hello</p>'), "hello"),                      # a tracking pixel
    (html(b'<p>before</p><img style="DISPLAY : NONE" src="x"><p>hello</p>'), "hello"),
    (html(b'<p>before</p><br style="display:none"><p>hello</p>'), "hello"),
    (html(b"<!-- a comment --><p>hello</p>"), "hello"),
    (html(b"https://only.a.link/x"), "https://only.a.link/x"),
])
def test_each_shape_is_read(raw, reads):
    assert reads in body_of(raw)


@pytest.mark.parametrize("raw", [TOP + b"\r\n", b"Subject: s\r\n\r\nhello\r\n",
                                 TOP + b"Content-Type: multipart/mixed; boundary=b\r\n\r\nno parts at all\r\n",
                                 TOP + b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: application/pdf\r\n\r\nx\r\n--b--\r\n",
                                 TOP + b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n@@@not base64@@@\r\n"])
def test_a_message_with_nothing_readable_is_no_crash(raw):
    assert isinstance(body_of(raw), str)


@pytest.mark.parametrize("markup", [
    b'<p style="display:none">secret</p>', b'<p style="display: none;">secret</p>', b'<p style="visibility:hidden">secret</p>',
    b'<p style="font-size:0">secret</p>', b'<p style="font-size: 0px">secret</p>', b"<p hidden>secret</p>",
    b"<script>secret</script>", b"<style>secret</style>", b'<div style="display:none"><p><b>secret</b></p></div>',
])
def test_text_no_reader_sees_is_not_read_and_the_rest_is(markup):
    body = body_of(html(b"<p>before</p>" + markup + b"<p>after</p>"))
    assert "secret" not in body and "before" in body and "after" in body


def test_a_size_that_only_starts_with_nought_is_seen():
    assert "seen" in body_of(html(b'<p style="font-size:0.9em">seen</p>'))


def test_a_link_hidden_from_a_reader_is_not_read_and_a_seen_one_is():
    record = _to_record("1", HEAD, html(b'<a href="https://seen.example/1">go</a><div hidden><a href="https://hidden.example/2">x</a></div>'))
    assert record["links"] == ["https://seen.example/1"]
