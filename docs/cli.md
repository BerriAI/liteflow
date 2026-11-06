# Liteflow CLI

Install the SDK and terminal extra in a virtual environment

```sh
pip install 'liteflow[cli]'
```

Set `LITELLM_PROXY_URL` to the gateway root URL without `/v1` and
`LITELLM_PROXY_API_KEY` to your virtual key. `LITELLM_TEAM_ID` is optional
and scopes draft registration and reads. There are no credential flags

Execution needs the owning team's virtual key or a team admin. Publication
stays a separate approval outside the CLI. The CLI never publishes a draft

## Show a local workflow

```sh
liteflow show example.py:workflow
```

```text
reply
├── node_1 ChatCompletion model=support-model
│   ├── messages: $input.messages
│   └── timeout=60.0s retry=1 backoff=0.0s
└── node_2 Python fn=finish
    ├── text: node_1.result
    └── timeout=60.0s retry=1 backoff=0.0s
output: node_2.result
```

This imports the Python file and compiles the workflow without network calls.
Importing runs its top-level code, so load only files you trust

## Register a draft

```sh
liteflow register example.py:workflow
liteflow register example.py:workflow --workflow-id draft-id
```

The second form reads the existing draft before replacing it with its ETag

```json
{"name":"reply","workflow_id":"draft-id","team_id":"team-id","etag":"etag","digest":"digest","published_snapshot_id":null}
```

## Validate

```sh
liteflow validate draft-id
```

```json
{"valid":true}
```

## Run a published workflow

```sh
liteflow run reply --message 'Review this draft' --idempotency-key review-1
liteflow run reply --messages messages.json
```

`messages.json` must contain an OpenAI Chat Completions message list

```text
Reviewed reply
run_id: run-id
```

An interactive stderr shows a transient invocation status, not node progress

## Inspect

```sh
liteflow inspect run-id
liteflow inspect run-id --outputs
```

```text
run_id: run-id
snapshot_id: snapshot-id
state: completed
cancel_requested: False
node    state      attempts duration tokens cost error
node_1  completed  1        2s       10/5   -    -
```

`--outputs` includes each step's input and output as JSON. A null output means
null, not redacted. Unfinished steps have no duration. Errors are short summaries

The SDK's step-read shape is provisional. A gateway must implement the matching
workflow execution and inspection APIs for network commands to work

## JSON and terminal safety

Every command accepts `--json`, for example `liteflow inspect run-id --json`.
JSON mode preserves response data, escapes control characters, and needs no Rich
installation. It writes one JSON value to stdout without ANSI sequences

Set `NO_COLOR` to any value, including an empty string, to disable ANSI output.
Piped text output also has no ANSI sequences. Text rendering removes terminal
control sequences and treats brackets literally rather than as Rich markup

Errors go to stderr and return exit code 1. A 401 points to the API key, a 403
points to team permissions, and a 409 or 412 asks you to register again
