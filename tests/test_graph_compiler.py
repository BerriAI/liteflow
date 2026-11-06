import importlib.util
import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model
from typing_extensions import TypedDict

from liteflow import (
    ChatCompletion,
    ChatMessage,
    FieldRef,
    ForEach,
    If,
    Input,
    Parallel,
    Python,
    Retry,
    Sequence,
    Workflow,
    bundle_digest,
    compile_workflow,
)
from liteflow.compiler import _embedded


class Items(BaseModel):
    items: list[str]


class Pair(TypedDict):
    left: str
    right: str


class FirstOrder(BaseModel):
    title: str
    amount: int


class OtherOrder(BaseModel):
    amount: int
    title: str


class Accepted(TypedDict):
    status: Literal["accepted"]
    text: str


class Review(TypedDict):
    status: Literal["review"]
    text: str


class FeedbackItems(TypedDict):
    kind: Literal["feedback"]
    items: list[str]


class HappyItems(TypedDict):
    kind: Literal["happy"]
    items: list[str]


class RefSiblings(BaseModel):
    detail: FirstOrder = Field(json_schema_extra={"minimum": 1})


def identity(text: str) -> str:
    return text


def integer(text: str) -> int:
    return len(text)


def consume_integer(number: int) -> int:
    return number


def consume_mapping(values: dict[str, int]) -> str:
    return str(values)


def typed_values(left: str, right: int) -> str:
    return left + str(right)


def unknown(text: str) -> Any:
    return text


def union_items(messages: list[ChatMessage]) -> FeedbackItems | HappyItems:
    return {"kind": "feedback", "items": ["email"]}


def tuple_strings(messages: list[ChatMessage]) -> tuple[str]:
    return ("email",)


def consume_numbers(values: list[int]) -> int:
    return len(values)


def make_items(messages: list[ChatMessage]) -> list[str]:
    return ["first", "second"]


def nested_items(messages: list[ChatMessage]) -> list[list[str]]:
    return [["first"], ["second"]]


def preserve_item(previous: str, original: str) -> str:
    return original + previous


def preserve_outer(previous: list[str], original: list[str]) -> list[str]:
    return original + previous


def join(pair: Pair) -> list[str]:
    return [pair["left"], pair["right"]]


def predicate(text: str) -> bool:
    raise AssertionError("Compilation must never evaluate a predicate")


def not_boolean(text: str) -> str:
    return text


def two_arguments(text: str, other: str) -> bool:
    return text == other


def first_order(text: str) -> FirstOrder:
    return FirstOrder(title=text, amount=1)


def other_order(text: str) -> OtherOrder:
    return OtherOrder(amount=1, title=text)


def accepted(text: str) -> Accepted | Review:
    return {"status": "accepted", "text": text}


def review(text: str) -> Accepted | Review:
    return {"status": "review", "text": text}


def _example(name: str):
    path = Path(__file__).parents[1] / "examples" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_sequential_bundle_and_digest_are_unchanged():
    spec = importlib.util.spec_from_file_location(
        "compiler_fixture_helpers", Path(__file__).with_name("test_compiler.py")
    )
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    workflow = Workflow(
        name="compatibility",
        nodes=[
            ChatCompletion(
                model="support-model",
                messages=Input.messages,
                response_model=helpers.DraftText,
                temperature=0,
                top_p=0.9,
                max_tokens=100,
                max_completion_tokens=200,
                seed=7,
                stop=("a", "b"),
                timeout_seconds=35,
                retry=Retry(max_attempts=2, backoff_seconds=0.5),
            ),
            Python(fn=helpers.prepare_record, inputs={"draft": Input.previous, "messages": Input.messages}),
            ChatCompletion(model="support-model", messages=Input.previous),
        ],
    )
    expected = json.loads(Path(__file__).with_name("fixtures").joinpath("sequential-v1.json").read_text())
    # Keep Liteflow wire fields locked without pinning upstream message schemas.
    messages_schema = TypeAdapter(list[ChatMessage]).json_schema()
    expected["input_schema"] = messages_schema
    expected["nodes"][0]["response_format"]["json_schema"]["schema"] = helpers.DraftText.model_json_schema()
    arguments_schema = create_model(
        "Arguments",
        __config__=ConfigDict(extra="forbid"),
        draft=(helpers.DraftText, ...),
        messages=(list[ChatMessage], ...),
    ).model_json_schema()
    arguments_schema["$defs"]["DraftText"]["x-liteflow-record"] = True
    expected["nodes"][1]["program"]["input_schema"] = arguments_schema
    expected["nodes"][1]["program"]["output_schema"] = messages_schema
    actual = compile_workflow(workflow)
    assert actual == expected
    assert bundle_digest(actual) == bundle_digest(expected)
    assert actual["contract_version"] == "1"


