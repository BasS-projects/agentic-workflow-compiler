# Structured execution: IR 0.2

`AdvancedRuntime` executes bounded loops, parallel branches and human approval
checkpoints with the same registered tool call contract as the original runtime.
IR 0.1 and its original runtime remain supported. Migration is explicit:
`migrate_v1(workflow)` returns an isolated IR 0.2 copy and preserves leaf behavior.
The original run identity cannot be resumed using a different IR version.
Run IDs share an exclusive lock and identity namespace across local engines in
the same database. Reusing an existing ID with another IR/runtime is rejected.

```python
from agentic_workflow.advanced_runtime import AdvancedRuntime

runtime = AdvancedRuntime('.state/runtime.sqlite3', 'workspace')
result = runtime.run(workflow, inputs, run_id='invoice-review-01')
if result['status'] == 'waiting_approval':
    checkpoint = result['approvals'][0]['step_path']
    runtime.approve(result['run_id'], checkpoint, True, actor='reviewer@example.org')
    result = runtime.run(workflow, inputs, run_id=result['run_id'], resume=True)
```

Run Python entrypoints behind `if __name__ == '__main__':` when using spawned
processes. The CLI and remote worker already provide this guard. Tool functions
and trusted configured closures are serialized locally with cloudpickle. No
workflow field, HTTP request or runtime database is deserialized as pickle.

## Nodes and references

Every node has an identifier unique within its sibling sequence. Every node may
have `when: {"equals": [EXPRESSION, EXPRESSION]}`. JSON booleans and numbers are
distinct. A skipped node returns `null`, and any reference into it returns `null`.
Only leaf `tool` and `ai` nodes accept retry and timeout configuration.

| Kind | Required fields beyond id/kind | Result |
| --- | --- | --- |
| `tool` / `ai` | `tool`, `args` | Tool JSON object |
| `foreach` | `items`, `max_items`, `steps` | `{items: [{body_id: result}], count: N}` |
| `parallel` | `branches`, `max_workers` | `{branches: {name: {body_id: result}}}` |
| `approval` | `prompt` | `{approved: true, actor: NAME}` after approval |

`foreach.items` must resolve to an array no longer than `max_items` (integer
1–1000). The bound is checked before any body execution. Empty arrays are valid
and return an empty result. Iterations execute in input order. Body references
`loop.item` and `loop.index` address the nearest enclosing loop; outer loops can
capture a value in a named step before entering a nested loop.

`parallel.branches` maps 1–100 identifiers to nonempty sequences. `max_workers`
(integer 1–8) bounds concurrently active branches in that block. Nested blocks
have their own branch bounds; an additional global cap of eight active tool
processes applies per runtime invocation. Independent branches join before the
next sibling node executes. A failed branch does not stop independent siblings;
a failed join blocks subsequent sibling nodes. Approval pauses its branch while
other branches continue and their completed work is checkpointed. Waiting
approvals from the join are collected together. Failure takes precedence over
waiting, and cancellation takes precedence in the terminal run status.

Each scope can read inputs, earlier sibling nodes, and captured earlier outer
nodes. Cross-branch references and forward references are rejected. References
use dotted object fields and nonnegative array indices, e.g.
`steps.batch.items.0.normalized.text`, `inputs.payload.records.0`,
`loop.item.details.name`. A missing field/index is an execution error. No eval,
Python import or expression language executes from the IR. Maximum static nodes:
1000. Maximum scope nesting: eight levels including the root.

```json
{
  "ir_version": "0.2",
  "id": "normalize_and_review",
  "inputs": {"documents": {"type": "array"}},
  "steps": [
    {
      "id": "batch", "kind": "foreach", "max_items": 100,
      "items": {"$ref": "inputs.documents"},
      "steps": [
        {"id": "normalize", "kind": "tool", "tool": "text.normalize",
         "args": {"text": {"$ref": "loop.item"}}}
      ]
    },
    {"id": "release", "kind": "approval", "prompt": "Release this normalized batch?"}
  ],
  "outputs": {"documents": {"$ref": "steps.batch.items"}}
}
```

## Checkpoint, approval, cancellation and recovery semantics

SQLite checkpoints every completed/skipped node, including leaf nodes inside
unfinished loops and branches. `resume=True` with identical workflow, inputs and
workspace retains completed nodes and retries failed/incomplete nodes. Automatic
`max_attempts` applies to each explicit invocation; cumulative attempts remain
visible in `inspect`. Retry delay and active tools are cancellable.

The idempotency key is SHA-256 of canonical `[run_id, step_path]`. Stable paths
are `batch/0/normalize` for loops and `quotes/vendor_a/read` for branches. Keys
remain the same across retries, approval pauses and explicit recovery. They are
passed to every trusted tool in `TaskContext`. An external service must actually
honor a key, or an operator must reconcile effects. Checkpointing alone cannot
promise exactly-once external effects after a crash.

`approve(run_id, step_path, approved, actor)` only accepts existing waiting
approval checkpoints. Decisions are durable and immutable. Repeating the same
actor/decision is idempotent; changing a recorded decision fails. Denial causes
run failure when execution resumes and blocks downstream work. A new reviewed
run is required to replace a denial. API authentication supplies the actor;
local Python/CLI callers are trusted and supply their own actor. Resume also
accepts `decisions={PATH: {"approved": BOOL, "actor": NAME}}` or a list of
`{"step_path": PATH, "approved": BOOL, "actor": NAME}` objects.

`cancel(run_id)` durably marks cancellation and returns immediately. Active
execution polls between nodes, during retry delay, and every 20 ms while tools
run. It sends SIGTERM to each tool process group, gives a 200 ms grace period,
then SIGKILL. Normal scheduler/OS delays may lengthen this interval. External
requests already accepted cannot be undone. Trusted tools must not deliberately
escape their process group. A cancelled run is terminal; resuming it returns
`cancelled`. A completed run remains completed if cancellation arrives later.
The final output transaction gives a concurrently persisted cancellation priority.

A surviving executor or tool owns OS locks that prevent duplicate local execution.
If an executor dies, `run(..., resume=True)` refuses implicit recovery. After
reconciling uncertain external effects, call `recover(run_id, policy='retry')`,
then resume. Recovery is refused while an orphan tool retains an effects lock;
wait for it or terminate/reconcile it operationally. A fenced execution token
also prevents a delayed spawn from invoking a tool after recovery changed its
identity. These local locks are not distributed coordination; HTTP workers use
the coordinator's durable leases and explicit recovery protocol.

If an HTTP worker loses its local checkpoints after approval decisions have
been recorded, takeover requires a new run ID and fresh review. Old decisions
must not approve reconstructed outputs: repeated file reads, AI calls or tools
may return different values. Resume on the worker retaining its original
checkpoints keeps completed outputs and their recorded decisions together.

`inspect` returns workflow, bound inputs, status, outputs, errors, decisions,
waiting approvals and per-path checkpoint/attempt/key records. `events` returns
ordered durable lifecycle events. `close` is a no-op because database
connections close after every transaction. Neither API hides tool failure as
success. Do not run this synchronous `run` method inside an already-running
asyncio loop; dispatch it from a worker thread or process instead.

## Verification

Run `python -m unittest discover -s tests -p 'test_advanced*.py' -v` for strict
validation, process overlap and bounds, durable approvals, denial, retry key
stability, cancellation, timeout, identity refusal and explicit recovery.
The independent SIT runner covers structured business workflows and HTTP workers.
The structural JSON Schema is `schemas/workflow-ir-v0.2.schema.json`; lexical
scope, total node/depth limits and dynamic list bounds additionally require the
Python validator/runtime.
