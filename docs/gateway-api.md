# Gateway API scope

Status: proposed contract. LiteLLM does not implement this workflow executor, SDK, or API today. The examples in this repository describe the intended developer experience.

A developer defines a typed workflow, tests its draft, and submits it for publication. An application invokes the published workflow through the gateway's Chat Completions endpoint. The gateway executes each stage, validates its result, and records a run that the developer can inspect or replay.

## First supported path

1. Author `Workflow(nodes=[...])` with typed `ChatCompletion`, `Python`, and `If` nodes. Use `Parallel` when calls need to overlap.
2. Compile the Python definition into a JSON definition. Compilation extracts top-level transform and condition functions; it does not execute them.
3. Register the draft, validate it, and run a billed test using an authorized model.
4. Have a separate publisher review and publish the draft.
5. Invoke `model="workflow/<name>"` through Chat Completions and follow the returned run ID to its trace.

The SDK takes ordinary completion arguments on `ChatCompletion`, such as `model`, `messages`, and `temperature`. Its optional `response_model` helper exports a JSON Schema into standard `response_format`. Developers do not need prompt IDs or version labels. The server keeps immutable publication snapshots for repeatable runs and approval records.

`Input.messages` refers to the original validated Chat Completions message array. `Input.previous` refers to the preceding node's typed result within the current sequence or branch. Preserve message roles and ordering; do not select the first or last message without an explicit transform. Reject a request shape that the published workflow cannot accept with a specific error. The examples use text messages; multimodal support requires matching provider and node contracts.

## Definition and execution rules

### Sequential lists and explicit parallel work

A node list executes in order and carries the previous result into the next stage. A unary `Python(fn=...)` consumes that result by default. A `ChatCompletion` declares its source through `messages=Input.messages` or `messages=Input.previous`; do not omit messages or guess their source. Referencing an earlier result does not make adjacent nodes run in parallel. A reference to a later node fails definition validation.

- `ChatCompletion` makes one trusted gateway call. Without structured output, its `result` is a string. With `response_format.type="json_schema"`, the gateway parses and validates its JSON before exposing `result`. P0 uses one choice (`n=1`) and no model-driven tool loop. Reject unsupported multi-choice or tool-only output contracts.
- `Python` consumes the typed value from the prior stage, or explicit named bindings, and returns its annotated type. The SDK derives its input and output contracts from the function's annotations.
- `Parallel` starts named branches with the same input. Each branch contains a sequential node list. Its result is an object whose keys are branch IDs and whose values are each branch's final typed result. The join waits for all branches; a failed required branch prevents downstream execution.
- `If` runs a typed condition function that returns a boolean. The condition receives the current value and passes that value to the selected branch. It does not replace the input with the boolean. Only the selected branch executes. Its final result becomes the conditional node's `result`.

Require both conditional branches to declare compatible output contracts for P0. Downstream nodes reference the conditional node's result, rather than a node inside a branch that might not run. Mark the other branch `skipped` in the trace. Reject cyclic graphs, duplicate node IDs, inaccessible branch-local references, and missing results.

Scope concurrency at the workflow and gateway levels. Each parallel branch still obeys the model's rate limits and the caller's limits. Queue time consumes the node's total deadline; it must not create an unbounded wait before execution.

### References within nodes

Use explicit references in consuming nodes. The following definition value selects a prior node's entire typed result:

```json
{"from": "assess_urgency", "output": "result"}
```

A projection selects a field within that result:

```json
{"from": "assess_urgency", "output": "result", "path": "/priority"}
```

`path` uses JSON Pointer escaping and indexes. Missing paths fail validation or execution with the source node and field path. An absent value differs from an explicit JSON `null`.

The compiler lowers `Input.messages`, `Input.previous`, and the unary Python sequential default into these explicit node bindings. A Python function that returns `list[ChatMessage]` can feed the next completion through `messages=Input.previous`. Developers do not repeat default bindings in ordinary `Python(fn=...)` definitions; functions with multiple arguments declare their source bindings. The server keeps those bindings within nodes. It derives the visualization from the sequence, composites, and bindings; callers do not maintain a second connections graph.

The gateway resolves references before provider dispatch and checks the resolved completion request. A reference used as `messages[].content` must resolve to supported message content. It cannot reach a provider as a workflow reference object. Preserve arrays, numbers, booleans, and objects when resolving typed fields; do not stringify all inputs or coerce values that fail the target contract.

