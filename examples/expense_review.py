"""Proposed Liteflow SDK: extract an expense and choose a processing route.

The output tells the caller which route to use; this workflow makes no payment
or approval API calls.
"""

from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from liteflow import ChatCompletion, If, Input, Python, Workflow


class Expense(BaseModel):
    amount_cents: int = Field(ge=0)
    currency: str
    receipt_present: bool


class ExpenseResult(TypedDict):
    status: Literal["review_required", "ready_for_processing"]
    amount_cents: int
    currency: str


def normalize_expense(expense: Expense) -> Expense:
    return Expense(
        amount_cents=expense.amount_cents,
        currency=expense.currency.strip().upper(),
        receipt_present=expense.receipt_present,
    )


def requires_review(expense: Expense) -> bool:
    if not expense.receipt_present:
        return True
    if expense.currency != "USD":
        return True
    return expense.amount_cents > 10_000


def review_required(expense: Expense) -> ExpenseResult:
    return {
        "status": "review_required",
        "amount_cents": expense.amount_cents,
        "currency": expense.currency,
    }


def ready_for_processing(expense: Expense) -> ExpenseResult:
    return {
        "status": "ready_for_processing",
        "amount_cents": expense.amount_cents,
        "currency": expense.currency,
    }


workflow = Workflow(
    name="expense-review",
    nodes=[
        ChatCompletion(
            model="extraction-model",
            messages=Input.messages,
            response_model=Expense,
        ),
        Python(fn=normalize_expense),
        If(
            condition=requires_review,
            then=[Python(fn=review_required)],
            otherwise=[Python(fn=ready_for_processing)],
        ),
    ],
)
