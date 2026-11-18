"""Settings changed without editing the shipped files: one file of the user's, by default `private/local.yaml`.

The file names a configuration file by its path from the folder the command runs in, then only the keys to change:

    configs/llm/local.yaml:
      api_base: http://localhost:8080/v1
    examples/deliveries/setups/real.yaml:
      tools: {MAILBOX: {client: {days: 7}}}

When the loader reads a file the layer names, a mapping is merged key by key and any other value (text, a number, a
list) replaces the shipped one whole. A key the shipped file does not have changes nothing and is said. Rules files,
exceptions files and the state folder's files are read without the layer: a change to one of them is said, not made.
Only files a run reads are changed; the layer opens no other file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chaperone import yaml_lines
from chaperone.errors import ConfigError, ConfigIssue


@dataclass
class Layer:
    file: str                                                 # the layer's own path, as given
    entries: dict[Path, tuple[str, Any]]                      # a configuration file's resolved path: (its key as written, the changes)
    changed: dict[tuple[str, str], str] = field(default_factory=dict)   # (file, key) -> how, for the report
    warnings: list[str] = field(default_factory=list)
    used: set[Path] = field(default_factory=set)
    top_lines: dict[str, int] = field(default_factory=dict)  # the line of each file's key in the layer
    _said: set[str] = field(default_factory=set)
    _reported: set[tuple[str, str]] = field(default_factory=set)

    def warn(self, text: str) -> None:
        if text not in self._said:
            self._said.add(text)
            self.warnings.append(text)

    def apply(self, path: str, value: Any, layered: bool) -> Any:
        """`value`, read from `path`, with this layer's changes for that file."""
        target = Path(path).resolve()
        if target not in self.entries:
            return value
        written, changes = self.entries[target]
        self.used.add(target)
        if not layered:
            self.warn(f"WARNING {self.file}:{_line(self, written)}: {written} is a rules, exceptions or state file, which "
                      f"{self.file} does not change; nothing changed.")
            return value
        if not isinstance(value, dict) or not isinstance(changes, dict):
            self.warn(f"WARNING {self.file}:{_line(self, written)}: {written} holds no keys to change; nothing changed.")
            return value
        self._merge(written, value, changes, [])
        return value

    def _merge(self, written: str, shipped: dict, changes: dict, where: list[str]) -> None:
        for key, new in changes.items():
            dotted = ".".join(where + [str(key)])
            line = getattr(changes, "key_lines", {}).get(key)
            if key not in shipped:
                self.warn(f"WARNING {self.file}:{line}: {written} has no key `{dotted}`; nothing changed.")
                continue
            old = shipped[key]
            if isinstance(old, dict) and isinstance(new, dict):
                self._merge(written, old, new, where + [str(key)])
                continue
            shipped[key] = new
            how = f"{self.file}:{line}" + (", a list, replaced whole" if isinstance(old, list) else "")
            self.changed[(written, dotted)] = how

    def report(self) -> list[str]:
        """What the layer changed since the last report, and what it could not: a line, then a line for each."""
        lines, new = [], sorted(k for k in self.changed if k not in self._reported)
        if new:
            lines.append(f"Configuration: {len(new)} setting{'' if len(new) == 1 else 's'} changed by {self.file}:")
            lines += [f"    {file}: {key} ({self.changed[(file, key)]})" for file, key in new]
            self._reported.update(new)
        lines += self.warnings
        self.warnings = []
        return lines

    def unused(self) -> list[str]:
        """The files the layer names that this run did not read: said once the run has read what it reads."""
        return [f"NOTE {self.file}: {written} was not read by this run; nothing in it changed."
                for target, (written, _) in self.entries.items() if target not in self.used]


def _line(layer: Layer, written: str) -> int | None:
    return layer.top_lines.get(written)


def read_layer(file: str | Path) -> Layer:
    """The layer in `file`: a mapping of configuration files' paths to the changes for each. A path that names no file
    is said, and changes nothing."""
    data = yaml_lines.load_yaml(file, layered=False)
    if not isinstance(data, dict):
        raise ConfigError([ConfigIssue(str(file), None, "expected a mapping: a configuration file's path, then the keys "
                                                        "to change in it")])
    layer = Layer(str(file), {}, top_lines=dict(getattr(data, "key_lines", {})))
    for written, changes in data.items():
        target = Path(str(written)).resolve()
        if not target.is_file():
            layer.warn(f"WARNING {file}:{layer.top_lines.get(written)}: no file {written}; nothing changed.")
            continue
        layer.entries[target] = (str(written), changes)
    return layer
