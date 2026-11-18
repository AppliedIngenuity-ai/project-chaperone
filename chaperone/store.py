"""The store: named values that are kept between runs, with their labels (specification 2.12 and rule P5).

Every stored name is declared in system.yaml with the value it has before anything is put to it. More than one run
may be in progress in one process; each operation is applied whole before the next begins. A second process on the
same file is not supported.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path as FilePath
from typing import Any, Mapping

from .values import Path, Problem, Value


class Store:
    def __init__(self, file: str | FilePath, declared: Mapping[str, Any]) -> None:
        """`declared` is the `stores:` section of system.yaml. A name kept in `file` that is no longer declared is
        refused, not dropped: Problem of type `store_not_declared`."""
        self.file = FilePath(file)
        self._lock = threading.Lock()
        self._values: dict[str, Value] = {name: Value.of(first) for name, first in declared.items()}
        if self.file.is_file():
            for name, kept in json.loads(self.file.read_text(encoding="utf-8")).items():
                if name not in declared:
                    raise Problem("store_not_declared", f"{self.file} keeps a value for {name}, which system.yaml "
                                                        f"does not declare under `stores`")
                self._values[name] = Value(kept["data"], {_path(p): frozenset(labels) for p, labels in kept["labels"]})

    # ---- the four engine operations --------------------------------------------------------------------------------
    def get(self, name: str) -> Value:
        with self._lock:
            return self._known(name)

    def put(self, name: str, value: Value) -> None:
        with self._lock:
            self._known(name)
            self._values[name] = value
            self._write()

    def add(self, name: str, items: Value) -> Value:
        """Adds to the stored list the items not already in it, by content. Returns `added`, the items added, and
        `present`, the items already in it, as stored. A stored item keeps the labels it was stored with."""
        with self._lock:
            kept = self._list(name)
            added, present = [], []
            for item in items.items():
                match = next((k for k in kept + added if k.data == item.data), None)
                (present if match is not None else added).append(match if match is not None else item)
            self._values[name] = Value.build(kept + added)
            self._write()
            return Value.build({"added": added, "present": present})

    def remove(self, name: str, items: Value) -> Value:
        """Removes the items present, by content. Returns `removed`, the items removed, as stored, and `absent`, the
        rest of what was passed."""
        with self._lock:
            kept = self._list(name)
            wanted = items.items()
            removed = [k for k in kept if any(k.data == w.data for w in wanted)]
            absent = [w for w in wanted if not any(k.data == w.data for k in kept)]
            self._values[name] = Value.build([k for k in kept if not any(k.data == w.data for w in wanted)])
            self._write()
            return Value.build({"removed": removed, "absent": absent})

    # ---- inside ----------------------------------------------------------------------------------------------------
    def _known(self, name: str) -> Value:
        if name not in self._values:
            raise Problem("store_not_declared", f"the store {name} is not declared in system.yaml under `stores`")
        return self._values[name]

    def _list(self, name: str) -> list[Value]:
        value = self._known(name)
        if not isinstance(value.data, list):
            raise Problem("not_a_list", f"the store {name} does not keep a list")
        return value.items()

    def _write(self) -> None:
        """The whole store, written beside the file and moved over it, so the file is never half written."""
        kept = {name: {"data": value.data, "labels": [[list(path), sorted(labels)]
                                                        for path, labels in sorted(value.labels.items(), key=str)]}
                for name, value in self._values.items()}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        beside = self.file.with_name(self.file.name + ".writing")
        beside.write_text(json.dumps(kept, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(beside, self.file)


def _path(written: list[Any]) -> Path:
    return tuple(written)
