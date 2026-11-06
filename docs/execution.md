# Execution contract

This document defines the proposed P0 contract for Monty execution. It does not claim that the workflow runner, compiler, or gateway integration implements this contract yet.

P0 uses Monty for Python transformations and `If` conditions. The gateway executes authenticated LLM, API, and MCP actions outside Monty. Authors invoke a published workflow through Chat Completions and inspect its node results through the workflow run record.

## Runtime boundary

The runner gives each Monty session the node's resolved input data and source. It grants no host functions, external modules, host objects, filesystem mounts, or OS callbacks. Code can transform data and return a value. It cannot request a new capability through its output.

Keep provider keys, gateway tokens, authentication headers, routing configuration, SDK clients, and internal request objects outside this boundary. Pass the node's validated typed result as plain JSON-compatible data. Keep live SDK objects and hidden transport attributes outside the session.

Monty supplies a language-level sandbox and worker subprocesses. The interpreter excludes ambient filesystem, environment, socket, process, and native-code access. Its Python worker pool also provides crash isolation. These properties do not constitute an OS sandbox or prove the absence of interpreter vulnerabilities. See the upstream [security model](https://pydantic.dev/docs/monty/concepts/security/).

Package the optional worker pool with the same LiteLLM release so that the Monty path needs no Docker daemon or managed execution service. Use a separate credential-free execution environment when the deployment needs an additional boundary against an interpreter escape. Keep that deployment choice outside workflow definitions.

## Source and typed data

The SDK compiles the selected function and its supported helpers into a source bundle. The gateway must not import or execute an uploaded Python module to discover its nodes, schemas, decorators, or dependencies.

For a function annotated with a record type, the compiler stores its JSON schema and generates a sandbox-safe record definition when the function needs attribute access. It packages the function and generated definition together. It does not copy a Pydantic class implementation into Monty or import Pydantic there.

Use trusted schema validation outside Monty. Do not run an author's custom Pydantic validators, constructors, import hooks, or descriptors on the gateway. Reject schema or source features that require unsupported behavior, with the affected node and a useful error.

At the execution boundary:

1. Resolve the node's bindings from the request and completed predecessor outputs.
2. Validate the resulting JSON against the stored input schema and size/depth limits.
3. Convert supported records inside Monty and execute the packaged function.
4. Convert its result to plain JSON-compatible data and validate the output schema and limits.
5. Store the validated result before releasing dependent nodes.

Allow nulls, booleans, finite numbers, strings, lists, and objects with string keys when their schema permits them. Reject arbitrary Python instances, callables, object proxies, and serialized executable objects. An `If` condition returns a boolean; the scheduler executes its declared true or false branch.

## Supported Python

P0 supports pure transformations and conditions within Monty's Python subset: functions, `if`/`elif`/`else`, loops, comprehensions, ordinary containers, and supported standard-library operations such as JSON parsing. A sandbox-local dataclass can represent a typed record.

Monty does not supply an arbitrary CPython environment, native extensions, or installed third-party packages. A workflow cannot assume that pandas, NumPy, Pydantic, or an existing application module can be imported. Check the upstream [supported subset](https://pydantic.dev/docs/monty/limitations/) against the pinned runtime. Reject unsupported code; do not fall back to CPython, `exec()`, or an unrestricted subprocess.

## Limits and worker lifecycle

The release must define and enforce these policy limits before enabling untrusted nodes:

| Limit | Required behavior |
| --- | --- |
| Source, input, and result size/depth | Reject oversized values before compilation, execution, or persistence. |
| Monty memory | Set `max_memory`; also bound worker memory at the process or container level. Monty's allocator budget is not a process-RSS ceiling. |
| Execution and recursion | Set the pinned runtime's execution-time and recursion limits. |
| Whole-node wall time | Bound compilation, execution, conversion, attempts, and retry delays together. A per-turn worker timeout does not replace this deadline. |
| Printed output | Cap captured bytes while receiving them; treat text as data and escape terminal control sequences. |
| Concurrency and worker lifetime | Bound queued/running work and use a fresh worker across trust contexts. |

Use the subprocess worker pool rather than an in-process interpreter integration. Pin the package and worker binary as a tested pair. Select the bundled binary by an explicit verified path, verify artifact digests, and verify publisher signatures when the release provides them. Do not accept a workflow-supplied binary path or resolve one from an untrusted `PATH` or environment override.

Configure a hard host-side deadline that terminates an unresponsive worker. Discard a session after a terminal timeout, memory failure, worker crash, or protocol violation. Recreate it from the stored source and input for a retry.

Set the worker recycling policy so unrelated callers do not share a reused worker. Monty's worker timeout, memory accounting, compilation budget, and host-side output collectors have different scopes; see [resource limits](https://pydantic.dev/docs/monty/concepts/resource-limits/). Do not describe any one of them as a complete resource limit for the gateway.

## Published versions and recovery

Freeze the source bundle, schemas, node configuration, and runtime policy/version in an immutable published workflow version. Resolve the version at run creation and record it with the run. An update changes the version used by later runs.

The workflow runner owns durable scheduling and recovery. Save each successful node's validated output and status before dispatching dependent work. After interruption, reuse saved predecessor results and rerun only unfinished work under the recorded version and input.

P0 does not require Monty memory dumps for recovery. Recreate pure transform sessions from source and JSON. Do not restore snapshots supplied by workflow code or API callers. If a later version stores runtime snapshots, authenticate their provenance and integrity before restoration.

Authenticated actions retain their own retry and idempotency contracts. A process crash after an external action succeeds can leave its result unrecorded. Do not promise exactly-once effects or replay completed writes as part of a Python retry.

## Verification status

A local smoke test with `pydantic-monty==1.4.0` exercised a JSON transformation, both boolean branches, and a sandbox-local dataclass. It also exercised failed file/environment access attempts, unavailable socket/subprocess/Pydantic imports, and termination of an infinite loop.

This evidence covers those runtime examples. It does not verify the workflow compiler, gateway invocation, persistence, resource-exhaustion coverage, or resistance to sandbox escape. Pin the package and worker versions that the implementation verifies. Upstream documentation can describe behavior beyond that tested version.

Before enabling the workflow integration, verify the same cases through its public API and add cases for malformed bindings, custom validators/import side effects, invalid or oversized JSON, large allocations/printing, deep recursion, worker death, cross-run state, and recovery after interruption. Include attempts to turn model output into a host callback, URL, credential selector, or executable object.