Keep `raw_response`, HTTP headers, authentication objects, callback state, and SDK hidden fields out of `result`. Trusted tracing can retain permitted provider response fields under the gateway's existing retention and access policies.

### Function contracts and Monty

P0 covers pure data transforms and condition functions through Monty. An exploratory local smoke test used `pydantic-monty` 1.4.0 and exercised JSON transformations, both condition outcomes, dataclass values, and termination of an infinite loop. That test included a failed file-read attempt without mounts and unavailable environment, socket, subprocess, and Pydantic imports. It did not establish blanket filesystem denial. This is exploratory validation, not a production isolation certification or a package pin for implementation.

Accept top-level functions with supported annotations. Reject closures, captured application objects, unbounded source/input/output sizes, unsupported imports, and unsupported annotation shapes before publication. Include permitted type declarations in the compiled function artifact. Validate the bound input and returned output as data; type annotations alone do not enforce a runtime contract. Do not import the author's module on the gateway or run custom Pydantic validators, constructors, descriptors, or import hooks. Use trusted schema validation on the stored data contract.

The authoring SDK may use Pydantic to produce schemas on the developer's machine. Python inside the transform runtime does not need Pydantic. The gateway supplies no host callbacks, filesystem mounts, provider credentials, or network primitives to user functions. Trusted call nodes handle external access outside Monty.

Configure runtime execution-time, memory, recursion, and output limits through server policy. Confirm the chosen Monty release's available limits before promising a specific control. Runtime errors include the node ID and a bounded diagnostic. A limit breach fails that step and releases its execution slot.

Defer full CPython packages, runtime package installation, file processing, arbitrary imports, network calls from Python, and persistent interpreter state. Those require a separate execution profile and security review.

## Management endpoints

Use `/v1/workflows` for executable definitions. Register the literal `/v1/workflows/runs` routes before workflow ID routes, and reserve `runs` as a name when resolving paths.

| Method and path | Purpose | Success |
| --- | --- | --- |
| `POST /v1/workflows` | Create a team-scoped draft from the compiled definition | `201` with workflow ID, draft ETag, validation summary |
| `GET /v1/workflows` | List definitions the caller can read, with cursor pagination | `200` |
| `GET /v1/workflows/{workflow_id}` | Read a draft, publication status, and permitted metadata | `200` with ETag |
| `PUT /v1/workflows/{workflow_id}` | Replace the draft using `If-Match` | `200` with new ETag |
| `POST /v1/workflows/{workflow_id}/validate` | Check the definition without model calls or function execution | `200` with validation findings |
| `POST /v1/workflows/{workflow_id}/test` | Execute a draft using the caller's allowed models and budgets | `202` with run ID and trace URL |
| `POST /v1/workflows/{workflow_id}/publish` | Publish the reviewed draft using `If-Match` | `200` with invocation model and snapshot ID |

The create and replace bodies contain the workflow name, typed nodes and bindings, input contract, and final output binding. Keep the SDK and server on one shared JSON schema. Examples elsewhere in this repository show the authoring syntax; they are not an implemented SDK.

Return an opaque ETag after creating or editing a draft. The SDK carries it when replacing or publishing that draft. Reject a stale `If-Match` with `412`; publishing must not approve changes made after review. Store the accepted definition, function source, schemas, dependency/model references, and content digest in an immutable snapshot. Updating a draft does not change an active run or the published snapshot.

The server resolves model references to configured gateway model groups. It must not store provider secrets or permit caller-defined deployment URLs in a workflow definition. An inline prompt belongs to the published definition; a prompt edit requires publication of that changed definition.

A test request uses the same message input shape as invocation, but requires draft execution permission. It creates a labeled draft run and incurs normal child-call costs. It cannot change the published workflow. Static validation does not execute sample code or call external models.

### Validation and errors

Validation checks node IDs and scope, reference paths, sequence order, branch output contracts, allowed completion parameters, supported function source/annotations, declared schemas, and configured model access. Treat static schema compatibility conservatively: reject a known incompatible binding and report unsupported constructs rather than claiming complete JSON Schema type inference.

Validate actual values at each stage. Reject missing JSON, invalid JSON, provider refusal, truncation, a tool-only completion, or a schema mismatch when the node requires a typed result. These failures must not become an empty successful value.

