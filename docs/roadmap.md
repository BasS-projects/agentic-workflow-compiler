# Design phase coverage — version 0.2.0

This records implemented scope separately from evidence that requires a real
external environment. See [validation](validation.md) and [SIT plan](sit-plan.md)
for actual execution results. M2 calls for one backend; LangGraph is that backend.

| Phase | Delivered implementation | Acceptance evidence |
| --- | --- | --- |
| M0 Foundation | Structured extraction, strict IR0.1, deterministic tools/AI, retries/timeouts, SQLite resume, CLI/Codespaces | Original regression suite and document CLI pipeline |
| M1 Semantic compilation | Review bundles, source/IR/provenance hashes, diagnostics, explicit rejection protocol, conservative optimizer, exact expected-workflow evaluation | Bundle review/tamper, ambiguity/unsupported fixtures and optimizer parity; live model evaluation is a separate configured gate |
| M2 First backend | Native LangGraph StateGraph execution, generated executable artifact, explicit capability rejection | Python/LangGraph output, retry, skip, failure and resume parity |
| M3 Advanced execution | Versioned IR0.2/migration, bounded foreach/parallel, durable approvals/cancel, central leased queue and HTTP workers | Real concurrent processes, durable checkpoints, denial/cancel, exclusive claims, stale fencing, lease loss and uncertain recovery |
| M4 Interaction/operations | API/RBAC, web console, interval scheduling, Prometheus metrics, local plugins, Playwright browser/X11 desktop RPA, container deployment/health/backup guidance | Actual HTTP/process integration, real browser/desktop gates, Docker Compose smoke and output verification |

## Explicit choices

- IR0.1 behavior remains compatible. IR0.2 defines structured control flow;
  it is not silently lowered to a less capable backend.
- LangGraph runs IR0.1 natively. Temporal, n8n, GitHub Actions as an execution
  backend, and Azure Durable Functions remain possible future adapters, not
  integrations claimed by this release. GitHub Actions currently provides CI.
- One coordinator owns its SQLite queue. Remote workers never open that file.
  Workers have independent durable workspaces. Lease fencing protects queue
  state; external side effects still need deduplication/reconciliation.
- Human action approval uses authenticated API roles. Compile bundle review
  binds an acknowledgement to content hashes; it is not a signed authorization
  artifact from an identity provider.
- Browser automation uses Playwright and explicit origins; desktop automation
  uses X11/xdotool. Both run as trusted worker tools, outside compiler logic.
- Deployment is an API/worker Compose stack with durable volumes. Production
  TLS, cloud account, routing, monitoring integration and backup destination
  depend on the selected deployment environment.

## External acceptance gates

1. **Live semantic model:** configure an endpoint/model/credential and run the
   supplied semantic corpus. Recorded responses cannot establish model quality.
2. **Production cloud rollout:** select the target account/host, configure TLS
   and private credentials, deploy, and repeat smoke/restore checks there.
3. **Customer RPA workflow:** replace local browser/desktop fixtures with the
   authorized target application's selectors, identities and action approvals.

These gates are reported as unexecuted until evidence exists; local test success
and CI container deployment are not a claim that a public service is provisioned.
