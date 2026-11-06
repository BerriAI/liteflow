# Graph workflows

This SDK change adds graph authoring and compilation. Gateway scheduling for these definitions is a separate implementation. Compiling a graph does not verify gateway execution, model availability, billing or recovery.

## Define nodes

`Workflow(nodes=[...])` runs nodes in order. Each node consumes explicit bindings and produces one JSON result. Add `Sequence` only when you want a nested group; the outer workflow already provides sequential order.

`Parallel(branches={name: [...]}, max_concurrency=4)` gives each named branch the same incoming value and returns an object keyed by branch name. The result follows branch names rather than completion order. Branches cannot bind to another branch's intermediate nodes.

`ForEach(items=Input.previous.field("items"), nodes=[...], max_items=100, max_concurrency=4)` defines one execution of its body for each list item. The body starts with that item as `Input.previous`. Its last result becomes the corresponding output item. Results retain input order. An empty input list returns `[]`; the body definition must still contain nodes.

`If(condition=needs_review, then=[...], otherwise=[...])` evaluates a top-level typed Python function that takes one argument and returns `bool`. The selected branch starts with the condition's incoming value, rather than the predicate's boolean result. Both exits must declare the same output schema; normalize differing results with Python or annotate both exits with the same union. The condition uses the existing Python program contract. It is not an expression string or an LLM routing decision.

Sequence groups, branches, and loop bodies must contain nodes. For a pass-through exit, use a typed Python function that returns its argument or normalizes the result.

## Bind data

| Binding | Meaning |
| --- | --- |
| `Input.messages` | The original request's chat messages throughout the graph |
| `Input.previous` | The preceding result in the current sequence |
| `Input.previous.field("items")` | A required object field; chain `.field(...)` for nested fields |
| `Input.item` | The original item from the nearest enclosing `ForEach` |

A branch's first node receives the container's incoming value. A loop's first node receives its item. Later nodes advance `Input.previous`; `Input.item` remains the same until a nested loop supplies a nearer item. The outer item becomes available again after that nested loop finishes. `Input.item` outside a loop is invalid.

Python arguments use annotations, with `inputs` for explicit multi-argument bindings. LLM nodes use `response_model` to declare structured results. The compiler checks required field selections, basic source/target shapes, predicates and conditional result schemas. It does not prove every JSON Schema constraint. The gateway must validate resolved inputs and outputs at execution.

## Keep execution bounded

The SDK defaults to four concurrent branches/items and 100 items per `ForEach`. Authors can request different positive limits. The compiler rejects nesting beyond 16 levels and a conservative expansion beyond 10,000 node executions. These authoring limits do not replace gateway admission and deployment limits.

The gateway implementation must enforce a shared active-leaf limit and expanded-work budget across nested fan-out, freeze iterator input before dispatch, and preserve completed child work during recovery. A failed child retries within its original absolute deadline. Composite retries are not part of this contract. The workflow deadline caps the run.

The existing leaf `timeout_seconds` includes attempts and backoff; `Retry.max_attempts` includes the initial call. Per-node fallback aliases remain separate work.

## Register and invoke

Use the existing `GatewayClient.register(workflow)` and independent publication flow. Invoke through `GatewayClient.run(name, messages=...)`, which uses `/v1/chat/completions` with `model="workflow/<name>"`. Graphs do not introduce another execution endpoint.

Existing flat ChatCompletion/Python workflows retain their v1 serialized bundle. Composite nodes and projected bindings use contract version 2. A gateway must understand that contract before accepting them.

Definition IDs are globally unique `node_N` values in preorder. They identify nodes in the visualizer. A gateway must separately identify each loop execution with an occurrence path; one `(run_id, node_id)` row cannot represent repeated items. The planned trace extension records hierarchy, item/branch identity and skipped nodes without charging container costs a second time. These runtime requirements are not implemented by the SDK.

## Examples

- [Conditional email feedback](../examples/email_feedback.py)
- [Per-item parallel email analysis](../examples/batch_email_analysis.py)

The [ADK workflow overview](https://adk.dev/agents/workflow-agents/) describes sequential, parallel and iterative workflows. Per-item list processing maps to `ForEach`: ADK's [LoopAgent](https://adk.dev/agents/workflow-agents/loop-agents/) repeats until a stopping condition or iteration limit. Hybrid authoring nests these constructs.
