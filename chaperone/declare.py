"""A module's declaration: the one statement above the function (specification 2.3).

    @chaperone.module("code.verify", properties={"DESTRUCTIVE": False}, requests={"UNTRUSTED": False, "VERIFIED": True})
    def verify(summary): ...

`requests` states the label changes the module may request (rule P4). A label with true or false is a change the
module may request for any part of its result. A field of the result with its own map is a change it may request for
that field only:

    requests={"summary": {"CHECKED": True}, "results": {"UNTRUSTED": False}}

The checks read the declaration and never the function's body. A function whose first parameter is named `ctx` takes
a tool; the setup says which (2.7).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

_collecting: list[list["Declaration"]] = []      # one list per project being loaded (chaperone.load)


@dataclass(frozen=True)
class Declaration:
    name: str
    function: Callable[..., Any] = field(repr=False, compare=False)
    properties: dict[str, bool]
    adds: tuple[str, ...]
    requests: dict[str, bool]                    # label -> the value it may request, for any part of its result
    part_requests: dict[str, dict[str, bool]]    # field of its result -> label -> the value, for that field only
    takes_ctx: bool
    file: str
    line: int


def module(name: str, *, properties: dict[str, bool] | None = None, adds: list[str] | None = None,
           requests: dict[str, Any] | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare the function below as the module `name`. Nothing is checked here; chaperone.load checks every part."""
    def declare(function: Callable[..., Any]) -> Callable[..., Any]:
        parameters = list(inspect.signature(function).parameters)
        code = function.__code__
        declaration = Declaration(name=name, function=function, properties=dict(properties or {}),
                                  adds=tuple(adds or ()),
                                  requests={k: v for k, v in (requests or {}).items() if not isinstance(v, dict)},
                                  part_requests={k: dict(v) for k, v in (requests or {}).items() if isinstance(v, dict)},
                                  takes_ctx=bool(parameters) and parameters[0] == "ctx",
                                  file=code.co_filename, line=code.co_firstlineno)
        function.__guard__ = declaration         # type: ignore[attr-defined]
        if _collecting:
            _collecting[-1].append(declaration)
        return function
    return declare
