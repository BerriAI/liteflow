"""Compile a batch email workflow with a loop, condition, and parallel calls.

Model names are illustrative gateway aliases. Running this file compiles
the definition without credentials or model calls. Execution requires a
gateway with support for the graph contract.
"""

import json
from typing import Literal

from pydantic import BaseModel

from liteflow import (
    ChatCompletion,
    ChatMessage,
    ForEach,
    If,
    Input,
    Parallel,
    Python,
    Retry,
    Workflow,
    compile_workflow,
)

Category = Literal["feedback", "dissatisfied", "happy"]


class Emails(BaseModel):
    items: list[str]


class Classification(BaseModel):
    category: Category


class Actions(BaseModel):
    action_items: list[str]


class EmailResult(BaseModel):
    email: str
    category: Category
    report: str | None


def prompt(email: str, instruction: str) -> list[ChatMessage]:
    return [{"role": "system", "content": instruction}, {"role": "user", "content": email}]


def is_dissatisfied(result: Classification) -> bool:
    return result.category == "dissatisfied"


def make_report(email: str, summary: str, actions: Actions) -> EmailResult:
    report = summary + "\n\nAction items:\n" + "\n".join(actions.action_items)
    return EmailResult(email=email, category="dissatisfied", report=report)


def no_report(email: str, classification: Classification) -> EmailResult:
    return EmailResult(email=email, category=classification.category, report=None)


def haiku_analysis(instruction: str, response_model: type[BaseModel] | None = None) -> list[Python | ChatCompletion]:
    return [
        Python(fn=prompt, inputs={"email": Input.item, "instruction": instruction}),
        ChatCompletion(model="haiku", messages=Input.previous, response_model=response_model, timeout_seconds=30),
    ]


workflow = Workflow(
    name="email-processing",
    nodes=[
        ChatCompletion(model="support-model", messages=Input.messages, response_model=Emails),
        ForEach(
            items=Input.previous.field("items"),
            max_items=100,
            max_concurrency=4,
            nodes=[
                Python(
                    fn=prompt,
                    inputs={"email": Input.item, "instruction": "Classify as feedback, dissatisfied, or happy."},
                ),
                ChatCompletion(
                    model="support-model",
                    messages=Input.previous,
                    response_model=Classification,
                    timeout_seconds=35,
                    retry=Retry(max_attempts=2, backoff_seconds=1),
                ),
                If(
                    condition=is_dissatisfied,
                    then=[
                        Parallel(
                            max_concurrency=2,
                            branches={
                                "summary": haiku_analysis("Summarize this customer complaint."),
                                "actions": haiku_analysis("List action items for this complaint.", Actions),
                            },
                        ),
                        Python(
                            fn=make_report,
                            inputs={
                                "email": Input.item,
                                "summary": Input.previous.field("summary"),
                                "actions": Input.previous.field("actions"),
                            },
                        ),
                    ],
                    otherwise=[Python(fn=no_report, inputs={"email": Input.item, "classification": Input.previous})],
                ),
            ],
        ),
    ],
)


if __name__ == "__main__":
    print(json.dumps(compile_workflow(workflow), indent=2))
