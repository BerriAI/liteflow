# liteflow

code-first workflows for the LiteLLM AI Gateway

write a workflow as a list of LLM calls and Python functions. register it with your gateway, then call it through Chat Completions.

## Features

- LLM calls with standard Chat Completions arguments
- Python functions with typed inputs and outputs
- structured JSON, parallel calls, and if/else branches
- invocation through the OpenAI client

## Installation

we're building the SDK and gateway runner. the examples below show the proposed API; we haven't released a package.

## Usage

draft a customer email, then add a signature:

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
gateway.register(workflow)
```

`Input.messages` refers to your request messages. the model returns a string, and `add_signature` takes that string as its input. write nodes in execution order; use the function's annotations to define its input and output.

set `LITELLM_PROXY_URL` to your gateway URL without `/v1` and `LITELLM_PROXY_API_KEY` to a key with workflow registration and invocation access. use a configured gateway model alias for `support-model`.

`register()` saves a draft. a separate publisher reviews and publishes it before you invoke it:

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
        {"role": "user", "content": "How can I update my account email?"},
    ],
)
print(response.choices[0].message.content)
```

use `workflow/<name>` where you'd use a model name. read the final result from `response.choices[0].message.content`.

more examples:

- [customer emails](examples/email_processing.py): three parallel analyses, then draft, check, and finalize
- [spreadsheet classification](examples/spreadsheet_classification.py): typed JSON and a Python review threshold
- [expense intake](examples/expense_review.py): extract fields and choose an if/else branch

## About

use liteflow for work you can define in advance: process an email, classify a spreadsheet row, or route a request for review. write the steps in Python and run them on your LiteLLM gateway.

see the [gateway API](docs/gateway-api.md), [Python execution contract](docs/execution.md), and [implementation plan](docs/implementation-plan.md).

## License

[MIT](LICENSE)

## Contributing

open an issue or pull request. start with the [workflow examples](examples/) and [implementation plan](docs/implementation-plan.md).
