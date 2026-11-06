"""Proposed Liteflow SDK: draft an email reply, then add a signature.

This file documents the SDK contract. The liteflow package and gateway runner
are not implemented in this repository.
"""

from liteflow import ChatCompletion, Input, Python, Workflow


def add_signature(text: str) -> str:
    return text.strip() + "\n\nCustomer Care"


workflow = Workflow(
    name="email-reply",
    nodes=[
        ChatCompletion(model="support-model", messages=Input.messages),
        Python(fn=add_signature),
    ],
)
