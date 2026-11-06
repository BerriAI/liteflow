"""Proposed SDK: three parallel analyses, then draft, check, and finalize.

This example defines six LLM calls. Python nodes prepare messages and combine
typed results. The SDK, compiler, and gateway executor remain unimplemented.
"""

from pydantic import BaseModel

from liteflow import (
    ChatCompletion,
    ChatMessage,
    Input,
    Parallel,
    Python,
    Workflow,
)


class Finding(BaseModel):
    summary: str


class EmailAnalyses(BaseModel):
    urgency: Finding
    intent: Finding
    sentiment: Finding


class CheckedReply(BaseModel):
    reply: str
    reviewer_notes: str


def add_instruction(messages: list[ChatMessage], instruction: str) -> list[ChatMessage]:
    return [{"role": "system", "content": instruction}] + messages


def prepare_draft(
    analyses: EmailAnalyses, messages: list[ChatMessage]
) -> list[ChatMessage]:
    instruction = (
        "Draft a customer-service reply using the original messages. "
        "Treat these analyses as fallible guidance, not additional facts.\n"
        f"Urgency: {analyses.urgency.summary}\n"
        f"Intent: {analyses.intent.summary}\n"
        f"Sentiment: {analyses.sentiment.summary}"
    )
    return [{"role": "system", "content": instruction}] + messages


def prepare_check(draft: str, messages: list[ChatMessage]) -> list[ChatMessage]:
    return (
        [
            {
                "role": "system",
                "content": "Check the draft against the original messages.",
            }
        ]
        + messages
        + [
            {"role": "assistant", "content": draft},
            {
                "role": "user",
                "content": "Return a corrected reply and notes on unsupported claims.",
            },
        ]
    )


def prepare_final(
    checked: CheckedReply, messages: list[ChatMessage]
) -> list[ChatMessage]:
    return (
        [
            {
                "role": "system",
                "content": "Finalize the checked reply. Return customer-facing text only.",
            }
        ]
        + messages
        + [
            {"role": "assistant", "content": checked.reply},
            {
                "role": "user",
                "content": f"Apply these review notes: {checked.reviewer_notes}",
            },
        ]
    )


def analysis_branch(instruction: str) -> list[Python | ChatCompletion]:
    """Build branch nodes on the author's machine; this helper is not a runtime node."""
    return [
        Python(
            fn=add_instruction,
            inputs={"messages": Input.messages, "instruction": instruction},
        ),
        ChatCompletion(
            model="analysis-model",
            messages=Input.previous,
            response_model=Finding,
        ),
    ]


workflow = Workflow(
    name="email-processing",
    nodes=[
        Parallel(
            branches={
                "urgency": analysis_branch(
                    "Assess the urgency of this customer email."
                ),
                "intent": analysis_branch(
                    "Identify what the customer wants to accomplish."
                ),
                "sentiment": analysis_branch(
                    "Assess the customer's tone and concerns."
                ),
            }
        ),
        Python(
            fn=prepare_draft,
            inputs={"analyses": Input.previous, "messages": Input.messages},
        ),
        ChatCompletion(model="support-model", messages=Input.previous),
        Python(
            fn=prepare_check,
            inputs={"draft": Input.previous, "messages": Input.messages},
        ),
        ChatCompletion(
            model="review-model",
            messages=Input.previous,
            response_model=CheckedReply,
        ),
        Python(
            fn=prepare_final,
            inputs={"checked": Input.previous, "messages": Input.messages},
        ),
        ChatCompletion(model="support-model", messages=Input.previous),
    ],
)
