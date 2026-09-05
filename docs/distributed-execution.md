# Distributed execution and API

The coordinator owns one durable SQLite queue. Each remote worker talks to it over
HTTP and owns its own runtime database and workspace. Workers do not mount or open
the coordinator database. The deployed coordinator is a single service with a
persistent local disk; running independent coordinator replicas against unrelated
databases is not a supported high-availability deployment.

## Authentication and roles

Create an authentication file mapping randomly generated tokens to identities:

```json
{
  "REPLACE_WITH_RANDOM_OPERATOR_TOKEN": {"actor": "operator-bas", "role": "operator"},
  "REPLACE_WITH_RANDOM_APPROVER_TOKEN": {"actor": "reviewer-bas", "role": "approver"},
  "REPLACE_WITH_RANDOM_WORKER_TOKEN": {"actor": "worker-1", "role": "worker"}
}
```

These strings are placeholders, not usable deployment secrets. Generate at least
32 random bytes per token with `secrets.token_urlsafe(32)`; protect the file with
owner-only permissions. The API rejects tokens shorter than 16 characters.
`deploy/init.py` generates deployment credentials and configuration.

| Role | Read runs/events/metrics | Submit/cancel/recover/schedule | Approve/reject | Claim/heartbeat/result |
| --- | --- | --- | --- | --- |
| viewer | Yes | No | No | No |
| operator | Yes | Yes | No | No |
| approver | Yes | No | Yes | No |
| worker | No | No | No | Yes |
| admin | Yes | Yes | Yes | Yes |

Actors come from the authenticated token. A body cannot grant itself another
role or actor. `worker_id` must match the token's actor, so give each independent
worker a distinct identity. Roles apply across the installation; per-project or
per-run tenancy is not implemented. The console and `/healthz` are public. All
API operations and `/metrics` require a bearer token. UI tokens remain in memory.
No CORS wildcard is configured. Put TLS in front of the API when crossing an
untrusted network; the example deployment binds the published port to loopback.

```bash
python -m agentic_workflow.server --host 127.0.0.1 --port 8080 \
  --db .state/coordinator.db --auth-file .state/auth.json --tick-seconds 1
```

The tick poller is disabled unless `--tick-seconds` is positive. An empty auth map
can expose the public health/console on loopback, but grants no API access.
Non-loopback binding rejects an empty map.

## HTTP protocol

All request bodies are JSON objects, limited to 2 MiB; duplicate keys, nonfinite
numbers and excessive nesting are rejected. Errors are JSON with an `error`
string: 400 malformed request, 401 authentication, 403 role or worker identity,
404 unknown resource, 409 identity/state/lease conflict, and 413 oversized body.

| Method and path | Body | Response |
| --- | --- | --- |
| `POST /v1/runs` | `workflow`, `inputs` (default `{}`), optional `run_id`, `replay_safe` | Run object (201) |
| `GET /v1/runs?limit=100` | — | `{"runs":[...]}` |
| `GET /v1/runs/ID` | — | Run object |
| `GET /v1/runs/ID/events` | — | `{"events":[...]}` |
| `POST /v1/runs/ID/cancel` | `{}` | Run object |
| `POST /v1/runs/ID/approve` | `step_path`, boolean `approved` | Run object |
| `POST /v1/runs/ID/recover` | boolean `retry` (default `true`) | Run object |
| `POST /v1/jobs/claim` | `worker_id`, optional `lease_seconds` | `{"job": run_or_null}` |
| `POST /v1/jobs/ID/heartbeat` | `worker_id`, `lease_token`, optional `lease_seconds` | Renewed lease |
| `POST /v1/jobs/ID/complete` | `worker_id`, `lease_token`, `result` | Run object |
| `POST /v1/jobs/ID/fail` | `worker_id`, `lease_token`, string `error` | Run object |
| `GET /v1/schedules` | — | `{"schedules":[...]}` |
| `POST /v1/schedules` | `workflow`, `inputs`, `interval_seconds`, optional `schedule_id` | Schedule object (201) |
| `POST /v1/schedules/tick` | `{}` | `{"runs":[...]}` |
| `GET /metrics` | — | Prometheus text |

`workflow` accepts IR 0.1, IR 0.2, or a reviewed compile bundle. A compile bundle
must pass its source, IR, provenance and approval hash checks before submission.
Raw IR is an explicit operator submission. Bundle review is an integrity
attestation; API RBAC independently controls submission and runtime approvals.

A run contains `run_id`, `workflow`, `inputs`, `status`, `result`, `error`,
`approvals`, `decisions`, `actor`, timestamps, `attempts`, `worker_id`,
`required_worker_id`, `lease_token`, `lease_until`, `replay_safe`, `resume` and
`recovery_requested`. Outputs appear in `result.outputs`. Waiting approvals are
`[{"step_path":"review","prompt":"Release?"}]`. Decisions are keyed by exact
step path with `{ "approved": true, "actor": "reviewer-bas" }` values.

## Remote workers and checkpoints

