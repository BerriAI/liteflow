from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, JsonValue

ChatMessage: TypeAlias = ChatCompletionMessageParam


class Input(str, Enum):
    messages = "messages"
    previous = "previous"


@dataclass(frozen=True, slots=True, kw_only=True)
class Retry:
    max_attempts: int = 1
    backoff_seconds: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatCompletion:
    model: str
    messages: Input | Sequence[ChatMessage]
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
    inputs: Mapping[str, Input | JsonValue] | None = None
    timeout_seconds: float = 60.0
    retry: Retry = Retry()


@dataclass(frozen=True, slots=True, kw_only=True)
class Workflow:
    name: str
    nodes: Sequence[ChatCompletion | Python]
    timeout_seconds: float = 300.0
