import ast
import dataclasses
import hashlib
import inspect
import json
import math
import re
import textwrap
from collections.abc import Callable, Mapping, Sequence as NodeSequence
from dataclasses import dataclass
from typing import get_args, get_type_hints

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, create_model

from liteflow.authoring import (
    Binding,
    ChatCompletion,
    FieldRef,
    ForEach,
    If,
    Input,
    Node,
    Parallel,
    Python,
    Sequence,
    Workflow,
)


_MAX_DEPTH = 16
_MAX_EXPANDED_NODES = 10_000
_BRANCH_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


def _schema(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("Expected an object schema")
    return value


def _type_schema(annotation: object) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_python(TypeAdapter(annotation).json_schema())


def _resolve(schema: dict[str, JsonValue], document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    for _ in range(_MAX_DEPTH):
        reference = schema.get("$ref")
        if not isinstance(reference, str):
            return schema
        if not reference.startswith("#/$defs/"):
            raise ValueError("Only local schema references are supported")
        if set(schema) - {"$ref", "$defs", "title", "description", "default", "examples", "x-liteflow-record"}:
            raise ValueError("Schema validation keywords beside $ref are unsupported")
        definitions = _schema(document.get("$defs", {}))
        name = reference.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
        if name not in definitions:
            raise ValueError("Unresolved schema reference")
        schema = _schema(definitions[name])
    raise ValueError("Schema reference nesting is too deep")


def _normalized(schema: dict[str, JsonValue]) -> JsonValue:
    def visit(value: dict[str, JsonValue], depth: int) -> dict[str, JsonValue]:
        if depth > 64:
            raise ValueError("Recursive or excessively nested schemas are unsupported")
        resolved = _resolve(value, schema) if "$ref" in value else value
        result: dict[str, JsonValue] = {}
        for key, item in resolved.items():
            if key in {"$defs", "title", "description", "default", "x-liteflow-record"}:
                continue
            if key == "properties" and isinstance(item, dict):
                result[key] = {name: visit(_schema(field), depth + 1) for name, field in item.items()}
            elif key in {"items", "additionalProperties"} and isinstance(item, dict):
                result[key] = visit(item, depth + 1)
            elif key in {"anyOf", "oneOf", "allOf", "prefixItems"} and isinstance(item, list):
                parts = [visit(_schema(part), depth + 1) for part in item]
                result[key] = (
                    parts if key == "prefixItems" else sorted(parts, key=lambda part: json.dumps(part, sort_keys=True))
                )
            elif key in {"required", "enum"} and isinstance(item, list):
                result[key] = sorted(item, key=lambda part: json.dumps(part, sort_keys=True))
            else:
                result[key] = item
        return result

    return visit(schema, 0)


def _compatible(actual: dict[str, JsonValue], expected: dict[str, JsonValue]) -> bool:
    def accepts(source: dict[str, JsonValue], target: dict[str, JsonValue]) -> bool:
        if source == target:
            return True
        source_union = source.get("anyOf", source.get("oneOf"))
        if isinstance(source_union, list):
            return all(accepts(_schema(part), target) for part in source_union)
        target_union = target.get("anyOf", target.get("oneOf"))
        if isinstance(target_union, list):
            return any(accepts(source, _schema(part)) for part in target_union)
        if not source and target:
            return False
        source_type, target_type = source.get("type"), target.get("type")
        if source_type is None and target_type is not None and "const" not in source and "enum" not in source:
            return False
        if source_type is not None and target_type is not None and source_type != target_type:
            if (source_type, target_type) != ("integer", "number"):
                return False
        if "const" in target and source.get("const") != target["const"]:
            return False
        if isinstance(target.get("enum"), list):
            choices = source.get("enum", [source["const"]] if "const" in source else None)
            if not isinstance(choices, list) or not all(choice in target["enum"] for choice in choices):
                return False
        if target_type == "array" and isinstance(source.get("items"), dict) and isinstance(target.get("items"), dict):
            return accepts(_schema(source["items"]), _schema(target["items"]))
        if (
            target_type == "array"
            and isinstance(source.get("prefixItems"), list)
            and isinstance(target.get("items"), dict)
        ):
            return all(accepts(_schema(item), _schema(target["items"])) for item in source["prefixItems"])
        if target_type == "object":
            source_fields = _schema(source.get("properties", {}))
            target_fields = _schema(target.get("properties", {}))
            source_required = source.get("required", [])
            for name in target.get("required", []):
                if name not in source_fields or name not in source_required:
                    return False
            additional = target.get("additionalProperties")
            extra_fields = {name: field for name, field in source_fields.items() if name not in target_fields}
            if additional is False and extra_fields:
                return False
            if isinstance(additional, dict):
                if any(not accepts(_schema(field), additional) for field in extra_fields.values()):
                    return False
                source_additional = source.get("additionalProperties")
                if isinstance(source_additional, dict) and not accepts(source_additional, additional):
                    return False
                if not source_fields and not isinstance(source_additional, dict):
                    return False
            return all(
                accepts(_schema(source_fields[name]), _schema(field))
                for name, field in target_fields.items()
                if name in source_fields
            )
        return True

    return accepts(_schema(_normalized(actual)), _schema(_normalized(expected)))


def _embedded(schemas: Mapping[str, dict[str, JsonValue]]) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    properties: dict[str, JsonValue] = {}
    definitions: dict[str, JsonValue] = {}
    for index, (name, document) in enumerate(schemas.items()):
        local = _schema(document.get("$defs", {}))
        names = {key: f"part_{index}_{key}" for key in local}

        def rename(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
            result = dict(value)
            reference = result.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                key = reference.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
                if key not in names:
                    raise ValueError("Unresolved schema reference")
                result["$ref"] = "#/$defs/" + names[key].replace("~", "~0").replace("/", "~1")
            for keyword in ("properties", "patternProperties", "dependentSchemas", "$defs"):
                children = result.get(keyword)
                if isinstance(children, dict):
                    result[keyword] = {name: rename(_schema(child)) for name, child in children.items()}
            for keyword in (
                "items",
                "additionalProperties",
                "contains",
                "not",
                "if",
                "then",
                "else",
                "propertyNames",
                "unevaluatedProperties",
                "unevaluatedItems",
            ):
                child = result.get(keyword)
                if isinstance(child, dict):
                    result[keyword] = rename(child)
            for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
                children = result.get(keyword)
                if isinstance(children, list):
                    result[keyword] = [rename(_schema(child)) for child in children]
            discriminator = result.get("discriminator")
            if isinstance(discriminator, dict) and isinstance(discriminator.get("mapping"), dict):
                result["discriminator"] = {
                    **discriminator,
                    "mapping": {
                        name: rename({"$ref": reference})["$ref"]
                        for name, reference in discriminator["mapping"].items()
                    },
                }
            return result

        properties[name] = rename({key: value for key, value in document.items() if key != "$defs"})
        definitions.update({names[key]: rename(_schema(value)) for key, value in local.items()})
    return properties, definitions


def _with_definitions(schema: dict[str, JsonValue], definitions: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {**schema, **({"$defs": definitions} if definitions else {})}


def _field(document: dict[str, JsonValue], name: str) -> dict[str, JsonValue]:
    if not isinstance(name, str) or not name:
        raise ValueError("Field names must be nonempty strings")
    schema = _resolve(document, document)
    alternatives = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(alternatives, list):
        projected = {
            str(index): _field(_with_definitions(_schema(part), _schema(document.get("$defs", {}))), name)
            for index, part in enumerate(alternatives)
        }
        representative = next(iter(projected.values()))
        if all(_normalized(part) == _normalized(representative) for part in projected.values()):
            return representative
        fields, definitions = _embedded(projected)
        return _with_definitions({"anyOf": list(fields.values())}, definitions)
    properties = _schema(schema.get("properties", {}))
    if name not in properties or name not in schema.get("required", []):
        raise ValueError(f"Field {name!r} must exist and be required in every possible input shape")
    return _with_definitions(_schema(properties[name]), _schema(document.get("$defs", {})))


@dataclass(frozen=True)
class _Value:
    reference: dict[str, JsonValue]
    schema: dict[str, JsonValue]


@dataclass(frozen=True)
class _Scope:
    previous: _Value
    messages: _Value
    item: _Value | None = None


def _reference(binding: Binding, scope: _Scope) -> _Value:
    if isinstance(binding, FieldRef):
        if not isinstance(binding.source, Input):
            raise ValueError("Field references must start from Input.messages, Input.previous, or Input.item")
        value = _reference(binding.source, scope)
        schema = value.schema
        for name in binding.fields:
            schema = _field(schema, name)
        path = "/" + "/".join(name.replace("~", "~0").replace("/", "~1") for name in binding.fields)
        if not binding.fields:
            raise ValueError("A field reference must select at least one field")
        return _Value({**value.reference, "path": path}, schema)
    if binding is Input.messages:
        return scope.messages
    if binding is Input.previous:
        return scope.previous
    if binding is Input.item:
        if scope.item is None:
            raise ValueError("Input.item is only available inside a ForEach body")
        return scope.item
    value = TypeAdapter(JsonValue).validate_python(binding)
    return _Value({"value": value}, _literal_schema(value))


def _literal_schema(value: JsonValue) -> dict[str, JsonValue]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "const": value}
    if isinstance(value, str):
        return {"type": "string", "const": value}
    if isinstance(value, int):
        return {"type": "integer", "const": value}
    if isinstance(value, float):
        return {"type": "number", "const": value}
    if isinstance(value, list):
        return (
            {"type": "array", "items": {"anyOf": [_literal_schema(item) for item in value]}}
            if value
            else {"type": "array"}
        )
    return {
        "type": "object",
        "properties": {name: _literal_schema(item) for name, item in value.items()},
        "required": list(value),
    }


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


def _positive(value: float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be positive and finite")


def _count(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _check_input(value: _Value, expected: dict[str, JsonValue], label: str) -> None:
    if not _compatible(value.schema, expected):
        raise ValueError(f"{label}: input contract is incompatible with its source")


def _node(
    node: ChatCompletion | Python, node_id: str, scope: _Scope
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    _positive(node.timeout_seconds, f"{node_id}.timeout_seconds")
    _count(node.retry.max_attempts, f"{node_id}.retry.max_attempts")
    if (
        isinstance(node.retry.backoff_seconds, bool)
        or not isinstance(node.retry.backoff_seconds, (int, float))
        or not math.isfinite(node.retry.backoff_seconds)
        or node.retry.backoff_seconds < 0
    ):
        raise ValueError(f"{node_id}.retry.backoff_seconds must be nonnegative and finite")
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
        binding = node.messages if isinstance(node.messages, (Input, FieldRef)) else list(node.messages)
        messages = _reference(binding, scope)
        _check_input(messages, scope.messages.schema, f"{node_id}.messages")
        compiled = TypeAdapter(dict[str, JsonValue]).validate_python(
            {
                **common,
                "type": "chat_completion",
                "model": node.model,
                "messages": messages.reference,
                **arguments,
            }
        )
        return compiled, _type_schema(str if node.response_model is None else node.response_model)
    program = _program(node)
    names = inspect.signature(node.fn).parameters
    if node.inputs is None and len(names) != 1:
        raise ValueError(f"{node_id}: multi-argument Python requires inputs")
    inputs = node.inputs if node.inputs is not None else {next(iter(names)): Input.previous}
    if set(inputs) != set(names):
        raise ValueError(f"{node_id}: inputs must bind exactly the function parameters")
    bound = {name: _reference(value, scope) for name, value in inputs.items()}
    input_schema = _schema(program["input_schema"])
    for name, value in bound.items():
        _check_input(value, _field(input_schema, name), f"{node_id}.inputs.{name}")
    return {
        **common,
        "type": "python",
        "program": program,
        "inputs": {name: value.reference for name, value in bound.items()},
    }, _schema(program["output_schema"])


class _Compiler:
    def __init__(self) -> None:
        self.next_id = 1
        self.version = "1"

    def sequence(self, nodes: NodeSequence[Node], scope: _Scope, depth: int = 0) -> tuple[list[JsonValue], _Value, int]:
        if depth > _MAX_DEPTH:
            raise ValueError(f"Workflow nesting exceeds {_MAX_DEPTH} levels")
        if not nodes:
            raise ValueError("Every workflow, sequence, branch, and ForEach body must contain at least one node")
        compiled: list[JsonValue] = []
        previous = scope.previous
        expanded = 0
        for node in nodes:
            result, output, work = self.node(node, _Scope(previous, scope.messages, scope.item), depth)
            compiled.append(result)
            previous = _Value({"from": result["id"], "output": "result"}, output)
            expanded += work
            if expanded > _MAX_EXPANDED_NODES:
                raise ValueError(f"Workflow may expand beyond {_MAX_EXPANDED_NODES} node executions")
        return compiled, previous, expanded

    def node(self, node: Node, scope: _Scope, depth: int) -> tuple[dict[str, JsonValue], dict[str, JsonValue], int]:
        node_id = f"node_{self.next_id}"
        self.next_id += 1
        if isinstance(node, (ChatCompletion, Python)):
            bindings = [node.messages] if isinstance(node, ChatCompletion) else (node.inputs or {}).values()
            if any(isinstance(binding, FieldRef) or binding is Input.item for binding in bindings):
                self.version = "2"
            compiled, output = _node(node, node_id, scope)
            return compiled, output, 1
        self.version = "2"
        if isinstance(node, Sequence):
            children, result, work = self.sequence(node.nodes, scope, depth + 1)
            return {"id": node_id, "type": "sequence", "nodes": children}, result.schema, 1 + work
        if isinstance(node, Parallel):
            _count(node.max_concurrency, f"{node_id}.max_concurrency")
            if not node.branches:
                raise ValueError(f"{node_id}: Parallel requires named branches")
            names = list(node.branches)
            if len(set(names)) != len(names) or any(
                not isinstance(name, str) or not _BRANCH_NAME.fullmatch(name) for name in names
            ):
                raise ValueError(f"{node_id}: branch names must be unique identifiers of at most 64 characters")
            branches: dict[str, JsonValue] = {}
            outputs: dict[str, dict[str, JsonValue]] = {}
            work = 1
            for name, nodes in node.branches.items():
                children, result, count = self.sequence(nodes, scope, depth + 1)
                branches[name] = children
                outputs[name] = result.schema
                work += count
            properties, definitions = _embedded(outputs)
            output = _with_definitions(
                {"type": "object", "properties": properties, "required": names, "additionalProperties": False},
                definitions,
            )
            return (
                {"id": node_id, "type": "parallel", "branches": branches, "max_concurrency": node.max_concurrency},
                output,
                work,
            )
        if isinstance(node, ForEach):
            _count(node.max_items, f"{node_id}.max_items")
            _count(node.max_concurrency, f"{node_id}.max_concurrency")
            items = _reference(node.items, scope)
            collection = _resolve(items.schema, items.schema)
            if collection.get("type") != "array" or not isinstance(collection.get("items"), dict):
                raise ValueError(f"{node_id}.items must reference a typed list")
            item_schema = _with_definitions(_schema(collection["items"]), _schema(items.schema.get("$defs", {})))
            item = _Value({"from": "$item", "output": "result"}, item_schema)
            children, result, work = self.sequence(node.nodes, _Scope(item, scope.messages, item), depth + 1)
            return (
                {
                    "id": node_id,
                    "type": "for_each",
                    "items": items.reference,
                    "nodes": children,
                    "max_items": node.max_items,
                    "max_concurrency": node.max_concurrency,
                },
                _with_definitions(
                    {"type": "array", "items": {key: value for key, value in result.schema.items() if key != "$defs"}},
                    _schema(result.schema.get("$defs", {})),
                ),
                1 + node.max_items * work,
            )
        if isinstance(node, If):
            program = _program(Python(fn=node.condition))
            names = program["parameter_names"]
            if not isinstance(names, list) or len(names) != 1 or get_type_hints(node.condition)["return"] is not bool:
                raise ValueError(f"{node_id}: If condition must have exactly one typed argument and return bool")
            parameter = names[0]
            if not isinstance(parameter, str):
                raise ValueError(f"{node_id}: invalid condition parameter")
            _check_input(scope.previous, _field(_schema(program["input_schema"]), parameter), f"{node_id}.condition")
            then, then_result, then_work = self.sequence(node.then, scope, depth + 1)
            otherwise, else_result, else_work = self.sequence(node.otherwise, scope, depth + 1)
            if _normalized(then_result.schema) != _normalized(else_result.schema):
                raise ValueError(
                    f"{node_id}: If branches must declare the same output contract; normalize their results or annotate both with the same explicit union"
                )
            return (
                {
                    "id": node_id,
                    "type": "if",
                    "condition": {"program": program, "inputs": {parameter: scope.previous.reference}},
                    "then": then,
                    "otherwise": otherwise,
                },
                then_result.schema,
                1 + max(then_work, else_work),
            )
        raise ValueError(f"Unsupported node type: {type(node).__name__}")


def compile_workflow(workflow: Workflow) -> dict[str, JsonValue]:
    _positive(workflow.timeout_seconds, "workflow.timeout_seconds")
    input_schema = _type_schema(list[ChatCompletionMessageParam])
    messages = _Value({"from": "$input", "output": "messages"}, input_schema)
    compiler = _Compiler()
    nodes, output, _ = compiler.sequence(workflow.nodes, _Scope(messages, messages))
    return {
        "contract_version": compiler.version,
        "name": workflow.name,
        "timeout_seconds": workflow.timeout_seconds,
        "input_schema": input_schema,
        "output": output.reference,
        "nodes": nodes,
    }


def bundle_digest(bundle: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(
        json.dumps(dict(bundle), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
