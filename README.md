# liteflow

code-first workflows for the LiteLLM AI Gateway

![Liteflow workflow defined in Python with LLM calls, Python transforms, and an if/else branch](docs/assets/liteflow-workflow.gif)

*Workflow design preview.*

write your workflow in Python, run it on your gateway, and read the result.

## Features

- LLM calls with standard Chat Completions arguments
- Python functions with typed inputs and outputs
- structured JSON from Pydantic schemas
- one client to register, run, and inspect workflows

## Installation

we're building the SDK and gateway runner. follow the [SDK PR](https://github.com/BerriAI/liteflow/pull/3) for the current code; we haven't released a package.

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

more workflow designs:

- [customer emails](examples/email_processing.py): three parallel analyses, then draft, check, and finalize
- [spreadsheet classification](examples/spreadsheet_classification.py): typed JSON and a Python review threshold
- [expense intake](examples/expense_review.py): extract fields and choose an if/else branch

## About

use liteflow for work you can define in advance: process an email, classify a spreadsheet row, or route a request for review.

see the [gateway API](docs/gateway-api.md), [Python execution contract](docs/execution.md), and [implementation plan](docs/implementation-plan.md).

## License

[MIT](LICENSE)

## Contributing

open an issue or pull request. start with the [workflow examples](examples/) and [implementation plan](docs/implementation-plan.md).
