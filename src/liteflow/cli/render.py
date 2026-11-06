import json
from collections.abc import Mapping
from typing import TextIO

from rich.console import Console
from rich.table import Column, Table
from rich.text import Text
from rich.tree import Tree

from liteflow import ChatCompletion, Python, RunSteps, Workflow
from liteflow.cli.sanitize import sanitize


def console(stream: TextIO, env: Mapping[str, str]) -> Console:
    terminal = stream.isatty() and "NO_COLOR" not in env
    return Console(
        file=stream, markup=False, emoji=False, highlight=False, force_terminal=terminal, no_color=not terminal
    )


def text(value: object) -> Text:
    return Text(sanitize(str(value)))


def fields(target: Console, values: Mapping[str, object]) -> None:
    width = max((len(sanitize(key)) for key in values), default=0)
    for key, value in values.items():
        target.print(text(f"{sanitize(key):<{width}}: {value}"))


def binding(value: object) -> str:
    if isinstance(value, dict) and "path" in value:
        raise ValueError("unsupported binding kind FieldRef")
    if isinstance(value, dict) and "from" in value:
        return f"{value['from']}.{value['output']}"
    return "literal " + json.dumps(value.get("value") if isinstance(value, dict) else value, ensure_ascii=True)


def show(target: Console, workflow: Workflow, bundle: Mapping[str, object]) -> None:
    for node in workflow.nodes:
        if not isinstance(node, (ChatCompletion, Python)):
            raise ValueError(f"unsupported node kind {type(node).__name__}")
    tree = Tree(text(workflow.name))
    for index, (node, compiled) in enumerate(zip(workflow.nodes, bundle["nodes"], strict=True), 1):
        label = (
            f"ChatCompletion model={node.model}"
            if isinstance(node, ChatCompletion)
            else f"Python fn={node.fn.__name__}"
        )
        branch = tree.add(text(f"node_{index} {label}"))
        bindings = {"messages": compiled["messages"]} if isinstance(node, ChatCompletion) else compiled["inputs"]
        for name, value in bindings.items():
            branch.add(text(f"{name}: {binding(value)}"))
        branch.add(
            text(
                f"timeout={node.timeout_seconds}s retry={node.retry.max_attempts} backoff={node.retry.backoff_seconds}s"
            )
        )
    target.print(tree)
    target.print(text(f"output: {binding(bundle['output'])}"))


def summary(error: str | None) -> str:
    if error is None:
        return "-"
    try:
        parsed = json.loads(error)
    except ValueError:
        parsed = error
    if isinstance(parsed, dict):
        parsed = parsed.get("message", parsed)
    return " ".join(sanitize(str(parsed)).split())[:160] or "-"


def inspect(target: Console, result: RunSteps, outputs: bool) -> None:
    target.print(text(f"run_id: {result.run_id}\nsnapshot_id: {result.snapshot_id}\nstate: {result.state.value}"))
    target.print(text(f"cancel_requested: {result.cancel_requested}"))
    table = Table(
        *(Column(text(name)) for name in ("node", "state", "attempts", "duration", "tokens", "cost", "error"))
    )
    for step in result.steps:
        table.add_row(
            *(
                text(value)
                for value in (
                    step.node_id,
                    step.state.value,
                    step.attempts,
                    "-" if step.duration is None else f"{step.duration.total_seconds():g}s",
                    f"{step.prompt_tokens}/{step.completion_tokens}",
                    "-" if step.response_cost is None else f"{step.response_cost:g}",
                    summary(step.error),
                )
            )
        )
    target.print(table)
    if outputs:
        for step in result.steps:
            target.print(text(f"{step.node_id} input:\n{json.dumps(step.input, indent=2, ensure_ascii=False)}"))
            target.print(text(f"{step.node_id} output:\n{json.dumps(step.output, indent=2, ensure_ascii=False)}"))
