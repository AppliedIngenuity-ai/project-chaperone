"""Mail from a folder of saved messages (.eml files), instead of a mailbox.

For trying a system on real messages without giving it any access to a mail
account: save or export a few messages into a folder, and point the reader at
the folder. The files are only read. They are served through the same read-only
mail reader as a real mailbox, so the same limits apply.

A message's "received" time is taken from the newest `Received:` header, which
the receiving mail system wrote. If there is none, the file is refused.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
from datetime import timezone
from pathlib import Path
from typing import Callable

from chaperone.connect.fake import FakeImap


class FolderProblem(OSError):
    """The folder is missing, or cannot be read as a mailbox."""
    kind = "folder"


def _received(raw: bytes, name: str) -> str:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    for header in msg.get_all("Received", []):
        stamp = str(header).rsplit(";", 1)[-1].strip()
        try:
            when = email.utils.parsedate_to_datetime(stamp)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc).strftime("%d-%b-%Y %H:%M:%S +0000")
    raise ValueError(f"{name}: no usable 'Received:' header, so it is not known when this message arrived")


def connect_to_folder(folder: Path | str) -> Callable[[], FakeImap]:
    """A `connect` function for MailReader that serves every .eml file in the folder. The id of a message is its file name."""
    folder = Path(folder)

    def connect() -> FakeImap:
        if not folder.is_dir():
            raise FolderProblem(f"there is no folder {folder}")
        messages = {}
        for path in sorted(folder.glob("*.eml")):
            raw = path.read_bytes()
            name = path.stem.replace(" ", "_")
            if name in messages:
                raise FolderProblem(f"{folder}: two files would be one message, {name}: rename one")
            messages[name] = (_received(raw, path.name), raw)
        return FakeImap(messages)

    return connect