def test_sequence_parallel_and_foreach_use_scoped_inputs_and_global_preorder_ids():
    workflow = Workflow(
        name="hybrid",
        nodes=[
            ChatCompletion(model="analysis-model", messages=Input.messages),
            Parallel(
                branches={
                    "left": [Sequence(nodes=[Python(fn=identity)])],
                    "right": [Python(fn=identity)],
                }
            ),
            Python(fn=join),
            ForEach(
                items=Input.previous,
                nodes=[
                    Python(fn=identity),
                    Python(fn=preserve_item, inputs={"previous": Input.previous, "original": Input.item}),
                ],
            ),
        ],
    )
    bundle = compile_workflow(workflow)
    assert bundle["contract_version"] == "2"
    parallel = bundle["nodes"][1]
    assert parallel["max_concurrency"] == 4
    assert parallel["branches"]["left"][0]["nodes"][0]["inputs"]["text"] == {"from": "node_1", "output": "result"}
    assert parallel["branches"]["right"][0]["inputs"]["text"] == {"from": "node_1", "output": "result"}
    loop = bundle["nodes"][3]
    assert loop == {
        "id": "node_7",
        "type": "for_each",
        "items": {"from": "node_6", "output": "result"},
        "nodes": loop["nodes"],
        "max_items": 100,
        "max_concurrency": 4,
    }
    assert loop["nodes"][0]["inputs"]["text"] == {"from": "$item", "output": "result"}
    assert loop["nodes"][1]["inputs"] == {
        "previous": {"from": "node_8", "output": "result"},
        "original": {"from": "$item", "output": "result"},
    }
    assert bundle["output"] == {"from": "node_7", "output": "result"}
    fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("hybrid-v2.json").read_text())
    fixture["input_schema"] = TypeAdapter(list[ChatMessage]).json_schema()
    assert bundle == fixture


def test_nested_loop_item_scope_restores_after_inner_loop():
    bundle = compile_workflow(
        Workflow(
            name="nested",
            nodes=[
                Python(fn=nested_items),
                ForEach(
                    items=Input.previous,
                    max_items=2,
                    nodes=[
                        ForEach(items=Input.item, max_items=2, nodes=[Python(fn=identity)]),
                        Python(fn=preserve_outer, inputs={"previous": Input.previous, "original": Input.item}),
                    ],
                ),
            ],
        )
    )
    outer = bundle["nodes"][1]
    assert outer["nodes"][0]["items"] == {"from": "$item", "output": "result"}
    assert outer["nodes"][1]["inputs"]["original"] == {"from": "$item", "output": "result"}
    assert outer["nodes"][1]["inputs"]["previous"] == {"from": "node_3", "output": "result"}


def test_field_projection_uses_json_aliases_and_escapes_json_pointer():
    Aliased = create_model("Aliased", values=(list[str], Field(alias="a/b~c")))
    bundle = compile_workflow(
        Workflow(
            name="projected",
            nodes=[
                ChatCompletion(model="analysis-model", messages=Input.messages, response_model=Aliased),
                ForEach(items=Input.previous.field("a/b~c"), nodes=[Python(fn=identity)]),
            ],
        )
    )
    assert bundle["nodes"][1]["items"] == {"from": "node_1", "output": "result", "path": "/a~1b~0c"}
    assert bundle["contract_version"] == "2"


def test_field_projection_without_composite_selects_v2():
    bundle = compile_workflow(
        Workflow(
            name="field",
            nodes=[
                ChatCompletion(model="analysis-model", messages=Input.messages, response_model=Items),
                Python(
                    fn=preserve_outer,
                    inputs={"previous": Input.previous.field("items"), "original": Input.previous.field("items")},
                ),
            ],
        )
    )
    assert bundle["contract_version"] == "2"


@pytest.mark.parametrize("name", ["", "missing", "values"])
def test_missing_or_non_alias_fields_fail(name):
    with pytest.raises(ValueError, match="Field"):
        compile_workflow(
            Workflow(
                name="bad-field",
                nodes=[
                    ChatCompletion(model="analysis-model", messages=Input.messages, response_model=Items),
                    ForEach(items=Input.previous.field(name), nodes=[Python(fn=identity)]),
                ],
            )
        )


