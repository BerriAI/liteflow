from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence as NodeSequence
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, JsonValue

ChatMessage: TypeAlias = ChatCompletionMessageParam


class Input(str, Enum):
    messages = "messages"
    previous = "previous"
    item = "item"

    def field(self, name: str) -> FieldRef:
        return FieldRef(source=self, fields=(name,))


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldRef:
    source: Input
    fields: tuple[str, ...]

    def field(self, name: str) -> FieldRef:
        return FieldRef(source=self.source, fields=(*self.fields, name))


Binding: TypeAlias = Input | FieldRef | JsonValue


@dataclass(frozen=True, slots=True, kw_only=True)
class Retry:
    max_attempts: int = 1
    backoff_seconds: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatCompletion:
    model: str
    messages: Input | FieldRef | NodeSequence[ChatMessage]
    response_model: type[BaseModel] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    seed: int | None = None
    stop: str | tuple[str, ...] | None = None
    timeout_seconds: float = 60.0
    retry: Retry = Retry()


@dataclass(frozen=True, slots=True, kw_only=True)
class Python:
    fn: Callable[..., object]
    inputs: Mapping[str, Binding] | None = None
    timeout_seconds: float = 60.0
    retry: Retry = Retry()


@dataclass(frozen=True, slots=True, kw_only=True)
class Sequence:
    nodes: NodeSequence[Node]


@dataclass(frozen=True, slots=True, kw_only=True)
class Parallel:
    branches: Mapping[str, NodeSequence[Node]]
    max_concurrency: int = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class ForEach:
    items: Binding
    nodes: NodeSequence[Node]
    max_items: int = 100
    max_concurrency: int = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class If:
    condition: Callable[..., bool]
    then: NodeSequence[Node]
    otherwise: NodeSequence[Node]


Node: TypeAlias = ChatCompletion | Python | Sequence | Parallel | ForEach | If


@dataclass(frozen=True, slots=True, kw_only=True)
class Workflow:
    name: str
    nodes: NodeSequence[Node]
    timeout_seconds: float = 300.0
