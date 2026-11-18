"""Talk to a language model. One small client, two kinds of server.

A model is described by a settings file, one per model:

    name: "a local model (8077)"
    provider: openai                 # any server with the common chat-completions interface; or: anthropic, stand-in
    model: "<the model's name on its server>"
    api_base: http://localhost:8077/v1
    api_key_env: null                # the NAME of an environment variable; the key itself is never in a file
    temperature: 0.0
    max_tokens: 1024
    timeout: 300
    extra:
      request_body: {seed: 42}       # merged into every request

`provider: stand-in` is no model server: code the application supplies answers instead, and `model` names which.
Its settings are read here like any other, and ChatModel refuses them.

A settings file describes an endpoint any project may use; which tool it is, and that tool's facts, the project's
setup states.

Every call is written to the access log: which model, the kind of question, how
long it took and how many tokens. The question and the answer are not logged.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import difflib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any

from chaperone.yaml_lines import read_yaml

from chaperone.connect.access_log import AccessLog

# Every setting a model's file may have, and what it is: the file is refused otherwise, so `temperature: "0.5"` is no
# text sent on to the server.
KNOWN = {"name": "text", "provider": "text", "model": "text", "api_base": "text or null", "api_key_env": "text or null",
         "temperature": "a number", "max_tokens": "a whole number", "timeout": "a number",
         "context_window": "a whole number or null", "extra": "a mapping"}
KINDS = {"text": (str,), "a number": (int, float), "a whole number": (int,), "a mapping": (dict,), "null": (type(None),)}
PROVIDERS = ("openai", "anthropic", "stand-in")


@dataclass
class LlmSettings:
    name: str
    provider: str
    model: str
    api_base: str | None = None
    api_key_env: str | None = None
    connects: str = ""                      # never in the file; no part of the framework sets it, and the log records it empty
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout: float = 300
    context_window: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def load_llm(path: Path | str) -> LlmSettings:
    raw = read_yaml(path)                                     # the one loader: values as written, a repeat refused
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: a model's settings are a mapping")
    unknown = sorted(map(str, set(raw) - set(KNOWN)))
    if unknown:
        near = difflib.get_close_matches(unknown[0], sorted(KNOWN), n=1)
        raise ValueError(f"{path}: unknown setting '{unknown[0]}'" + (f"; nearest name: '{near[0]}'" if near else "")
                         + f" (known: {', '.join(sorted(KNOWN))})")
    for needed in ("name", "provider", "model"):
        if not raw.get(needed):
            raise ValueError(f"{path}: '{needed}' is required")
    for key, value in raw.items():
        kinds = KNOWN[key].split(" or ")
        if isinstance(value, bool) or not isinstance(value, tuple(t for k in kinds for t in KINDS[k])):
            raise ValueError(f"{path}: '{key}' is {KNOWN[key]}, not {value!r}")
    if raw["provider"] not in PROVIDERS:
        raise ValueError(f"{path}: provider must be one of {', '.join(PROVIDERS)}")
    if raw["provider"] == "openai" and not raw.get("api_base"):
        raise ValueError(f"{path}: 'api_base' is required: the address of the model's server")
    return LlmSettings(**raw)


class ModelError(OSError):
    """The model server could not be reached, or answered with something that is not an answer."""


class ModelRefused(ModelError):
    """The server answered that it will not take this one message (too long, say). Trying again will not change that; the next message may be fine."""


REFUSED_THIS_MESSAGE = (400, 413, 422)


_ONE_AT_A_TIME: dict[str, threading.Lock] = {}                     # one request at a time per model endpoint
_LOCKS = threading.Lock()


def _lock_for(endpoint: str) -> threading.Lock:
    with _LOCKS:
        return _ONE_AT_A_TIME.setdefault(endpoint, threading.Lock())


class ChatModel:
    def __init__(self, settings: LlmSettings, log: AccessLog | None = None) -> None:
        if settings.provider == "stand-in":
            raise ValueError(f"{settings.name}: a stand-in has no server to ask; the application answers for it")
        self.settings = settings
        self.log = log or AccessLog(None)
        self.exchange: Callable[[dict[str, Any]], None] | None = None   # told of every question and answer, for the owner's own journal

    @property
    def label(self) -> str:
        return self.settings.name

    def ask(self, kind: str, system: str, user: str, max_tokens: int | None = None) -> str:
        """One question, one answer, as text. `kind` is a short name for the log. Requests to one endpoint never
        overlap, whatever calls this: a local model on a shared-memory GPU is asked one thing at a time."""
        with _lock_for(self.settings.api_base or self.settings.provider):
            return self._ask(kind, system, user, max_tokens)

    def _ask(self, kind: str, system: str, user: str, max_tokens: int | None) -> str:
        s = self.settings
        limit = max_tokens or s.max_tokens
        key = os.environ.get(s.api_key_env) if s.api_key_env else None
        if s.api_key_env and not key:
            raise ModelError(f"the environment variable {s.api_key_env} is not set")
        if s.provider == "openai":
            url = str(s.api_base).rstrip("/") + "/chat/completions"
            body = {"model": s.model, "temperature": s.temperature, "max_tokens": limit,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    **(s.extra.get("request_body") or {})}
            headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
        else:
            url = (s.api_base or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
            body = {"model": s.model, "max_tokens": limit, "system": system,
                    "messages": [{"role": "user", "content": user}], **(s.extra.get("request_body") or {})}
            headers = {"Content-Type": "application/json", "x-api-key": key or "", "anthropic-version": "2023-06-01"}

        started = time.monotonic()
        try:
            request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(request, timeout=s.timeout) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.log.record("model", kind, model=s.name, failed=f"HTTP {exc.code}")
            if exc.code in REFUSED_THIS_MESSAGE:
                raise ModelRefused(f"{s.name} refused this message: HTTP {exc.code}") from exc
            raise ModelError(f"no answer from {s.name}: HTTP {exc.code}") from exc
        except (OSError, http.client.HTTPException, ValueError) as exc:    # URLError and TimeoutError are OSErrors; a cut answer is neither
            self.log.record("model", kind, model=s.name, failed=type(exc).__name__)
            raise ModelError(f"no answer from {s.name}: {type(exc).__name__}") from exc
        ms = round((time.monotonic() - started) * 1000)

        try:
            if s.provider == "openai":
                text = answer["choices"][0]["message"].get("content") or ""
                was_cut = answer["choices"][0].get("finish_reason") == "length"
                usage = answer.get("usage") or {}
                tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
            else:
                text = "".join(part.get("text", "") for part in answer["content"] if part.get("type") == "text")
                was_cut = answer.get("stop_reason") == "max_tokens"
                usage = answer.get("usage") or {}
                tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            self.log.record("model", kind, model=s.name, ms=ms, failed="unexpected reply")
            raise ModelError(f"{s.name} replied with something that is not an answer") from exc
        self.log.record("model", kind, model=s.name, connects=s.connects, ms=ms, tokens_in=tokens_in, tokens_out=tokens_out)
        if self.exchange is not None:
            self.exchange({"kind": kind, "model": s.name, "connects": s.connects, "ms": ms, "tokens_in": tokens_in, "tokens_out": tokens_out,
                           "system": system, "user": user, "answer": str(text)})
        if was_cut:                                               # half an answer is no answer: the step refuses it, and it is counted
            self.log.record("model", "llm_cut", step=kind, reason=f"the answer was cut at max_tokens {limit}")
            return ""
        return str(text)
