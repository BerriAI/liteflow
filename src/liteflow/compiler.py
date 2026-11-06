import ast
import dataclasses
import hashlib
import inspect
import json
import textwrap
from collections.abc import Callable, Mapping
from typing import get_args, get_type_hints

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, create_model

from liteflow.authoring import ChatCompletion, Input, Python, Workflow


def _reference(binding: Input | JsonValue, previous: str) -> JsonValue:
    if binding is Input.messages:
        return {"from": "$input", "output": "messages"}
    if binding is Input.previous:
        return {"from": previous, "output": "messages" if previous == "$input" else "result"}
    return {"value": TypeAdapter(JsonValue).validate_python(binding)}


def _record_names(annotation: object) -> frozenset[str]:
    nested = frozenset(name for argument in get_args(annotation) for name in _record_names(argument))
    if not isinstance(annotation, type):
        return nested
    if not (issubclass(annotation, BaseModel) or dataclasses.is_dataclass(annotation)):
        return nested
    fields = get_type_hints(annotation)
    return (
        nested | {annotation.__name__} | frozenset(name for field in fields.values() for name in _record_names(field))
    )


def _mark_records(schema: dict[str, object], records: frozenset[str]) -> dict[str, object]:
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        return schema
    return {
        **schema,
        **(
            {
                "$defs": {
                    name: {**definition, **({"x-liteflow-record": True} if name in records else {})}
                    for name, definition in definitions.items()
                }
            }
            if definitions
            else {}
        ),
    }


def _source(fn: Callable[..., object], seen: frozenset[str] = frozenset()) -> str:
    if fn.__closure__ or "<locals>" in fn.__qualname__:
        raise ValueError(f"{fn.__name__}: closures are unsupported")
    module = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    definition = module.body[0]
    if not isinstance(definition, ast.FunctionDef):
        raise ValueError(f"{fn.__name__}: expected a top-level synchronous function")
    if any(isinstance(node, (ast.Lambda, ast.ClassDef)) for node in ast.walk(definition)):
        raise ValueError(f"{fn.__name__}: nested definitions are unsupported")
    if any(
        node is not definition and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(definition)
    ):
        raise ValueError(f"{fn.__name__}: nested definitions are unsupported")
    definition.decorator_list = []
    globals_used = inspect.getclosurevars(fn).globals
    helpers = tuple(
        value
        for name, value in globals_used.items()
        if name not in seen and inspect.isfunction(value) and value.__module__ == fn.__module__
    )
    unsupported = tuple(
        name for name, value in globals_used.items() if not inspect.isfunction(value) and not isinstance(value, type)
    )
    if unsupported:
        raise ValueError(f"{fn.__name__}: captured globals are unsupported")
    return "\n".join((*(_source(helper, seen | {fn.__name__}) for helper in helpers), ast.unparse(definition)))


def _program(node: Python) -> dict[str, JsonValue]:
    signature = inspect.signature(node.fn)
    parameters = tuple(signature.parameters.values())
    if not parameters or any(
        parameter.kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        or parameter.default is not inspect.Parameter.empty
        for parameter in parameters
    ):
        raise ValueError(f"{node.fn.__name__}: use required positional parameters")
    hints = get_type_hints(node.fn)
    if "return" not in hints or any(parameter.name not in hints for parameter in parameters):
        raise ValueError(f"{node.fn.__name__}: every parameter and return must be annotated")
    records = frozenset(name for annotation in hints.values() for name in _record_names(annotation))
    input_model = create_model(
        "Arguments",
        __config__=ConfigDict(extra="forbid"),
        **{parameter.name: (hints[parameter.name], ...) for parameter in parameters},
    )
    source = _source(node.fn) + "\n"
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "contract_version": "1",
            "source": source,
            "function_name": node.fn.__name__,
            "parameter_names": [parameter.name for parameter in parameters],
            "input_schema": _mark_records(input_model.model_json_schema(), records),
            "output_schema": _mark_records(TypeAdapter(hints["return"]).json_schema(), records),
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        }
    )


def _node(node: ChatCompletion | Python, node_id: str, previous: str) -> dict[str, JsonValue]:
    common: dict[str, JsonValue] = {
        "id": node_id,
        "timeout_seconds": node.timeout_seconds,
        "retry": {"max_attempts": node.retry.max_attempts, "backoff_seconds": node.retry.backoff_seconds},
    }
    if isinstance(node, ChatCompletion):
        response_format = (
            None
            if node.response_model is None
            else {
                "type": "json_schema",
                "json_schema": {
                    "name": node.response_model.__name__,
                    "strict": True,
                    "schema": node.response_model.model_json_schema(),
                },
            }
        )
        arguments = {
            name: value
            for name, value in {
                "temperature": node.temperature,
                "top_p": node.top_p,
                "max_tokens": node.max_tokens,
                "max_completion_tokens": node.max_completion_tokens,
                "seed": node.seed,
                "stop": list(node.stop) if isinstance(node.stop, tuple) else node.stop,
                "response_format": response_format,
            }.items()
            if value is not None
        }
        binding = node.messages if isinstance(node.messages, Input) else list(node.messages)
        return TypeAdapter(dict[str, JsonValue]).validate_python(
            {
                **common,
                "type": "chat_completion",
                "model": node.model,
                "messages": _reference(binding, previous),
                **arguments,
            }
        )
    program = _program(node)
    names = inspect.signature(node.fn).parameters
    if node.inputs is None and len(names) != 1:
        raise ValueError(f"{node_id}: multi-argument Python requires inputs")
    inputs = node.inputs if node.inputs is not None else {next(iter(names)): Input.previous}
    if set(inputs) != set(names):
        raise ValueError(f"{node_id}: inputs must bind exactly the function parameters")
    return {
        **common,
        "type": "python",
        "program": program,
        "inputs": {name: _reference(value, previous) for name, value in inputs.items()},
    }


def compile_workflow(workflow: Workflow) -> dict[str, JsonValue]:
    if not workflow.nodes:
        raise ValueError("A workflow must have at least one node")
    return {
        "contract_version": "1",
        "name": workflow.name,
        "timeout_seconds": workflow.timeout_seconds,
        "input_schema": TypeAdapter(list[ChatCompletionMessageParam]).json_schema(),
        "output": {"from": f"node_{len(workflow.nodes)}", "output": "result"},
        "nodes": [
            _node(node, f"node_{index + 1}", "$input" if index == 0 else f"node_{index}")
            for index, node in enumerate(workflow.nodes)
        ],
    }


def bundle_digest(bundle: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(
        json.dumps(dict(bundle), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
