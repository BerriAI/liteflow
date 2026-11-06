"""Proposed Liteflow SDK: classify one spreadsheet row per invocation.

The caller supplies one row in a user message and retains its row identifier.
Workbook ingestion and batch scheduling are outside this example.
"""

from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from liteflow import ChatCompletion, Input, Python, Workflow

Category = Literal["billing", "travel", "other"]


class RowClassification(BaseModel):
    category: Category
    confidence: float = Field(ge=0, le=1)


class RowResult(TypedDict):
    category: Category
    confidence: float
    status: Literal["classified", "review_required"]


def apply_review_threshold(result: RowClassification) -> RowResult:
    return {
        "category": result.category,
        "confidence": result.confidence,
        "status": "review_required" if result.confidence < 0.85 else "classified",
    }


workflow = Workflow(
    name="classify-row",
    nodes=[
        ChatCompletion(
            model="classification-model",
            messages=Input.messages,
            temperature=0,
            response_model=RowClassification,
        ),
        Python(fn=apply_review_threshold),
    ],
)
