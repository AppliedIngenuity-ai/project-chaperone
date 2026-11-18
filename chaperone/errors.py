"""The error raised for a configuration that cannot be used: every problem found, each with its file and line."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigIssue:
    """One problem of a configuration: the file, the line (None when no single line is at fault), what is wrong."""

    file: str
    line: int | None
    message: str

    def __str__(self) -> str:
        if self.line is None:
            return f"{self.file}: {self.message}"
        return f"{self.file}:{self.line}: {self.message}"


class ConfigError(Exception):
    """Every problem found in a configuration, raised together, so that all of them can be corrected at once."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = list(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))
