# Run the coordinator and remote workers

This deployment runs the existing Python compiler/runtime with an authenticated
HTTP coordinator and its embedded operations console. Worker processes receive
jobs through HTTP and keep their own execution checkpoints. The API database is
not mounted in the worker. Building or starting these services does not provision
a cloud account or publish a public endpoint.

## Start with Docker Compose

Requirements: Docker Engine or Docker Desktop with Compose v2, Python 3.11+, and
network access for the first image build. From the repository root on Linux or
macOS:

```sh
sh deploy/local.sh
```

The script generates local credentials, builds the services, waits for their
health checks, and executes a real API/worker smoke test. Open
http://127.0.0.1:8080 and obtain an operator token in your terminal:

```sh
python3 deploy/init.py --token operator
```

Paste that token into the console. The browser stores it only in page memory;
reloading or disconnecting clears it. Select **Submit workflow**, keep or edit the
included approval example, and submit. It normalizes document text, waits for a
review, and then writes `output/approved.txt` in that run's worker workspace.
Approve using a separate approver token:

```sh
python3 deploy/init.py --token approver
```

Connect with that token and approve the pending step in Runs. The operator can
submit/cancel/recover jobs, while the approver can issue review decisions.
Alternatively use the generated admin token for an isolated local demonstration.

On PowerShell, or to run the steps individually:

```sh
python deploy/init.py
docker compose up --build -d --wait
python deploy/smoke.py --docker
docker compose ps
```

The default image includes the LangGraph dependency. The queue worker executes
the version-appropriate local runtime; choose LangGraph explicitly through the
backend CLI/API documented in the architecture. Installing LangGraph does not
implicitly change the remote worker's execution engine.

`deploy/init.py` generates unique tokens with Python's `secrets` module, binds
the worker token to actor `worker-1`, and writes its files under `.state/deploy`.
That directory is owner-only on POSIX. The mounted JSON files are readable by
the container's nonroot UID; the private parent directory prevents other local
users from reading them. The environment/token files remain owner-only.
Complete existing configuration is preserved on subsequent runs. A partial or
inconsistent configuration fails instead of silently rotating credentials.
`.state` is excluded from Git and from the Docker build context.

The Compose port is bound to `127.0.0.1`. Set `AWC_PORT` to choose a different local
port; when using a custom port, supply that URL to the smoke script. Services run
as UID/GID 10001, with a read-only root filesystem, dropped capabilities, and
dedicated durable volumes. The worker health check examines its recent status
file and reports disconnected/stopped workers as unhealthy. The runtime image
includes `procps`, which the worker needs to stop descendant task processes.

## Roles and API

The authorization file maps generated bearer tokens to fixed actor/role records.
The server obtains actor identity from this file, not from submitted request
bodies. Worker actor identity must equal the configured `--worker-id`.

| Operation | Viewer | Operator | Approver | Admin | Worker |
|---|---|---|---|---|---|
| Read runs, schedules, events, metrics | Yes | Yes | Yes | Yes | No |
| Submit, cancel, recover, create schedules, tick | No | Yes | No | Yes | No |
| Approve or reject pending steps | No | No | Yes | Yes | No |
| Claim, heartbeat, complete, fail leased jobs | No | No | No | Yes | Yes |

`/healthz` and the console shell are public; the data endpoints require a bearer
token. The console sends requests only to the same origin. Results, IDs, errors,
and prompts are rendered as text, including strings resembling HTML. Tokens are
not written into URLs or local/session storage. There is no wildcard CORS.

| Method and path | Request or response |
|---|---|
| `GET /v1/runs` | `{"runs": [...]}` |
| `POST /v1/runs` | `{workflow, inputs, run_id?, replay_safe?}`; returns the run with HTTP 201 |
| `GET /v1/runs/ID` | Run metadata, workflow, inputs, results, pending approvals |
| `GET /v1/runs/ID/events` | `{"events": [...]}` |
| `POST /v1/runs/ID/approve` | `{step_path, approved}`; `false` rejects |
| `POST /v1/runs/ID/cancel` | `{}` |
| `POST /v1/runs/ID/recover` | `{"retry": true}` after reviewing uncertain effects |
| `GET /v1/schedules` | `{"schedules": [...]}` |
| `POST /v1/schedules` | `{workflow, inputs, interval_seconds, schedule_id?}` |
| `POST /v1/schedules/tick` | `{}`; queues due occurrences |
| `GET /metrics` | Prometheus text, authenticated |

Submit an immutable `run_id` when the client may retry the same request. Reusing
the ID with a different workflow, inputs, or replay policy is rejected.
Authentication failure returns 401; an authenticated role lacking permission
returns 403. A stale lease or invalid state transition returns 409.

Compose enables an interval scheduler tick every two seconds. The server's
`--tick-seconds 0` setting disables automatic ticking; an authorized API tick
remains available. Duplicate ticks do not queue a duplicate occurrence. This
release supports creating/listing interval schedules; it does not expose a
schedule editing/deletion API. Use a separate deployment for temporary SIT
schedules rather than adding them to an operational coordinator.

## State, interruptions, and recovery