Use the gateway's existing error envelope with stable machine-readable codes. A proposed node failure includes:

```json
{
  "error": {
    "type": "workflow_validation_error",
    "code": "node_output_schema_mismatch",
    "message": "Expected a string at /priority.",
    "param": "nodes.assess_urgency.result.priority"
  }
}
```

Use `400` for an unsupported request contract, `403` for a permitted resource with a denied action, `404` for an inaccessible or missing resource, `409` for an idempotency conflict or invalid lifecycle transition, `412` for a stale draft, and `429` for capacity or rate limits. Runtime failures retain the recorded step outcome and the gateway's error mapping. Do not copy credentials, customer inputs, or unbounded exception text into error messages.

## Invocation through Chat Completions

Applications continue to use the existing gateway base URL, credentials, and client:

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
        {"role": "user", "content": "I cannot open the invoice you sent. Can you help?"}
    ],
)
print(response.choices[0].message.content)
```

`LITELLM_PROXY_URL` is the gateway base URL without `/v1`; keep the API key in protected environment configuration. The gateway resolves the published workflow once, records its snapshot ID, and creates a durable run before starting its nodes. Both draft testing and published invocation call the same execution service. The Chat Completions adapter formats the final result; it does not run a second workflow engine.

Return an ordinary Chat Completions object. A string workflow result becomes assistant message content. For a declared JSON result, serialize validated JSON into message content; do not use Python `repr`. Report aggregate child usage, and retain detailed per-node usage in the run trace. Set `x-litellm-workflow-run-id` on accepted invocations and on subsequent gateway error responses for that run.

For P0, accept the workflow model name, supported messages, `user`, and permitted metadata. Reject unsupported generation/tool options on the outer workflow request with a specific error. Do not propagate an outer `temperature`, `response_format`, `n`, or tools value across the workflow's nodes. Node definitions own those choices.

Start with non-streaming invocation. Streaming the final completion can follow after the executor handles cancellation, final-node failure, and usage without emitting intermediate node outputs as assistant tokens. A streaming connection cannot remove an upstream caller's absolute timeout.

### Idempotency and client timeouts

Support an optional `Idempotency-Key` header on invocation and draft tests. Scope its persisted record by authenticated principal, team, operation, and workflow. Store a canonical request digest and the accepted snapshot ID.

- An equal key and equal request returns the original completed result, or attaches to the existing active run within the caller's wait budget.
- An equal key with a changed request returns `409`.
- A client retry after publication changes still refers to the original accepted snapshot.
- Commit run creation and its durable dispatch intent together, so a crash cannot leave a recorded run with no dispatch path or dispatch the same request as another run.

Record an accepted run before replying. A client disconnect detaches the waiter; it does not cancel the run. An explicit cancel action stops further dispatch. A gateway wait timeout returns its normal error envelope and run ID header when the transport remains available. A caller whose upstream proxy closes the connection can retry with the same idempotency key to recover the run without starting another one.

Set a bounded retention policy for idempotency records and stored results. Document the retention window before releasing this contract. Do not promise exactly-once effects from external systems that provide no idempotency support.

## Run, step, and replay endpoints

Extend the current run read surfaces and add engine-owned step records:

| Method and path | Purpose |
| --- | --- |
| `GET /v1/workflows/runs` | List permitted runs with workflow/status filters and pagination |
| `GET /v1/workflows/runs/{run_id}` | Read status, snapshot identity, aggregate usage/cost, output, and error summary |
| `GET /v1/workflows/runs/{run_id}/events` | Read a cursor-paginated ordered event stream |
| `GET /v1/workflows/runs/{run_id}/steps` | Read node states, dependencies, attempts, and skip reasons |
| `GET /v1/workflows/runs/{run_id}/steps/{step_id}` | Read permitted bound inputs, typed results, attempts, and diagnostics |
| `POST /v1/workflows/runs/{run_id}/cancel` | Request cancellation through the executor |
| `POST /v1/workflows/runs/{run_id}/steps/{step_id}/replay` | Execute one recorded step in a new debug run |

Use separate `node_id` and `step_id` fields. `node_id` identifies the definition node; `step_id` identifies its execution record in that run. Record attempt numbers under that step. Keep node inputs, typed result, request ID, timing, cache-hit state, model, usage, cost, and bounded error details. A skipped branch has no LLM call or Python invocation.

For an isolated step replay, reuse the source run's snapshot and the selected step's recorded inputs. Create a new debug run with links to the source run and step. Execute only that step; do not run downstream nodes or mutate the original trace. Reapply current authorization, budgets, and runtime limits. External calls can incur new cost. Refuse replay when retention policy has removed required inputs or when the caller cannot read those inputs.

Defer resume-and-recompute-downstream replay until the execution service handles invalidating dependent results and conditional choices. Durable crash recovery is a separate executor responsibility: recover from stored successful checkpoints without treating a process restart as a user replay.

Cancellation is idempotent. Persist the request, prevent new node starts, and terminate or cancel active work where the runtime/provider supports it. Keep already-incurred provider costs and completed step records. Mark a run canceled only after its execution work settles; surface an explicit cancellation-requested state in the meantime. Do not promise cancellation of an external effect after a provider has accepted it.

### Compatibility with existing workflow tracking

The LiteLLM checkout already has `POST /v1/workflows/runs`, run update/list/read endpoints, events, and messages. The create body takes `workflow_type`, optional `input`, and optional `metadata`; it inserts a tracking record and does not execute work.

Keep that create behavior and its external tracking clients. Use a server-controlled run origin/owner field to distinguish externally tracked runs from gateway-executed runs. Ordinary callers must not use the existing PATCH/event/message mutation routes to forge an engine-owned run's status, output, or execution history, even if they created its invocation. Engine writes belong to the executor. Preserve current ownership rules for legacy external tracking records; add team-aware read permissions for the new workflow feature.

Keep new fields additive where existing readers depend on the response. The existing run status set lacks cancellation; extend persisted and response types through a deliberate compatibility change rather than pretending cancellation already exists. Keep static `/runs` routes ahead of dynamic definition routes, and cover both methods when reviewing shared paths.

## Permissions and publication

Separate authoring, invoking, reading traces, and publishing. Scope those actions to the owning team and workflow, using the gateway's existing identity and role system.

| Capability | Author/developer | Publisher/admin | Application key |
| --- | --- | --- | --- |
| Create/edit/validate a draft | Allowed by team grant | Allowed by team grant | Denied |
| Test a draft | Allowed with model/budget grants | Allowed with model/budget grants | Denied |
| Read permitted traces and costs | Allowed by scope | Allowed by scope | Own permitted runs |
| Publish a draft | Denied for its contributors | Allowed after review if not a contributor | Denied |
| Invoke a published workflow | Allowed by invocation grant | Allowed by invocation grant | Allowed by invocation grant |
| Replay or cancel | Explicit scoped grant | Explicit scoped grant | Explicit scoped grant |

The publisher must not appear among contributors to the draft being published. A role label alone does not prevent self-approval. Store the author/contributor identities, publisher identity, reviewed digest, and timestamp with the snapshot. Any draft edit changes its digest and requires a new review. Keep an emergency self-approval override outside P0.

Keep deployment authority out of functions. A workflow definition cannot change its permissions, publish itself, inject provider credentials, choose a host callback, or enable a runtime capability.

## Authorization, budgets, and billing

Authorize the workflow grant at admission. A published workflow-only key should not require knowledge of its internal model names or expose those models for direct use. Implement a scoped internal execution grant that binds the caller, owning team/project, immutable snapshot, and approved model dependencies. Treat that grant as a typed internal capability, not a broad master key passed to Python.

Reuse shared gateway authorization for each trusted child call. Recheck current team/project/model blocks, revoked invocation permissions, end-user restrictions, budgets, and runtime limits before dispatch. Publication cannot freeze an old permission that an administrator later revokes. If the first implementation instead requires callers to hold each child model permission, document that narrower behavior and do not claim workflow-only access is complete.

Trusted child calls use the existing gateway preprocessing, routing, hooks, caching, and accounting path. Calling the router alone omits policy checks that the auth layer owns. Extract small typed entry points at those owning layers when internal execution needs them; do not copy authorization into the workflow executor or fabricate an HTTP request per child.

- Charge actual child LLM calls and their billable retry attempts to the original caller/team/project/end user. Record workflow/node/attempt metadata on those records.
- Sum those canonical costs for the workflow trace. The workflow wrapper does not create another billable completion or a second model RPM/TPM charge.
- Enforce workflow concurrency/admission separately from child model RPM/TPM. Three parallel calls consume three child-call slots.
- A node's total deadline includes queueing, routing/fallbacks, retry delay, and execution. Router retries consume that same policy budget; they must not multiply a workflow retry limit without visibility.
- Reuse existing per-node cache policy. Mark cache hits in traces and apply existing charging policy. Disable whole-workflow completion caching for P0.
- Python transforms incur no provider token charge. Keep runtime usage metrics separate if a future offering bills execution time.

Validate completion parameters at registration and again after resolving references. Accept supported generation fields. Reject request-supplied `api_key`, `api_base`, `base_url`, custom providers, authentication headers, callbacks, `user_config`, and router overrides. Resolve model aliases using trusted gateway configuration. A workflow definition is untrusted input even after a developer signs in.

## Implementation slices and acceptance

Each slice should form one focused PR with its own observable acceptance criteria. These slices describe planned work; this repository does not implement them.

1. **Definition contract and validation.** Add shared typed schemas and a compiler validation boundary for sequential lists, refs, `Parallel`, `If`, and supported function annotations. Acceptance: invalid IDs/forward refs/branch output mismatches produce stable paths; definition validation makes no model or function calls; existing tracking endpoints keep their behavior.
2. **Draft storage and publication.** Add team-scoped CRUD, ETags, immutable snapshots, contributor records, and publish permission. Acceptance: stale writes fail; authors cannot self-publish; editing a draft cannot change a published snapshot; unauthorized teams cannot read definitions.
3. **Monty transforms and conditions.** Add the data-only runtime adapter and typed input/output validation. Acceptance: transform and both conditional paths produce validated results; denied imports/callbacks/mounts remain unavailable; runaway code terminates and releases its slot; stdout/output stay bounded. Tests use the selected pinned runtime rather than mocks alone.
4. **Sequential execution and Chat Completions adapter.** Add durable run admission, checkpoints, child-call policy delegation, completion formatting, and idempotency. Acceptance: a real gateway request executes LLM to transform to LLM through a controlled provider; raw reference objects never reach it; costs and rate counts apply once per child; retries with one idempotency key cannot create a second run.
5. **Parallel and conditional scheduling.** Add bounded branch concurrency, joins, and persisted selected/skip states. Acceptance: a controlled provider records overlapping independent calls; sequential nodes stay ordered; only the selected conditional branch executes; one required branch failure prevents the downstream join.
6. **Trace, cancellation, and isolated step replay.** Add step/attempt reads, event pagination, cancellation, retention checks, and debug runs. Acceptance: the trace shows real bound inputs/results and charged attempts; replay calls only the selected step; cancel prevents new starts and releases execution resources; users cannot forge engine-owned status through legacy write endpoints.

Before filing implementation PRs, test against a controlled provider and show captured HTTP requests/responses or trace evidence for the workflow under review. Unit tests support that evidence; they do not establish a live execution path. Run the repository's required checks and verify target-branch compatibility before calling an implementation slice complete.

## Existing-code evidence and limits

The source review included a locally modified LiteLLM checkout. Recheck the target branch before implementation. This scoping repository ships no gateway changes.

Relevant owners in the inspected checkout:

- `litellm/proxy/management_endpoints/workflow_management_endpoints.py`: existing run/event/message tracking.
- `litellm/proxy/schema.prisma`: tracking tables; no complete executable-definition, checkpoint, or typed-step model.
- `litellm/proxy/proxy_server.py`: Chat Completions handler and router registration.
- `litellm/proxy/auth/user_api_key_auth.py` and `auth_checks.py`: identity, model permissions, budgets, and route authorization.
- `litellm/proxy/common_request_processing.py`: pre/post hooks and child-call processing.
- `litellm/proxy/route_llm_request.py`: routing, including request credential/base-URL overrides that definitions must reject.
- `litellm/llms/litellm_proxy/skills/sandbox_executor.py`: existing full-Python skills integration; its runtime package installation and container lifecycle do not establish the proposed Monty transform contract.

Unresolved release details include exact SDK wire-field names, supported annotation/JSON Schema subsets, invocation wait and retention defaults, model capability handling for structured outputs, and streaming. Settle these through compiler/runtime tests before publishing executable API documentation. No production deployment procedure or customer infrastructure configuration forms part of this scope.
