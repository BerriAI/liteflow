# liteflow

code-first workflows for the LiteLLM AI Gateway

![Liteflow workflow defined in Python with LLM calls, Python transforms, and an if/else branch](docs/assets/liteflow-workflow.gif)

*Workflow design preview.*

write your workflow in Python, run it on your gateway, and read the result.

## Workflow types

- **Sequential:** run nodes in order, passing each result to the next node.
- **Parallel:** run named branches together and collect their results.
- **Loop:** use `ForEach` to run the same steps for each item in a list.
- **Conditional:** use `If` to choose a branch from a structured result.
- **Hybrid:** combine these patterns in one workflow.

## Installation

install from a source checkout:

```sh
python -m pip install -e .
```

these examples compile with this SDK. gateway graph execution is still in development.

## Examples

use these imports for the examples below. replace `sonnet` and `haiku` with your gateway's model aliases.

```python
from typing import Literal

from pydantic import BaseModel

from liteflow import ChatCompletion, ChatMessage, ForEach, If, Input, Parallel, Python, Workflow
```

### Sequential

generate a reply, then pass it to a typed Python function:

```python
def add_signature(reply: str) -> str:
    return reply + "\n\nCustomer Care"


workflow = Workflow(name="email-reply", nodes=[
    ChatCompletion(model="sonnet", messages=Input.messages),
    Python(fn=add_signature),
])
```

### Parallel

ask two models the same question. the result contains `sonnet` and `haiku` responses:

```python
workflow = Workflow(name="compare-models", nodes=[
    Parallel(branches={
        "sonnet": [ChatCompletion(model="sonnet", messages=Input.messages)],
        "haiku": [ChatCompletion(model="haiku", messages=Input.messages)],
    }),
])
```

### Loop

extract a list of emails, then summarize each one:

```python
class Emails(BaseModel):
    items: list[str]


def summary_messages(email: str) -> list[ChatMessage]:
    return [{"role": "user", "content": "Summarize this email: " + email}]


workflow = Workflow(name="email-summaries", nodes=[
    ChatCompletion(model="sonnet", messages=Input.messages, response_model=Emails),
    ForEach(items=Input.previous.field("items"), nodes=[
        Python(fn=summary_messages),
        ChatCompletion(model="haiku", messages=Input.previous),
    ]),
])
```

`ForEach` passes one item to its body and returns results in the original order.

### Conditional

classify an email. send dissatisfied emails to Haiku; return the category for the others:

```python
class Classification(BaseModel):
    category: Literal["feedback", "dissatisfied", "happy"]


def is_dissatisfied(result: Classification) -> bool:
    return result.category == "dissatisfied"


def keep_category(result: Classification) -> str:
    return result.category


workflow = Workflow(name="email-triage", nodes=[
    ChatCompletion(model="sonnet", messages=Input.messages, response_model=Classification),
    If(condition=is_dissatisfied,
       then=[ChatCompletion(model="haiku", messages=Input.messages)],
       otherwise=[Python(fn=keep_category)]),
])
```

### Hybrid

combine the loop and parallel patterns: get two model summaries for each email. this reuses `Emails` and `summary_messages` above.

```python
workflow = Workflow(name="compare-email-summaries", nodes=[
    ChatCompletion(model="sonnet", messages=Input.messages, response_model=Emails),
    ForEach(items=Input.previous.field("items"), nodes=[
        Python(fn=summary_messages),
        Parallel(branches={
            "sonnet": [ChatCompletion(model="sonnet", messages=Input.previous)],
            "haiku": [ChatCompletion(model="haiku", messages=Input.previous)],
        }),
    ]),
])
```

## Register and run

on a gateway supporting the selected workflow type, use the same client below. set `LITELLM_PROXY_URL` to the gateway root URL without `/v1`, and keep your key in `LITELLM_PROXY_API_KEY`.

```python
import os

from liteflow import GatewayClient

gateway = GatewayClient(
    base_url=os.environ["LITELLM_PROXY_URL"],
    api_key=os.environ["LITELLM_PROXY_API_KEY"],
)
draft = gateway.register(workflow)
```

ask a separate authorized reviewer to publish the draft. then run it:

```python
result = gateway.run(workflow.name, messages=[
    {"role": "user", "content": "Summarize these emails: My refund is late. Thanks for your help!"},
])
print(result.output)
gateway.close()
```

`run()` uses Chat Completions with `model="workflow/<name>"`. use `result.run_id` to inspect the run.

see [graph workflows](docs/graph-workflows.md) for input bindings, limits and node settings, or browse the [full examples](examples/).

## About

use liteflow for work you can define in advance: process an email, classify a spreadsheet row, or route a request for review.

see the [gateway API](docs/gateway-api.md), [Python execution contract](docs/execution.md), and [implementation plan](docs/implementation-plan.md).

## License

[MIT](LICENSE)

## Contributing

open an issue or pull request. start with the [workflow examples](examples/) and [implementation plan](docs/implementation-plan.md).
