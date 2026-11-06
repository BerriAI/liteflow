"""Compile per-email parallel analyses and a final batch report.

Model names are illustrative configured gateway aliases. Registration and
execution require the v2 executor. Run this file to print the compiled
definition without calling a model or reading gateway credentials.
"""

import json
from pydantic import BaseModel
from typing_extensions import TypedDict

from liteflow import ChatCompletion, ChatMessage, ForEach, Input, Parallel, Python, Sequence, Workflow, compile_workflow


class Emails(BaseModel):
    items: list[str]


class Finding(BaseModel):
    summary: str


class Analyses(TypedDict):
    intent: Finding
    sentiment: Finding


class EmailAnalysis(TypedDict):
    email: str
    intent: str
    sentiment: str


def prepare_analysis(email: str, instruction: str) -> list[ChatMessage]:
    return [{"role": "system", "content": instruction}, {"role": "user", "content": email}]


def combine_analysis(analyses: Analyses, email: str) -> EmailAnalysis:
    return {"email": email, "intent": analyses["intent"].summary, "sentiment": analyses["sentiment"].summary}


def prepare_report(analyses: list[EmailAnalysis]) -> list[ChatMessage]:
    lines = [item["email"] + "\nIntent: " + item["intent"] + "\nSentiment: " + item["sentiment"] for item in analyses]
    return [{"role": "user", "content": "Summarize these email analyses:\n\n" + "\n\n".join(lines)}]


workflow = Workflow(
    name="batch-email-analysis",
    nodes=[
        ChatCompletion(model="support-model", messages=Input.messages, response_model=Emails),
        ForEach(
            items=Input.previous.field("items"),
            max_items=100,
            max_concurrency=4,
            nodes=[
                Parallel(
                    branches={
                        "intent": [
                            Python(
                                fn=prepare_analysis,
                                inputs={"email": Input.item, "instruction": "Identify the request in this email."},
                            ),
                            ChatCompletion(model="haiku", messages=Input.previous, response_model=Finding),
                        ],
                        "sentiment": [
                            Python(
                                fn=prepare_analysis,
                                inputs={"email": Input.item, "instruction": "Assess the customer's sentiment."},
                            ),
                            ChatCompletion(model="haiku", messages=Input.previous, response_model=Finding),
                        ],
                    },
                    max_concurrency=2,
                ),
                Python(fn=combine_analysis, inputs={"analyses": Input.previous, "email": Input.item}),
            ],
        ),
        Sequence(
            nodes=[
                Python(fn=prepare_report),
                ChatCompletion(model="support-model", messages=Input.previous, timeout_seconds=35),
            ]
        ),
    ],
)


if __name__ == "__main__":
    print(json.dumps(compile_workflow(workflow), indent=2))
