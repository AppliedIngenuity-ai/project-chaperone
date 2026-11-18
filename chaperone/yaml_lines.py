"""The one loader of configuration files: every value is read as it is written, and every place is kept.

How a value is read:

- `true` and `false` (also `True`, `TRUE`, `False`, `FALSE`) are true and false.
- A number is written as JSON writes it: `12`, `-3`, `1.5`, `5e-3`.
- `null`, `~` and nothing are null.
- Anything else is text. A quoted value, and a block of text (`|`, `>`), are always text.

What is refused, at its line:

- A value that plain YAML would read as another type: `yes`, `no`, `on`, `off` (true or false to plain YAML); `012`,
  `.5`, `1:30`, `0x1F`, `+1`, `1_000`, `.inf`, `.nan` (numbers); `2026-10-29` (a date). The file says which it means.
- A number too large to be one (`1e400`).
- An explicit tag (`!!int 3`): a value's type is how it is written.
- A key given two different values, keys merged with `<<` included; the same value given twice loads.
- Two keys that are one key to a Python mapping (`1`, `1.0` and `true`).
- A value that contains itself, through an alias.

Every mapping is a `LineDict` and every list a `LineList`: plain dictionaries and lists that also keep `line`, where
they begin, and `key_lines` or `item_lines`, where each key or item is written. Lines count from 1.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml
from yaml.composer import Composer
from yaml.events import MappingStartEvent, ScalarEvent, SequenceStartEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.parser import Parser
from yaml.reader import Reader
from yaml.resolver import Resolver
from yaml.scanner import Scanner

from chaperone.errors import ConfigError, ConfigIssue

JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
TRUE_WORDS = {"true", "True", "TRUE"}
FALSE_WORDS = {"false", "False", "FALSE"}
NULL_WORDS = {"null", "Null", "NULL", "~", ""}
YAML_TAG = "tag:yaml.org,2002:"
MERGE = YAML_TAG + "merge"
# A plain value that is text here and something else to plain YAML, by the tag plain YAML gives it: what plain YAML
# reads, and what to write to mean that.
PLAIN_YAML_READS = {
    YAML_TAG + "bool": ("true/false", "write true or false"),
    YAML_TAG + "int": ("a number", "write the number as JSON does (12, 0.5, 5e-3)"),
    YAML_TAG + "float": ("a number", "write the number as JSON does (12, 0.5, 5e-3)"),
    YAML_TAG + "timestamp": ("a date", "quote it"),
    YAML_TAG + "value": ("a special value", "quote it"),                # `=`
    MERGE: ("a merge", "quote it"),                                      # `<<` written as a value, not as a key
}
TYPE_RULE = ("a value's type is how it is written (a number as JSON writes it, true or false, anything else text; "
             "quote a text that looks like something else)")


class LineDict(dict):
    """A mapping read from a file. `line`: where it begins; `key_lines`: the line of each key."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.line: int | None = None
        self.key_lines: dict[Any, int] = {}


class LineList(list):
    """A list read from a file. `line`: where it begins; `item_lines`: the line of each item."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.line: int | None = None
        self.item_lines: list[int] = []


class Conflict(ValueError):
    """A key given two different values, or two keys that are one key to a mapping. `line` is the later place;
    `column` is given too when both are on one line."""

    def __init__(self, file: str, line: int, reason: str, column: int | None = None) -> None:
        self.file, self.line, self.reason = file, line, reason
        super().__init__(f"{file}:{line}:{column}: {reason}" if column else f"{file}:{line}: {reason}")

    def message(self) -> str:
        return self.reason


class Refused(yaml.MarkedYAMLError):
    """A value or a tag this loader does not read, at its place in the file."""

    def __init__(self, problem: str, mark: Any) -> None:
        super().__init__(problem=problem, problem_mark=mark)


def same_value(a: Any, b: Any) -> bool:
    """Whether two values read from files are the same: of the same type, and the same part by part. 1, 1.0 and true
    are three values, though Python counts them equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) != len(b):
            return False
        for key, value in a.items():
            if key not in b:
                return False
            other = next(k for k in b if k == key)
            if not (same_value(key, other) and same_value(value, b[key])):
                return False
        return True
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


LAYER: Any = None        # the user's changes to the shipped files (chaperone.overrides.Layer), set by the command


def read_yaml(path: str | Path, layered: bool = True) -> Any:
    """Read one configuration file. An empty file, or one of comments only, is an empty mapping.

    Raises `Conflict` (a ValueError) for a key given twice, yaml.YAMLError for a mistake of YAML, a refused value or
    text that is not UTF-8 (each naming the file and the place), and OSError for a file that cannot be opened.
    With `layered`, the user's changes to the file (`LAYER`) are applied; rules, exceptions and state are read without."""
    name = str(path)
    with open(path, "rb") as stream:                          # the stream's name is the file's, for every message
        loader = _Loader(stream)
        try:
            node = loader.get_single_node()
        finally:
            loader.dispose()
    value = LineDict() if node is None else _Builder(name).value(node)
    return value if LAYER is None else LAYER.apply(name, value, layered)


def load_yaml(path: str | Path, layered: bool = True) -> Any:
    """`read_yaml`, with each way it can fail given as a ConfigError that names the file and, where known, the line."""
    try:
        return read_yaml(path, layered)
    except OSError as exc:
        line, reason = None, f"cannot read the file: {exc.strerror or exc}"
    except Conflict as conflict:
        line, reason = conflict.line, conflict.message()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = None if mark is None else mark.line + 1
        reason = f"not valid YAML: {getattr(exc, 'problem', None) or exc}"
    raise ConfigError([ConfigIssue(str(path), line, reason)])


