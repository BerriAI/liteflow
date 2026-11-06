import hashlib
import json

import pytest
from pydantic import BaseModel, Field

from liteflow import ChatCompletion, ChatMessage, Input, Python, Workflow, bundle_digest, compile_workflow


class DraftText(BaseModel):
    text: str
    priority: int = Field(ge=0)


def prepare_followup(text: str) -> list[ChatMessage]:
    return [{"role": "user", "content": "Review this draft: " + text}]


def prepare_record(draft: DraftText, messages: list[ChatMessage]) -> list[ChatMessage]:
    return messages + [{"role": "user", "content": draft.text}]


def test_compile_preserves_original_messages_and_predecessor_bindings():
    workflow = Workflow(
        name="email-reply",
        nodes=[
            ChatCompletion(model="support-model", messages=Input.messages),
            Python(fn=prepare_followup),
            ChatCompletion(model="support-model", messages=Input.previous, temperature=0),
        ],
    )
    bundle = compile_workflow(workflow)
    nodes = bundle["nodes"]
    assert nodes[0]["messages"] == {"from": "$input", "output": "messages"}
    assert nodes[1]["inputs"] == {"text": {"from": "node_1", "output": "result"}}
    assert nodes[2]["messages"] == {"from": "node_2", "output": "result"}
    assert nodes[2]["temperature"] == 0
    program = nodes[1]["program"]
    assert program["source_sha256"] == hashlib.sha256(program["source"].encode()).hexdigest()
    assert program["input_schema"]["properties"]["text"]["type"] == "string"
    assert program["output_schema"]["type"] == "array"
    assert bundle_digest(bundle) == bundle_digest(compile_workflow(workflow))
    assert json.loads(json.dumps(bundle)) == bundle


def test_record_arguments_are_data_schemas_not_uploaded_classes():
    bundle = compile_workflow(
        Workflow(
            name="structured-reply",
            nodes=[
                ChatCompletion(model="support-model", messages=Input.messages, response_model=DraftText),
                Python(fn=prepare_record, inputs={"draft": Input.previous, "messages": Input.messages}),
            ],
        )
    )
    nodes = bundle["nodes"]
    assert nodes[0]["response_format"]["json_schema"]["schema"]["properties"]["priority"]["minimum"] == 0
    program = nodes[1]["program"]
    assert program["parameter_names"] == ["draft", "messages"]
    assert program["input_schema"]["$defs"]["DraftText"]["x-liteflow-record"] is True
    assert "class DraftText" not in program["source"]
    assert nodes[1]["inputs"] == {
        "draft": {"from": "node_1", "output": "result"},
        "messages": {"from": "$input", "output": "messages"},
    }


def test_multi_argument_functions_require_exact_bindings():
    with pytest.raises(ValueError, match="multi-argument Python requires inputs"):
        compile_workflow(Workflow(name="bad-binding", nodes=[Python(fn=prepare_record)]))


def test_captured_functions_are_rejected():
    prefix = "captured"

    def captured(text: str) -> str:
        return prefix + text

    with pytest.raises(ValueError, match="closures are unsupported"):
        compile_workflow(Workflow(name="capture", nodes=[Python(fn=captured)]))