| Location | Durable content |
|---|---|
| Compose `coordinator-data` volume | Coordinator database, jobs, leases, approvals, schedules, events |
| Compose `worker-data` volume | Per-run runtime database, workspaces, executor logs/results |
| `.state/deploy` | Local auth file, role tokens, worker environment, trusted plugin configuration |

Worker run directories use `runs/SHA256(run_id)/`. Their `workspace/` subdirectory
contains output files, while `runtime.db` holds execution checkpoints. Approval
resumption stays on the worker that owns the checkpoints. Multiple machines must
have distinct worker identities, tokens, and durable workspaces; do not scale the
provided singleton worker with a shared identity/volume. Add explicit workers
with distinct actor mappings for a multi-host deployment.

A lease fences coordinator writes; it cannot make an already-issued external
side effect disappear. The default expired/interrupted run requires operator
recovery. Inspect the run's events, checkpoints and the target system before
choosing **Recover and retry**. Only select `replay_safe` on submission when all
effects can safely repeat. Workers supervise their executor subprocess and stop
its descendants on cancellation or lease loss; a remote external operation
already accepted by another service may still finish.

Stop/restart commands preserve named volumes:

```sh
docker compose stop
docker compose start
docker compose logs --tail 100 api worker
```

For an application-consistent backup, stop both services before backing up both
named volumes and the private `.state/deploy` configuration. Restore them as a
matching set and preserve file ownership. Do not copy a live SQLite database file
alone or discard its journal/WAL. `docker compose down` preserves named volumes;
adding `-v` destroys the stored execution history and workspaces.

After updating the checkout, rebuild with `docker compose up --build -d --wait`
and rerun `python deploy/smoke.py --docker`. Take a stopped-service backup before
upgrading a deployment whose execution history matters.

## Browser-enabled worker

The optional `rpa` image installs Playwright and its real Chromium browser. Edit
`.state/deploy/plugins.json` to register only the origins the workflows need:

```json
{
  "plugins": [
    {
      "builtin": "browser",
      "config": {"allowed_origins": ["https://your-test-application.example"]}
    }
  ]
}
```

The example hostname must be replaced with your actual controlled test origin.
Then start the browser worker:

```sh
docker compose -f compose.yaml -f deploy/compose.browser.yaml up --build -d --wait
```

Use the same two `-f` arguments for subsequent Compose operations on that stack.
The browser worker has additional shared memory for Chromium. Browser execution
is a trusted tool capability with an origin allowlist, not a general network
sandbox. Do not register untrusted Python entrypoints as plugins. See
[tool-plugins.md](tool-plugins.md) for the action contract and desktop/X11 setup.
The standard Compose deployment has no desktop session; actual desktop RPA needs
its own X11 display and explicit plugin setup.

## Run without Docker

Create a virtual environment and install the package, then generate credentials:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[all]'
python deploy/init.py
python -m agentic_workflow.server --host 127.0.0.1 --port 8080 --db .state/coordinator.db --auth-file .state/deploy/auth.json --tick-seconds 2
```

In another terminal, activate the same environment and load the generated worker
environment file before starting the worker:

```sh
set -a
. .state/deploy/worker.env
set +a
python -m agentic_workflow.remote_worker --url http://127.0.0.1:8080 --token-env AWC_WORKER_TOKEN --worker-id worker-1 --workspace .state/worker --plugins .state/deploy/plugins.json --health-file .state/worker/health
```

Run `python deploy/smoke.py` in a third terminal. It reports container file
inspection as `not_run`, because no Docker container is involved. Live model
providers require separately supplied endpoint, model, and credential
environment variables; no paid model call is part of deployment initialization.

## Verification and cloud handoff

`deploy/smoke.py --docker` tests public health, anonymous read rejection, viewer
write rejection, operator submission, a worker reaching a pending review,
operator approval rejection, approver acceptance, the final business result,
audit history, and the exact output file bytes inside the actual container. It
writes `.state/docker-smoke.json` and exits nonzero on a failed check. It never
turns an unavailable Docker test into a passed container result.

`python deploy/test_console.py` starts disposable API/worker processes and opens
the actual console with Playwright Chromium. It submits and approves through the
UI, checks the completed output and event history, verifies hostile strings stay
inert, checks that tokens are cleared on reload, and captures desktop/mobile
screenshots. Install Chromium with `python -m playwright install --with-deps
chromium` in the CI/test environment first. Results are recorded in
`.state/ui-report.json` with screenshots under `.state/ui-proof/`. Missing browser
dependencies fail this browser gate instead of producing a simulated pass.

The broader system integration tests are documented in
[sit-plan.md](sit-plan.md). Unit, browser, desktop, model, and Docker results are
separate gates. A passing mock provider test is not a live model quality result,
and a local API/worker test is not a container or production cloud deployment.

For a cloud VM, copy this checkout and freshly generated target-specific
configuration, attach durable storage, and run the same Compose deployment.
Expose it through your authenticated HTTPS ingress or a private network; the
provided local HTTP port binding should remain private. Use a single coordinator
writer deployment with its SQLite store. High-availability replicated
coordinators and provider-specific managed database migration are not provided.
No production cloud destination or account is assumed by these files.
