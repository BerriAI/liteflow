# liteflow

code-first workflows for the LiteLLM AI Gateway

write your workflow in Python, run it on your gateway, and read the result.

## Features

- LLM calls with standard Chat Completions arguments
- Python functions with typed inputs and outputs
- structured JSON from Pydantic schemas
- sequential, parallel, conditional and per-item workflow definitions
- one client to register, run, and inspect workflows

## Installation

install from a source checkout:

```sh
python -m pip install -e .
```

gateway execution requires the [workflow runner](https://github.com/BerriAI/litellm/pull/50376) and [Python execution adapter](https://github.com/BerriAI/litellm/pull/50327). graph authoring in this branch compiles locally; the gateway graph runner is still in development. the sequential runner does not execute graph definitions.

## Usage

make a workflow that drafts an email reply and adds a signature:

```python
import os

from liteflow import ChatCompletion, GatewayClient, Input, Python, Workflow


def add_signature(reply: str) -> str:
    return reply.strip() + "\n\nCustomer Care"


workflow = Workflow(
    name="email-reply",
    nodes=[
        ChatCompletion(model="support-model", messages=Input.messages),
        Python(fn=add_signature),
    ],
)

gateway = GatewayClient(
    base_url=os.environ["LITELLM_PROXY_URL"],
    api_key=os.environ["LITELLM_PROXY_API_KEY"],
)
draft = gateway.register(workflow)
```

`Input.messages` refers to the messages you pass when you run the workflow. the model returns a string, and `add_signature` takes that string as its input. write nodes in execution order.

set `LITELLM_PROXY_URL` to your gateway URL without `/v1`. use a key with workflow registration and invocation access, and replace `support-model` with a configured gateway model alias.

ask an admin to review and publish the draft, then run it:

```python
result = gateway.run(
    workflow.name,
    messages=[
        {"role": "system", "content": "Write a short customer-service reply."},
        {"role": "user", "content": "How can I update my account email?"},
    ],
)
print(result.output)
gateway.close()
```

`result.output` contains the final reply, including the signature. use `result.run_id` to inspect the run. `run()` calls the gateway's Chat Completions API and executes the published workflow. publish a new draft after changing its definition.

## Workflow patterns

keep nodes in a list. put another workflow pattern inside that list when you need branches or per-item work:

| Pattern | Definition | Output passed to the next node |
| --- | --- | --- |
| Sequential | `Workflow(nodes=[...])` | the last node's result |
| Nested sequence | `Sequence(nodes=[...])` | the last child result |
| Parallel | `Parallel(branches={"summary": [...], "risk": [...]})` | an object keyed by branch name |
| Per-item loop | `ForEach(items=Input.previous.field("items"), nodes=[...])` | results in the original item order |
| Conditional | `If(condition=needs_review, then=[...], otherwise=[...])` | the selected branch's result |

combine these patterns to build a hybrid workflow. you use the same `gateway.register(workflow)` and `gateway.run(workflow.name, messages=...)` calls for each pattern.

full examples with typed functions and model responses:

- [email feedback](examples/email_feedback.py): classify an email as feedback, dissatisfied or happy; send dissatisfied emails to Haiku for a summary, action items and a report
- [batch email analysis](examples/batch_email_analysis.py): extract a list, process each email, run parallel analyses, and combine the results into a report
- [spreadsheet classification](examples/spreadsheet_classification.py): classify one row and apply a Python review threshold

## Inputs and outputs

use `response_model=YourPydanticModel` for structured LLM output. Python function annotations define the next step's input and output contracts.

- `Input.messages`: the original invocation messages
- `Input.previous`: the previous node's result; a branch starts with its parent's incoming value
- `Input.previous.field("items")`: a field from a structured result
- `Input.item`: the original item inside the nearest `ForEach`, even after another node changes `Input.previous`

a Python function with one argument receives `Input.previous`. use `inputs={...}` when it needs several values, such as the classification and original email. you do not repeat input/output schemas in the node definition.

set `max_items` and `max_concurrency` on `ForEach`; set `max_concurrency` on `Parallel`. both `If` exits return the same output contract. see [graph workflows](docs/graph-workflows.md) for definitions, limits and nesting.

leaf calls keep their existing timeout and retry settings:

```python
from liteflow import Retry

ChatCompletion(
    model="support-model",
    messages=Input.messages,
    timeout_seconds=40,
    retry=Retry(max_attempts=2, backoff_seconds=1),
)
```

the timeout covers the node's attempts and backoff. per-node fallback model aliases are a separate gateway follow-up.

## About

use liteflow for work you can define in advance: process an email, classify a spreadsheet row, or route a request for review.

see the [gateway API](docs/gateway-api.md), [Python execution contract](docs/execution.md), and [implementation plan](docs/implementation-plan.md).

## License

[MIT](LICENSE)

## Contributing

open an issue or pull request. start with the [workflow examples](examples/) and [implementation plan](docs/implementation-plan.md).

