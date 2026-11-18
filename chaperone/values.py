"""Values and their labels (specification 2.1, 2.9 and section 3, rules P1 to P6).

A value is a scalar, a list or a dictionary. Labels are attached to its scalar parts; a list or a dictionary carries
a label if some scalar part within it does. A `Value` is the data and, for each scalar part, the labels it carries
with the value true. The data is never changed by a label, and a module is passed the data alone.

A path names a part: a tuple of field names and list indices, written `a.b[2]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Collection, Iterable, Iterator, Mapping

Path = tuple[str | int, ...]
SCALARS = (str, int, float, bool, type(None))
_STEP = re.compile(r"\.?([A-Za-z_]\w*)|\[(\d+)\]")


class Problem(Exception):
    """A value, a request or a store operation that the rules of the specification refuse. `type` is the error's
    type name; the workflow engine (step 3) turns a Problem into an error value."""

    def __init__(self, type: str, message: str) -> None:
        super().__init__(message)
        self.type = type


def parse_path(text: str) -> Path:
    """`a.b[2]` as ("a", "b", 2). The empty text is the whole value."""
    out: list[str | int] = []
    position = 0
    while position < len(text):
        step = _STEP.match(text, position)
        if step is None or (position == 0 and text.startswith(".")):
            raise Problem("no_such_part", f"`{text}` is not a path: a path is field names and list indices, a.b[2]")
        out.append(step.group(1) if step.group(1) is not None else int(step.group(2)))
        position = step.end()
    return tuple(out)


def show_path(path: Path) -> str:
    text = "".join(f"[{step}]" if isinstance(step, int) else f".{step}" for step in path)
    return text.lstrip(".") or "the whole value"


def scalar_paths(data: Any, prefix: Path = ()) -> Iterator[Path]:
    """The path of every scalar part of `data`. Data that is not a scalar, a list or a dictionary with text keys is
    refused: a value is what a configuration file could write."""
    if isinstance(data, dict):
        for key, inner in data.items():
            if not isinstance(key, str):
                raise Problem("not_a_value", f"a dictionary key is {key!r}; a key is a text")
            yield from scalar_paths(inner, prefix + (key,))
    elif isinstance(data, list):
        for index, inner in enumerate(data):
            yield from scalar_paths(inner, prefix + (index,))
    elif isinstance(data, SCALARS):
        yield prefix
    else:
        raise Problem("not_a_value", f"{type(data).__name__} at {show_path(prefix)}: a value is a scalar, a list or a "
                                     f"dictionary")


def _at(data: Any, path: Path) -> Any:
    """The part of `data` at `path`; LookupError when there is none."""
    for step in path:
        if isinstance(step, str) and isinstance(data, dict) and step in data:
            data = data[step]
        elif isinstance(step, int) and not isinstance(step, bool) and isinstance(data, list) and 0 <= step < len(data):
            data = data[step]
        else:
            raise LookupError(step)
    return data


@dataclass(frozen=True)
class Value:
    data: Any
    labels: Mapping[Path, frozenset[str]] = field(default_factory=dict)    # scalar part -> labels; absent: none

    # ---- making values ---------------------------------------------------------------------------------------------
    @classmethod
    def of(cls, data: Any, labels: Iterable[str] = ()) -> "Value":
        """`data` with each of `labels` on every scalar part. With no labels: a value that carries none, as a value
        written in a workflow file does."""
        wanted = frozenset(labels)
        paths = list(scalar_paths(data))
        return cls(data, {path: wanted for path in paths} if wanted else {})

    @classmethod
    def build(cls, template: Any) -> "Value":
        """One value from a structure whose parts may be Values: a referenced part keeps its labels, and a part
        written out carries none (2.12)."""
        labels: dict[Path, frozenset[str]] = {}

        def place(node: Any, prefix: Path) -> Any:
            if isinstance(node, Value):
                for path, carried in node.labels.items():
                    labels[prefix + path] = carried
                return node.data
            if isinstance(node, dict):
                return {key: place(inner, prefix + (key,)) for key, inner in node.items()}
            if isinstance(node, list):
                return [place(inner, prefix + (index,)) for index, inner in enumerate(node)]
            return node
        data = place(template, ())
        list(scalar_paths(data))                                 # refuses what is not a value
        return cls(data, labels)

    # ---- reading ---------------------------------------------------------------------------------------------------
    def carried(self, path: Path = ()) -> frozenset[str]:
        """The labels the part at `path` carries: its own if it is a scalar, else those of the scalar parts within."""
        out: set[str] = set()
        for part, labels in self.labels.items():
            if part[:len(path)] == path:
                out |= labels
        return frozenset(out)

    def has_part(self, path: Path) -> bool:
        try:
            _at(self.data, path)
        except LookupError:
            return False
        return True

    def get(self, path: Path) -> "Value":
        """The part at `path`, with its labels (the engine operation `get`)."""
        try:
            data = _at(self.data, path)
        except LookupError:
            raise Problem("no_such_part", f"the value has no part {show_path(path)}") from None
        return Value(data, {part[len(path):]: labels for part, labels in self.labels.items()
                            if part[:len(path)] == path})

    def pick(self, fields: Iterable[str]) -> "Value":
        """The projection onto the named fields (the engine operation `pick`)."""
        if not isinstance(self.data, dict):
            raise Problem("no_such_part", "pick is applied to a dictionary")
        return Value.build({name: self.get((name,)) for name in fields})

    def items(self) -> list["Value"]:
        if not isinstance(self.data, list):
            raise Problem("not_a_list", "a list is expected")
        return [self.get((index,)) for index in range(len(self.data))]

    def marks(self) -> "Value":
        """The labels of each part, as a value with the same paths that carries no label (`labels_of`). A scalar part
        is replaced by the sorted names of its labels."""
        def swap(node: Any, prefix: Path) -> Any:
            if isinstance(node, dict):
                return {key: swap(inner, prefix + (key,)) for key, inner in node.items()}
            if isinstance(node, list):
                return [swap(inner, prefix + (index,)) for index, inner in enumerate(node)]
            return sorted(self.labels.get(prefix, ()))
        return Value(swap(self.data, ()))

    # ---- changing labels -------------------------------------------------------------------------------------------
    def set_label(self, path: Path, label: str, on: bool) -> "Value":
        """`label` attached to, or removed from, the part at `path` and every scalar part within it."""
        if not self.has_part(path):
            raise Problem("no_such_part", f"the value has no part {show_path(path)}")
        labels = dict(self.labels)
        for part in scalar_paths(_at(self.data, path), path):
            now = set(labels.get(part, ()))
            now.add(label) if on else now.discard(label)
            if now:
                labels[part] = frozenset(now)
            else:
                labels.pop(part, None)
        return Value(self.data, labels)


def spreading_of(arguments: Iterable[Value], spreading: Collection[str]) -> frozenset[str]:
    """Each label with `spreads: true` that any part of any argument carries (P2)."""
    out: set[str] = set()
    for argument in arguments:
        out |= argument.carried()
    return frozenset(out & set(spreading))


def result(raw: Any, adds: Iterable[str], arguments: Iterable[Value], spreading: Collection[str]) -> Value:
    """A module's result with its labels by P1 and P2: every scalar part carries each label of the module's `adds`
    and each spreading label carried by any argument of the call."""
    return Value.of(raw, frozenset(adds) | spreading_of(arguments, spreading))


Request = tuple[str, str, bool]                                 # (path as written, label, value)


def apply_requests(value: Value, requests: Iterable[Request], whole: Mapping[str, bool],
                   by_part: Mapping[str, Mapping[str, bool]]) -> Value:
    """P4. Each request (path, label, value) is applied to that part and every part within it, if the module's
    declaration states it: `whole` holds the (label, value) pairs declared for any part of the result, `by_part`
    those declared for one named top field. A request the declaration does not state, or whose path names no part,
    refuses the call: Problem of type `bad_request`, and no label is changed."""
    out = value
    for written, label, wanted in requests:
        path = parse_path(written)
        top = path[0] if path and isinstance(path[0], str) else None
        declared = whole.get(label) is wanted or (top is not None and by_part.get(top, {}).get(label) is wanted)
        if not declared:
            raise Problem("bad_request", f"the request ({show_path(path)}, {label}, {'true' if wanted else 'false'}) "
                                         f"is not stated in the module's declaration")
        if not value.has_part(path):
            raise Problem("bad_request", f"the request ({show_path(path)}, {label}, {'true' if wanted else 'false'}) "
                                         f"names no part of the result")
        out = out.set_label(path, label, wanted)
    return out


def error(type: str, message: str, *, state: str, call: str, adds: Iterable[str], arguments: Iterable[Value],
          spreading: Collection[str], detail: Any = None) -> Value:
    """P6. An error is a value with the fields of 2.12. It carries the module's `adds` and every spreading label
    carried by any argument of the call; no request applies to it."""
    data = {"type": type, "message": message, "state": state, "call": call}
    if detail is not None:
        data["detail"] = detail
    return result(data, adds, arguments, spreading)


def equals(a: Value, b: Value, spreading: Collection[str]) -> Value:
    """The engine operation `equals`: a new value, which carries each spreading label carried by either operand (P3)."""
    return Value.of(a.data == b.data, spreading_of((a, b), spreading))