def test_item_reference_cannot_escape_its_loop():
    with pytest.raises(ValueError, match="only available inside"):
        compile_workflow(Workflow(name="bad-item", nodes=[Python(fn=identity, inputs={"text": Input.item})]))


def test_field_reference_requires_a_supported_input_root():
    with pytest.raises(ValueError, match="must start from"):
        compile_workflow(
            Workflow(
                name="bad-root",
                nodes=[Python(fn=identity, inputs={"text": FieldRef(source="previous", fields=("text",))})],
            )
        )


def test_parallel_schemas_keep_same_named_definitions_separate():
    TextDetail = create_model("Detail", value=(str, ...))
    NumberDetail = create_model("Detail", value=(int, ...))
    Left = create_model("Envelope", detail=(TextDetail, ...))
    Right = create_model("Envelope", detail=(NumberDetail, ...))
    bundle = compile_workflow(
        Workflow(
            name="definition-names",
            nodes=[
                Parallel(
                    branches={
                        "left": [ChatCompletion(model="analysis-model", messages=Input.messages, response_model=Left)],
                        "right": [
                            ChatCompletion(model="analysis-model", messages=Input.messages, response_model=Right)
                        ],
                    }
                ),
                Python(
                    fn=typed_values,
                    inputs={
                        "left": Input.previous.field("left").field("detail").field("value"),
                        "right": Input.previous.field("right").field("detail").field("value"),
                    },
                ),
            ],
        )
    )
    assert bundle["nodes"][1]["inputs"]["right"]["path"] == "/right/detail/value"


def test_parallel_known_values_cannot_bind_to_an_incompatible_mapping():
    with pytest.raises(ValueError, match="incompatible"):
        compile_workflow(
            Workflow(
                name="bad-mapping",
                nodes=[
                    ChatCompletion(model="analysis-model", messages=Input.messages),
                    Parallel(branches={"left": [Python(fn=identity)], "right": [Python(fn=identity)]}),
                    Python(fn=consume_mapping),
                ],
            )
        )


def test_known_tuple_items_cannot_bind_to_an_incompatible_list():
    with pytest.raises(ValueError, match="incompatible"):
        compile_workflow(Workflow(name="bad-tuple", nodes=[Python(fn=tuple_strings), Python(fn=consume_numbers)]))


def test_common_typed_list_field_can_be_projected_from_a_union():
    bundle = compile_workflow(
        Workflow(
            name="union-items",
            nodes=[
                Python(fn=union_items),
                ForEach(items=Input.previous.field("items"), nodes=[Python(fn=identity)]),
            ],
        )
    )
    assert bundle["nodes"][1]["items"]["path"] == "/items"


def test_unsupported_schema_ref_sibling_constraints_are_not_silently_discarded():
    with pytest.raises(ValueError, match="beside \\$ref are unsupported"):
        compile_workflow(
            Workflow(
                name="ref-siblings",
                nodes=[
                    ChatCompletion(model="analysis-model", messages=Input.messages, response_model=RefSiblings),
                    Python(fn=identity, inputs={"text": Input.previous.field("detail").field("title")}),
                ],
            )
        )


def test_condition_compiles_without_running_and_branches_receive_same_input():
    bundle = compile_workflow(
        Workflow(
            name="conditional",
            nodes=[
                ChatCompletion(model="analysis-model", messages=Input.messages),
                If(condition=predicate, then=[Python(fn=identity)], otherwise=[Python(fn=identity)]),
            ],
        )
    )
    conditional = bundle["nodes"][1]
    incoming = {"from": "node_1", "output": "result"}
    assert conditional["condition"]["inputs"] == {"text": incoming}
    assert conditional["condition"]["program"]["output_schema"] == {"type": "boolean"}
    assert conditional["then"][0]["inputs"] == {"text": incoming}
    assert conditional["otherwise"][0]["inputs"] == {"text": incoming}


@pytest.mark.parametrize("condition", [not_boolean, two_arguments])
def test_conditions_require_one_annotated_argument_and_bool_return(condition):
    with pytest.raises(ValueError, match="exactly one typed argument and return bool"):
        compile_workflow(
            Workflow(
                name="bad-condition",
                nodes=[
                    ChatCompletion(model="analysis-model", messages=Input.messages),
                    If(condition=condition, then=[Python(fn=identity)], otherwise=[Python(fn=identity)]),
                ],
            )
        )