```bash
# Export AWC_WORKER_TOKEN using your local secret manager or owner-only env file.
python -m agentic_workflow.remote_worker \
  --url http://127.0.0.1:8080 --token-env AWC_WORKER_TOKEN \
  --worker-id worker-1 --workspace .state/worker-1 \
  --lease-seconds 60 --poll-seconds 1 --health-file .state/worker-1/health.json
```

`--once` attempts one claim, completes that claimed invocation (including pausing
for approval), and exits. Without it the worker continuously polls. A waiting
approval does not keep the executor process alive. Reuse the same worker ID and
persistent workspace on restart. Requests never choose Python imports; trusted
plugins are loaded only from the worker's local `--plugins CONFIG.json`.

Each run uses `WORKSPACE/runs/SHA256_RUN_ID/runtime.db` and a separate `workspace/`
subdirectory. Submitted file paths refer to that run workspace. The API does not
upload initial files, replicate workspaces, or stream file artifacts; workflows
can produce input files from JSON inputs, or operators can stage required files
in the known run directory. Output references and audit events are central;
large/local file artifacts require an explicit artifact service in a deployment.

Optional AI tools are configured independently with `--ai-config providers.json`:

```json
{
  "ai.summarize": {
    "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
    "model": "your-model",
    "api_key_env": "MODEL_API_KEY",
    "timeout_seconds": 30
  }
}
```

Omit `api_key_env` for an endpoint that requires no key. Secrets are resolved
only from the executor's environment; the coordinator bearer variable is removed
from the executor environment. Provider requests enforce their configured endpoint
and reject redirects. Runtime AI execution requires explicit `kind: "ai"` steps.

Health JSON contains `status`, `worker_id`, `updated_at` Unix seconds and `pid`.
A healthy probe must require a recent timestamp **and** status `idle` or
`executing`; `disconnected` and `stopped` are unhealthy.

## Leases, cancellation and explicit recovery

A claim is an atomic transaction. Its monotonically increasing integer token and
worker identity are required for heartbeat, completion and failure writes. A
stale token cannot change central state. Workers heartbeat every
`min(5 seconds, lease_seconds / 3)` while a separate executor process runs.
The supervisor stops the executor on a failed heartbeat or lost lease. If the
supervisor itself is abruptly killed, its executor can survive until the current
tool timeout; reconcile that outcome before authorizing a replacement. Cancellation
revokes the lease immediately; active work is stopped at the next heartbeat, plus
the request timeout and process cleanup grace. With the default lease this is
normally within about five seconds on a reachable coordinator; a blocked network
request can add up to ten seconds. This is cooperative distributed cancellation,
not a guarantee to reverse an already accepted third-party request.

Cleanup enumerates descendants, including leaf processes with independent
sessions, and terminates them. Linux host-mounted `/proc` is translated through
`NSpid` to avoid confusing host and container PIDs. Tool code remains trusted and
must not daemonize or deliberately evade supervision.

By default an expired lease or crashed executor moves the run to
`needs_recovery`; another worker will not execute it. Inspect external receipts,
reconcile unknown effects, and explicitly authorize retry with the operator
recover endpoint. `retry:false` cancels instead. Recovery is audited with the
possibility of repeated external effects.

`replay_safe:true` is an explicit submission contract allowing automatic retry
following lease expiry. Set it only when every relevant operation is repeatable
or its receiver enforces the stable idempotency key. Lease fencing protects the
queue; it cannot fence a third-party service or promise exactly-once effects.

Approval resumes are pinned to their original worker so its durable completed
nodes are retained. Another worker cannot claim that queue item. If checkpoints
are lost and any approval decisions were recorded, even explicit operator recovery
refuses execution: submit a new run ID and request fresh approval. Replaying earlier
AI/tool outputs can change what the reviewer originally approved, so decisions
cannot be transferred to reconstructed state by step path alone. For runs with no
recorded decisions, explicit operator recovery can release affinity and reconstruct
a fresh workspace; earlier effects may repeat. No shared/network SQLite database
is used as a distributed lock.

## Schedules and evidence

Schedules persist an interval and the next due Unix timestamp. Tick performs run
creation and advancement in one transaction, derives an occurrence ID from the
schedule and due timestamp, and prevents duplicate execution when concurrent
tickers see the same occurrence. Missed intervals coalesce into one job while
preserving the cadence; they do not create an unbounded catch-up storm. Schedule
runs default to `replay_safe:false`. Calendar cron, timezone/DST rules and schedule
edit/delete are outside this interval scheduler's interface.

Tests prove exclusive concurrent claims, immutable submission identity, stale
fencing, explicit recovery, cancellation, actor/role separation, body limits,
schedule duplicate ticks, real HTTP worker processes with separate disks,
heartbeat lease extension and approval restart without rewriting prior output.
The independent SIT runner adds business outcomes and cross-component scenarios.
This proves local process and HTTP behavior; external cloud account deployment
and live model behavior have separate execution gates.
