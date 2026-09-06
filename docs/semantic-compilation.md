# Semantic compilation and review

The compiler can extract a workflow from prose through an explicitly configured
chat completions endpoint. `SemanticCompileProvider` requests IR 0.1 and an
explicit refusal for ambiguous, underspecified, or unsupported requests. The
structured compiler accepts validated IR 0.1 and 0.2. Neither path executes tools
during compilation or evaluation.

Semantic output is a proposed interpretation. Schema validation alone cannot
prove that it captures the user's intention. Every compile bundle begins with a
pending review, and execution through the CLI requires an approved bundle.

## Compile, inspect, approve, execute

```bash
# The endpoint is opt-in; this command sends the source to that provider.
export WORKFLOW_AI_KEY='your-provider-key'
python -m agentic_workflow compile examples/semantic/normalize.md \
  --semantic --endpoint https://YOUR-HOST/v1/chat/completions \
  --model YOUR-MODEL --api-key-env WORKFLOW_AI_KEY \
  -o .state/proposed.bundle.json

# Read source, workflow, diagnostics and provenance in the JSON before approval.
python -m agentic_workflow approve-bundle .state/proposed.bundle.json \
  --actor your-name -o .state/approved.bundle.json
python -m agentic_workflow run .state/approved.bundle.json \
  --inputs examples/semantic/inputs.json \
  --workspace .state/semantic-work --db .state/semantic.db
```

An endpoint key is read from the named environment variable. Bundle provenance
records the provider implementation, model, endpoint origin and timeout; it
never records provider headers, API keys, URL path or query. Source text is
retained for review, so keep credentials out of Skills and literal IR arguments.
Transport failure details and arbitrary extractor exceptions are sanitized.

The review binds SHA-256 hashes of the exact UTF-8 source, canonical IR and the
bundle payload, including diagnostics and provenance. Changing any bound field
invalidates the review. Recompiling creates a new pending review; copying an old
approval does not approve the new content. The local review is an integrity
acknowledgement, not a digital signature: someone with permission to rewrite the
bundle can also manufacture a new approval record. Enforce trusted reviewer
identity with repository permissions or an authenticated approval service.

## Safe optimizer

`compile --bundle --optimize` folds constant equality conditions. It removes a
constant true condition and replaces a constant false condition with the
canonical false equality. It retains all steps and references in their original
order, including skipped steps, and preserves the distinction between booleans
and numbers. It never executes, removes or reorders effects, propagates input
defaults, or substitutes tool results. The same optimization applies recursively
to structured IR 0.2 bodies. Every change is listed in diagnostics before review.

## Evaluation evidence

`examples/semantic/evaluation-cases.json` includes positive prose with exact,
independently written IR oracles and negative ambiguous/unsupported prose. The
evaluator records each output, expected/actual acceptance, source hash and pass
status. An accept case passes only if its complete canonical IR matches its
oracle. This intentionally catches structurally valid but semantically wrong
output. Case IDs and step IDs are specified in the prose to avoid cosmetic ID
differences masquerading as semantic failures.

```bash
python -m agentic_workflow evaluate examples/semantic/evaluation-cases.json \
  --endpoint https://YOUR-HOST/v1/chat/completions \
  --model YOUR-MODEL --api-key-env WORKFLOW_AI_KEY \
  -o .state/live-evaluation.json
```

The CLI command explicitly runs a configured endpoint and labels its report
`mode=live`. Library callers testing a deterministic extractor or local HTTP
fixture use `evaluate_cases(cases, extractor, mode='fixture')`; that report says
`live_endpoint=not_tested`. A timeout, HTTP failure or unexpected extractor
exception is an evaluation error, never a successful refusal. No case executes
the emitted workflow.

The repository's HTTP fixture tests prove protocol handling, explicit refusal,
review integrity and evaluation scoring. They do not prove model quality. A live
model evaluation remains an external acceptance gate until it has been run with
an actual configured model. The report is finite example evidence; it is not a
guarantee of correctness for unseen Skills. Other independent SIT cases execute
reviewed workflows and verify business outputs and side effects.

## Python API

```python
from agentic_workflow.compilation import (
    compile_bundle, approve_bundle, verify_bundle, evaluate_cases,
    SemanticCompileProvider,
)

bundle = compile_bundle(skill_text, extractor=provider, optimize=True)
approved = approve_bundle(bundle, actor="reviewer")  # returns an isolated copy
workflow = verify_bundle(approved)                  # refuses pending/tampered bundles
```