def test_if_rejects_different_results_without_inventing_a_union():
    with pytest.raises(ValueError, match="same output contract"):
        compile_workflow(
            Workflow(
                name="bad-results",
                nodes=[
                    ChatCompletion(model="analysis-model", messages=Input.messages),
                    If(condition=predicate, then=[Python(fn=identity)], otherwise=[Python(fn=integer)]),
                ],
            )
        )


def test_if_accepts_structurally_equal_records_with_different_field_order():
    compile_workflow(
        Workflow(
            name="record-order",
            nodes=[
                ChatCompletion(model="analysis-model", messages=Input.messages),
                If(condition=predicate, then=[Python(fn=first_order)], otherwise=[Python(fn=other_order)]),
            ],
        )
    )


def test_if_accepts_same_explicit_union_on_both_exits():
    compile_workflow(
        Workflow(
            name="union-results",
            nodes=[
                ChatCompletion(model="analysis-model", messages=Input.messages),
                If(condition=predicate, then=[Python(fn=accepted)], otherwise=[Python(fn=review)]),
            ],
        )
    )


def test_unknown_output_is_not_accepted_as_a_constrained_input():
    with pytest.raises(ValueError, match="incompatible"):
        compile_workflow(
            Workflow(
                name="unknown-output",
                nodes=[
                    ChatCompletion(model="analysis-model", messages=Input.messages),
                    Python(fn=unknown),
                    Python(fn=consume_integer),
                ],
            )
        )


@pytest.mark.parametrize(
    "node",
    [
        Sequence(nodes=[]),
        Parallel(branches={}),
        Parallel(branches={"empty": []}),
        ForEach(items=Input.previous, nodes=[]),
        If(condition=predicate, then=[], otherwise=[Python(fn=identity)]),
    ],
)
def test_empty_groups_are_rejected(node):
    with pytest.raises(ValueError):
        compile_workflow(Workflow(name="empty", nodes=[Python(fn=make_items), node]))


@pytest.mark.parametrize(
    "node",
    [
        Parallel(branches={"bad/name": [Python(fn=identity)]}),
        Parallel(branches={"good": [Python(fn=identity)]}, max_concurrency=0),
        ForEach(items=Input.previous, nodes=[Python(fn=identity)], max_items=0),
        ForEach(items=Input.previous, nodes=[Python(fn=identity)], max_concurrency=True),
    ],
)
def test_branch_names_and_concurrency_limits_are_validated(node):
    with pytest.raises(ValueError):
        compile_workflow(Workflow(name="invalid", nodes=[Python(fn=make_items), node]))


def test_expansion_and_depth_are_bounded_before_execution():
    with pytest.raises(ValueError, match="expand beyond"):
        compile_workflow(
            Workflow(
                name="too-many",
                nodes=[
                    Python(fn=make_items),
                    ForEach(items=Input.previous, nodes=[Python(fn=identity)], max_items=10_000),
                ],
            )
        )
    node = Python(fn=identity)
    for _ in range(17):
        node = Sequence(nodes=[node])
    with pytest.raises(ValueError, match="nesting exceeds"):
        compile_workflow(Workflow(name="too-deep", nodes=[node]))


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_deadlines_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        compile_workflow(Workflow(name="bad-deadline", nodes=[Python(fn=make_items)], timeout_seconds=timeout))


def test_schema_reference_rebasing_preserves_literal_data():
    literal = {"$ref": "a literal, not a schema reference", "$defs": {"leave": "alone"}}
    schema = {
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/Thing"}},
        "$defs": {"Thing": {"type": "string", "default": literal, "examples": [literal]}},
    }
    properties, definitions = _embedded({"branch": schema})
    assert properties["branch"]["properties"]["value"]["$ref"] == "#/$defs/part_0_Thing"
    assert definitions["part_0_Thing"]["default"] == literal
    assert definitions["part_0_Thing"]["examples"] == [literal]


@pytest.mark.parametrize("name", ["email_feedback.py", "batch_email_analysis.py"])
def test_examples_compile_actual_v2_bundles_without_gateway_credentials(name):
    bundle = compile_workflow(_example(name).workflow)
    assert bundle["contract_version"] == "2"
    assert json.loads(json.dumps(bundle)) == bundle
    assert bundle_digest(bundle) == bundle_digest(compile_workflow(_example(name).workflow))