class _Composer(Composer):
    """PyYAML's composer, refusing a node written with a tag before plain YAML can read the node by it."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, (ScalarEvent, SequenceStartEvent, MappingStartEvent)) and event.tag is not None:
            written = "!!" + event.tag[len(YAML_TAG):] if event.tag.startswith(YAML_TAG) else event.tag
            raise Refused(f"the tag {written} is refused: {TYPE_RULE}", event.start_mark)
        return super().compose_node(parent, index)


class _Loader(Reader, Scanner, Parser, _Composer, Resolver):
    """Reads a stream into nodes. PyYAML's resolver gives each plain value the tag plain YAML would read it by; no
    value is built here."""

    def __init__(self, stream: Any) -> None:
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)
        _Composer.__init__(self)
        Resolver.__init__(self)


class _Builder:
    """Builds the values of one file from its nodes. A node reached twice, through an alias, is built once."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.built: dict[int, Any] = {}
        self.building: set[int] = set()
        self.places: dict[int, dict[Any, tuple[Any, int, int, str]]] = {}   # a mapping's keys: (key, line, column, as written)

    def value(self, node: Node) -> Any:
        if id(node) in self.built:
            return self.built[id(node)]
        if id(node) in self.building:
            raise Refused("a value that contains itself (through an alias) is refused", node.start_mark)
        self.building.add(id(node))
        try:
            if isinstance(node, ScalarNode):
                result = self.scalar(node)
            elif isinstance(node, SequenceNode):
                result = self.sequence(node)
            else:
                result = self.mapping(node)
        finally:
            self.building.discard(id(node))
        self.built[id(node)] = result
        return result

    def scalar(self, node: ScalarNode) -> Any:
        text = node.value
        if node.style is not None:                             # quoted, or a block of text
            return text
        if JSON_NUMBER.fullmatch(text):
            if not any(c in text for c in ".eE"):
                return int(text)
            number = float(text)
            if math.isinf(number):
                raise Refused(f"'{text}' is too large to be a number", node.start_mark)
            return number
        if text in TRUE_WORDS:
            return True
        if text in FALSE_WORDS:
            return False
        if text in NULL_WORDS:
            return None
        if node.tag in PLAIN_YAML_READS:
            meant, fix = PLAIN_YAML_READS[node.tag]
            write = f'quote it: "{text}"' if fix == "quote it" else f'{fix}, or quote it: "{text}"'
            raise Refused(f"'{text}' is {meant} to plain YAML and text here: {write}", node.start_mark)
        return text

    def sequence(self, node: SequenceNode) -> LineList:
        out = LineList()
        out.line = node.start_mark.line + 1
        for item in node.value:
            out.append(self.value(item))
            out.item_lines.append(item.start_mark.line + 1)
        return out

    def mapping(self, node: MappingNode) -> LineDict:
        out = LineDict()
        out.line = node.start_mark.line + 1
        places: dict[Any, tuple[Any, int, int, str]] = {}
        self.places[id(out)] = places
        written: list[tuple[Node, Node]] = []
        for key_node, value_node in node.value:                # keys merged with `<<` first, then the keys written here
            if isinstance(key_node, ScalarNode) and key_node.style is None and key_node.tag == MERGE:
                for source in self.merged(value_node):
                    for key, (_, line, column, as_written) in self.places[id(source)].items():
                        self.put(out, places, key, source[key], line, column, as_written)
            else:
                written.append((key_node, value_node))
        for key_node, value_node in written:
            if not isinstance(key_node, ScalarNode):
                raise Refused("a key is refused unless it is a text, a number, true, false or null", key_node.start_mark)
            key = self.value(key_node)
            mark = key_node.start_mark
            self.put(out, places, key, self.value(value_node), mark.line + 1, mark.column + 1, key_node.value)
        return out

    def merged(self, node: Node) -> list[LineDict]:
        """The mappings a `<<` merges: one mapping, or a list of them."""
        nodes = node.value if isinstance(node, SequenceNode) else [node]
        if not all(isinstance(n, MappingNode) for n in nodes):
            raise Refused("`<<` merges a mapping, or a list of mappings", node.start_mark)
        return [self.value(n) for n in nodes]

    def put(self, out: LineDict, places: dict, key: Any, value: Any, line: int, column: int, as_written: str) -> None:
        if key in places:                                      # a key Python counts as one already there
            first, first_line, first_column, first_written = places[key]
            one_line = first_line == line
            at_first, at_here = ((f"line {first_line}:{first_column}", f"line {line}:{column}") if one_line
                                 else (f"line {first_line}", f"line {line}"))
            if not same_value(first, key):
                raise Conflict(self.name, line, f"the keys {first_written} ({at_first}) and {as_written} ({at_here}) "
                                                "are one key to the program, so one would silently replace the other. "
                                                "Write one of them", column if one_line else None)
            if not same_value(out[key], value):
                raise Conflict(self.name, line, f"'{_named(key)}' is given two different values, at {at_first} and "
                                                f"{at_here}; plain YAML would silently keep the second. Write it once",
                               column if one_line else None)
            return                                             # the same value again: nothing changes
        places[key] = (key, line, column, as_written)
        out[key] = value
        out.key_lines[key] = line


def _named(key: Any) -> str:
    """A key as YAML writes it."""
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    return str(key)
