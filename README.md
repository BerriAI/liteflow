# liteflow

code-first workflows for the LiteLLM AI Gateway

![Liteflow workflow defined in Python with LLM calls, Python transforms, and an if/else branch](docs/assets/liteflow-workflow.gif)

chain LLM calls and typed Python functions. each node passes its result to the next:

```python
from liteflow import ChatCompletion, ChatMessage, Input, Python, Workflow


def review_messages(draft: str) -> list[ChatMessage]:
    return [{"role": "user", "content": "Review and improve this reply: " + draft}]


workflow = Workflow(name="email-reply", nodes=[
    ChatCompletion(model="sonnet", messages=Input.messages),
    Python(fn=review_messages),
    ChatCompletion(model="haiku", messages=Input.previous),
])
```

Sonnet drafts a reply. Python turns that draft into review messages. Haiku reads those messages and returns the final reply.

## Register and run

install from a source checkout with `python -m pip install -e .`. replace `sonnet` and `haiku` with your gateway's model aliases.

set `LITELLM_PROXY_URL` to your gateway root URL without `/v1`, and keep your key in `LITELLM_PROXY_API_KEY`. register the workflow:

```python
import os

from liteflow import GatewayClient

gateway = GatewayClient(
    base_url=os.environ["LITELLM_PROXY_URL"],
    api_key=os.environ["LITELLM_PROXY_API_KEY"],
)
draft = gateway.register(workflow)
```

ask a separate authorized reviewer to publish the draft. then run it and read the result:

```python
result = gateway.run(workflow.name, messages=[
    {"role": "system", "content": "Write a short customer-service reply."},
    {"role": "user", "content": "How can I update my account email?"},
])
print(result.output)
gateway.close()
```

`run()` uses Chat Completions with `model="workflow/email-reply"`. `Input.messages` is the original request; `Input.previous` is the previous node's result. use `result.run_id` to inspect the run.

## Advanced workflows

the node list above is sequential. use these patterns when you need more control:

- **Parallel:** run named branches together and collect their results.
- **Loop:** use `ForEach` to process each item in a list.
- **Conditional:** use `If` to choose a branch from a structured result.
- **Hybrid:** nest these patterns inside one another.

graph definitions compile with this SDK; gateway graph execution is still in development.

```python
from typing import Literal

from pydantic import BaseModel

from liteflow import ForEach, If, Parallel
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

see [graph workflows](docs/graph-workflows.md) for input bindings, limits and node settings.

*Workflow design preview.*

## About

use liteflow for work you can define in advance: process an email, classify a spreadsheet row, or route a request for review.

see the [gateway API](docs/gateway-api.md), [Python execution contract](docs/execution.md), and [implementation plan](docs/implementation-plan.md).

## License

[MIT](LICENSE)

## Contributing

open an issue or pull request. start with the [workflow examples](examples/) and [implementation plan](docs/implementation-plan.md).
