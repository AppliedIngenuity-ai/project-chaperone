"""A stand-in mail server, for tests and for running a system on made-up mail.

It answers only the commands a read-only reader needs. Any other command raises,
so a test fails loudly if a client ever tries to change something.
"""

from __future__ import annotations

import email
import email.policy
from datetime import datetime
from typing import Any


def raw_message(sender: str, subject: str, body: str, *, authenticated: bool = True, html: bool = False,
                to: str = "me@example.test", reply_to: str | None = None) -> bytes:
    kind = "text/html" if html else "text/plain"
    auth = "dmarc=pass" if authenticated else "dkim=fail; dmarc=fail"
    reply = (f"Reply-To: {reply_to}\r\n" if "<" in reply_to else f"Reply-To: <{reply_to}>\r\n") if reply_to else ""
    return (f"From: Sender <{sender}>\r\nTo: Me <{to}>\r\n{reply}Subject: {subject}\r\nAuthentication-Results: mx.example; {auth}\r\n"
            f"Message-ID: <claimed-by-sender@x>\r\nContent-Type: {kind}; charset=utf-8\r\n\r\n{body}\r\n").encode()


class FakeImap:
    """`messages` maps a server id to (when the server received it, the raw message)."""

    def __init__(self, messages: dict[str, tuple[str, bytes]], ignores_since: bool = False) -> None:
        self.messages = messages
        self.commands: list[tuple] = []
        self.ignores_since = ignores_since                # a server or bridge that returns more than it was asked for

    def select(self, mailbox: str, readonly: bool = False) -> tuple:
        self.commands.append(("select", mailbox, readonly))
        if not readonly:
            raise AssertionError("the mailbox was opened for writing")
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple:
        self.commands.append((command,) + args)
        if command == "SEARCH":
            tokens = [a for a in args if a is not None]                  # the first argument is the charset
            hits = list(self.messages) if self.ignores_since else [u for u in self.messages if _matches(self.messages[u], tokens)]
            return "OK", [b" ".join(u.encode() for u in hits)]
        if command == "FETCH":
            date, body = self.messages[args[0].decode()]
            return "OK", [(f'1 (UID {args[0].decode()} INTERNALDATE "{date}" BODY[] {{{len(body)}}}'.encode(), body), b")"]
        raise AssertionError(f"a command was sent that a reader must never send: {command}")

    def logout(self) -> None:
        self.commands.append(("logout",))

    def __getattr__(self, name: str) -> Any:              # store, copy, expunge, append, delete ...
        self.__dict__["commands"].append((name,))         # seen, whatever the run does with the refusal
        raise AssertionError(f"'{name}' was used, which a reader must never do")


def _matches(entry: tuple[str, bytes], tokens: list[str]) -> bool:
    """Evaluate IMAP search keys the way a server would: SINCE, BEFORE, FROM, SUBJECT, and OR of two keys.
    A sequence of keys must all hold. Anything else is a key a reader must never send."""
    when, raw = entry
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        received = datetime.strptime(when, "%d-%b-%Y %H:%M:%S %z").date()
    except ValueError:
        received = None

    def key(i: int) -> tuple[bool, int]:
        name = str(tokens[i]).upper()
        if name == "OR":
            a, i = key(i + 1)
            b, i = key(i)
            return a or b, i
        if name in ("SINCE", "BEFORE"):
            day = datetime.strptime(str(tokens[i + 1]), "%d-%b-%Y").date()
            if received is None:
                return True, i + 2                                        # let the reader's own check decide
            return (received >= day if name == "SINCE" else received < day), i + 2
        if name in ("FROM", "SUBJECT"):
            term = str(tokens[i + 1]).strip('"').lower()
            hay = str(msg.get("From" if name == "FROM" else "Subject", "")).lower()
            return term in hay, i + 2
        raise AssertionError(f"a search key was sent that a reader must never send: {tokens[i]}")

    i, ok = 0, True
    while i < len(tokens):
        result, i = key(i)
        ok = ok and result
    return ok
