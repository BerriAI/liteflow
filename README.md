# Liteflow

A code-first way to build workflows with the LiteLLM AI Gateway.

Define model calls, Python transformations, and conditional branches in a typed Python SDK. Register a workflow with your gateway, then invoke it through Chat Completions.

**SDK contract in development.** This repository contains the proposed SDK interface, examples, and gateway contract. It does not yet contain an installable SDK or a workflow runner for LiteLLM.

## An email reply in two nodes

```python
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
```

```text
Chat Completions request → model draft → Python signature → assistant response
```

Use a configured gateway model alias for `support-model`. The caller supplies the drafting instructions and customer email in its messages. See the [complete example](examples/email_reply.py).

## Read the workflow

The SDK contract uses these rules:

- `Workflow(nodes=[...])` runs nodes in list order. The list carries the previous node's typed result between stages.
- `Input.messages` refers to the caller's original Chat Completions messages. `Input.previous` refers to the current result when a node needs an explicit binding.
- `ChatCompletion` accepts familiar fields such as `model`, `messages`, and `temperature`. Its default result is a string. `response_model=MyModel` declares a structured result with a Pydantic schema.
- `Python(fn=transform)` uses the function's parameter and return annotations as its schema contract. A unary function receives the previous result.
- `Parallel(branches={"name": [...]})` runs named branches concurrently. Each branch has a sequential node list; the join returns their named, typed results.
- `If(condition=predicate, then=[...], otherwise=[...])` evaluates a typed function that returns `bool`. Only the selected branch runs, starting with the same input. Its last result becomes the `If` result.

For a model call after Python prepares messages, bind the result explicitly:

```python
Python(fn=prepare_messages),  # returns list[ChatMessage]
ChatCompletion(model="support-model", messages=Input.previous),
```

Authors use record types to describe data. The planned compiler converts supported records into sandbox-safe data definitions; it does not run Pydantic or arbitrary application packages inside Python nodes. See the [execution contract](docs/execution.md).

## Three parallel calls, then three sequential calls

The [email-processing example](examples/email_processing.py) analyzes urgency, intent, and sentiment in parallel. Python combines those typed findings with the original messages, then prepares each subsequent model call:

```text
         ┌─ urgency ───┐
messages ├─ intent ────┼─ Python join → draft → check → finalize → reply
         └─ sentiment ┘
```

The example uses `Parallel(branches={...})` for concurrency and `Input.previous` for sequential results. Its Python functions use explicit `inputs` when they need both a prior result and the caller's original messages.

## Register, publish, invoke

The planned SDK separates uploading a draft from publishing it:

```python
import os

from liteflow import GatewayClient

gateway = GatewayClient(
    base_url=os.environ["LITELLM_PROXY_URL"],
    api_key=os.environ["LITELLM_PROXY_API_KEY"],
)
gateway.register(workflow)
```

`register()` compiles the graph, schemas, and supported Python source into a bundle. The gateway validates the bundle and stores a draft. An authorized publisher activates a reviewed draft through the separate publication API. See the [gateway API contract](docs/gateway-api.md).

Once a workflow is published, the planned invocation uses the existing OpenAI client interface:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url=os.environ["LITELLM_PROXY_URL"].rstrip("/") + "/v1",
    api_key=os.environ["LITELLM_PROXY_API_KEY"],
)
response = client.chat.completions.create(
    model="workflow/email-reply",
    messages=[
        {"role": "system", "content": "Write a short customer-service reply."},
        {"role": "user", "content": "How can I update the email address on my account?"},
    ],
)
print(response.choices[0].message.content)
```

`LITELLM_PROXY_URL` is your gateway's base URL without `/v1`. Store credentials outside workflow files. A string result becomes the assistant message content; the gateway serializes a structured result as JSON content. The invocation route above remains a proposed gateway addition.

## Production application examples

These examples describe application use cases for the proposed interface. They do not represent deployed workflows.

| Application | Workflow | Example |
| --- | --- | --- |
| Customer email processing | Analyze in parallel, then draft, check, and finalize a reply. | [Email processing](examples/email_processing.py) |
| Spreadsheet row classification | Produce a typed category and confidence, then route uncertain rows for review. | [Row classification](examples/spreadsheet_classification.py) |
| Expense intake | Extract fields, normalize currency, and select a review or processing route. | [Expense review](examples/expense_review.py) |

The row example handles one row per request. Your application retains row identifiers and schedules the workbook's requests. Cost estimates, sampling, and workbook ingestion require separate product work.

The expense example returns a routing decision for the application. It does not approve or send payments.

## Implementation scope

P0 covers the typed SDK/compiler, workflow registration and publication, Chat Completions invocation, node-local retries and deadlines, Monty execution for pure Python, and run inspection. The gateway implementation must persist node results and expose errors before these examples can run end to end.

The terminal experience should let developers validate, visualize, invoke, and inspect workflows from Claude Code, Codex, or a shell. API and MCP clients should use the same workflow definitions and execution service. Native retrieval connectors, execution cost estimates, feedback/evals, and advanced caching remain follow-up work.

Read the [implementation plan](docs/implementation-plan.md) for workstreams and release gates. The [gateway API contract](docs/gateway-api.md) and [execution contract](docs/execution.md) define the implementation boundaries and verification gaps.
