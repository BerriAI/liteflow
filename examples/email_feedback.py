"""Classify feedback and produce a report only for dissatisfied customers.

Model names are illustrative gateway aliases. Registration and execution
require the v2 executor. Run this file to print the compiled definition;
compilation makes no model calls and needs no gateway credentials.
"""

import json
from typing import Literal

from pydantic import BaseModel

from liteflow import ChatCompletion, ChatMessage, If, Input, Python, Retry, Workflow, compile_workflow

Category = Literal["feedback", "dissatisfied", "happy"]


class EmailClassification(BaseModel):
    category: Category


class FeedbackReport(BaseModel):
    summary: str
    action_items: list[str]
    report: str


class EmailResult(BaseModel):
    category: Category
    dissatisfied_report: FeedbackReport | None


def prepare_classification(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [{"role": "system", "content": "Classify this email as feedback, dissatisfied, or happy."}] + messages


def is_dissatisfied(classification: EmailClassification) -> bool:
    return classification.category == "dissatisfied"


def prepare_report(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [
        {"role": "system", "content": "For this dissatisfied customer, produce a summary, action items, and a report."}
    ] + messages


def attach_dissatisfied_report(report: FeedbackReport) -> EmailResult:
    return EmailResult(category="dissatisfied", dissatisfied_report=report)


def no_report(classification: EmailClassification) -> EmailResult:
    return EmailResult(category=classification.category, dissatisfied_report=None)


workflow = Workflow(
    name="email-feedback",
    nodes=[
        Python(fn=prepare_classification),
        ChatCompletion(model="support-model", messages=Input.previous, response_model=EmailClassification),
        If(
            condition=is_dissatisfied,
            then=[
                Python(fn=prepare_report, inputs={"messages": Input.messages}),
                ChatCompletion(
                    model="haiku",
                    messages=Input.previous,
                    response_model=FeedbackReport,
                    timeout_seconds=35,
                    retry=Retry(max_attempts=2, backoff_seconds=0.5),
                ),
                Python(fn=attach_dissatisfied_report),
            ],
            otherwise=[Python(fn=no_report)],
        ),
    ],
)


if __name__ == "__main__":
    print(json.dumps(compile_workflow(workflow), indent=2))
