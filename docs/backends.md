# Executable backend: LangGraph

LangGraph is the first external backend selected from the design's target list.
Temporal, n8n, GitHub Actions workflow export and Azure Durable Functions remain
future backend options. The project CI using GitHub Actions is not a workflow
compiler backend implementation.

Install the optional dependency and run an IR 0.1 workflow:

```bash
python -m pip install -e '.[langgraph]'
python -m agentic_workflow compile examples/document_pipeline/SKILL.md \
  -o .state/document.json
python -m agentic_workflow run .state/document.json \
  --backend langgraph --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline --db .state/langgraph.db
```

`LangGraphBackend.compile()` emits importable/executable Python source,
`workflow.json`, an IR hash and a capability manifest. The generated entrypoint
loads project code and builds a native `langgraph.graph.StateGraph`; it does not
contain arbitrary executable code from the IR.

```bash
python -m agentic_workflow backend-compile .state/document.json \
  --backend langgraph -o .state/langgraph-artifact
python .state/langgraph-artifact/run_workflow.py \
  --inputs examples/document_pipeline/inputs.json \
  --workspace examples/document_pipeline --db .state/generated.db
```

## Execution and persistence

Each IR step becomes a distinct StateGraph node. Graph edges drive ordered
execution and stop traversal on failure. Every node resolves its own condition
and arguments, then executes one trusted registered leaf tool in a spawned
process with a hard timeout. The project records durable SQLite step results,
attempt counts and events; `graph_node_visited` events identify native graph
visits. The installed/tested version is LangGraph 1.2.11; the extra supports
`langgraph>=1.2.11,<2`.

| Behavior | LangGraph backend |
| --- | --- |
| IR version | 0.1; rejects 0.2 explicitly |
| Ordered tool and AI steps | One native graph node per step |
| Conditions and skipped references | Same typed equality and null semantics as local runtime |
| Retry and timeout | Per-invocation bounded retry, spawned process timeout |
| Resume | Completed/skipped results restored from durable SQLite |
| Interrupted effects | Explicit `recover(..., policy='retry')` before resume |
| Stable identity | Workflow, inputs and workspace bound to run ID |
| Idempotency keys | Same run ID + step ID formula as local runtime |
| Loops, parallel, approval | Use the Python IR 0.2 runtime |

The backend uses the project's leaf checkpoint schema instead of a LangGraph
checkpointer. On resume, the graph traverses earlier nodes to restore persisted
results but does not execute their completed effects. The same run must keep the
same workflow, inputs and workspace. Changing a tool implementation is still a
caller-managed deployment concern; callable identity is not pinned by IR 0.1.
Spawned leaves hold a per-run effect lock and check an execution epoch before
calling the tool. Recovery refuses while a surviving child still holds that
lock; rotating the epoch fences an old child that starts after recovery. This
also has a real killed-parent test, in addition to checkpoint-state tests.
Recovery does not guarantee exactly-once effects: integrations must enforce their
idempotency keys or an operator must reconcile uncertain effects first.

`LangGraphRuntime(db_path, workspace, tools=None, ai_tools=None)` offers the same
`run`, `inspect`, `events`, `recover` and `close` API as the local runtime.
`LangGraphBackend().run(workflow, inputs, db_path, workspace, ...)` is a convenience
wrapper. Runtime invocation is synchronous; a Python caller should call it from
a worker thread when already running an asyncio event loop.

Tests use the real installed StateGraph and assert invocation, file outcomes,
failure short circuiting, retry counts, skip/null semantics, resume without
duplicate completed effects, identity mismatch and explicit recovery. Optional
dependency absence is reported as skipped; it is not replaced with a fake graph.

Primary API references: [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
and [StateGraph reference](https://reference.langchain.com/python/langgraph/graph/state/StateGraph).
