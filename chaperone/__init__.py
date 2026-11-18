"""Chaperone: labels on the data, checked before a run and at every call (docs/specification.md).

A module is declared with `chaperone.module`; a project is read with `chaperone.load_project`. A module raises an error with
`chaperone.ModuleError` (2.3); the framework's own `chaperone.values.Problem` is not for modules.
"""

from .declare import Declaration, module
from .engine import ModuleError, Outcome, Run, check_call
from .load import ModuleFacts, Project, load_project, selects
from .store import Store
from .values import Value

__all__ = ["Declaration", "ModuleError", "ModuleFacts", "Outcome", "Project", "Run", "Store", "Value",
           "check_call", "load_project", "module", "selects"]
