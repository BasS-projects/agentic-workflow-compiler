# Independent system integration proof

Run from a checkout with Python 3.11+ on POSIX:

```bash
python -m pip install -e '.[all]'
python -m sit.run --output .state/sit-report.json
```

The runner creates JSON and JUnit XML beside each other and preserves evidence
under `.state/sit-artifacts/<UTC timestamp>/`. It exits nonzero for any failed
scenario. Every unavailable external gate has an explicit `skipped` reason.
`passed_with_skips` does **not** prove the skipped capabilities. The report records
the git revision, a hash of the actual tested source, dependency versions,
individual durations, observed outcomes, and artifact paths.
`git_dirty` distinguishes an uncommitted integration checkout from the named
revision. `source_tree_sha256` hashes each sorted relative path, a NUL delimiter,
its bytes, and another NUL for `src/**/*.py`, `sit/**/*.py`, and `pyproject.toml`.
Runtime evidence and build outputs are excluded from that source fingerprint.

The acceptance oracles describe business results rather than implementation
internals. No production database, external account, or user files are needed.
All coordinator tests start a real authenticated server in a separate process;
remote worker processes communicate over loopback HTTP and use separate local
workspaces. The scenario code never edits the coordinator SQLite database.

| Scenario | Business acceptance oracle | Executed boundary |
| --- | --- | --- |
| `semantic_review` | Invoice normalization executes only after review; changing either approved source or IR invalidates review | Compile bundle, hashing, validation, real runtime and output file; deterministic extractor fixture |
| `langgraph_parity` | Python and LangGraph preserve invoice lines, omit disabled action, stop on missing source, resume once source arrives, retry exactly once after HTTP 503 | Actual LangGraph StateGraph, SQLite checkpoints, real files and separate HTTP fixture process |
| `advanced_batch_parallel` | Three documents retain their order; fourth exceeds the declared bound; four vendor branches genuinely overlap with peak concurrency exactly two | Foreach output oracle; process IDs and monotonic start/end intervals from actual child processes |
| `advanced_approval_cancellation` | Preparation runs once across restart; approved invoice publishes; denied invoice never publishes; active cancellation stops its delayed effect | Durable runtime reopen, independent cancellation caller, child processes and final file absence after original action deadline |
| `api_rbac_identity` | Anonymous callers cannot read; viewers cannot submit; operators cannot approve or claim; workers cannot impersonate; request body cannot forge actor; changed duplicate ID is rejected | Real HTTP 401/403/409 responses, audit identity, protected metrics |
| `remote_workers_exclusive` | Two remote workers each deliver one invoice; a later single invoice has one claimant despite two competing workers | Two independent OS processes, HTTP leased queue, exact delivered file content |
| `lease_fencing_recovery_schedule` | Expired ownership cannot complete/heartbeat; uncertain work requires operator recovery; cancellation invalidates old completion; repeated tick creates one due occurrence and the worker delivers it | Real wall-clock lease expiry, HTTP fencing, replacement worker, actual schedule tick |
| `remote_approval_restart` | New worker process resumes approved invoice without rewriting preparation; approval audit names token owner | API approval, remote runtime checkpoint, same worker identity and persisted workspace across process exit |
| `remote_active_cancellation` | Cancellation reaches a running remote executor; its delayed effect and downstream publication remain absent past their deadline | Explicit trusted test plugin, actual worker supervision and coordinator fencing |
| `remote_abrupt_crash_takeover` | After supervisor SIGKILL following an accepted invoice delivery, lease expires, replay requires approval, replacement preserves idempotency key, vendor ledger contains one delivery | Actual killed OS process, HTTP vendor ledger, wall-clock expiry, fresh worker workspace and stale-owner rejection |
| `browser_vendor_quote` | Filling ACME / 1250, submitting, and downloading produces `Accepted ACME quote: 1250.00 THB` | Real Chromium through Playwright, JS form, HTTP server receipt, downloaded text and PNG screenshot |
| `desktop_real` | Typing `ACME 1250` and pressing Return saves that receipt; window title and green success pixel match | Real disposable Xvfb X11 display, Tk application, xdotool input and Pillow screenshot |
| `live_semantic_provider` | An explicitly configured model extracts one normalization action whose real execution produces the held expected output | Actual configured chat-completions endpoint; skipped unless environment explicitly configured |

`text.normalize` preserves internal spacing and line boundaries, trims each line,
and removes blank lines from the beginning and end. The normalization examples
assert this existing behavior; they do not claim to summarize or understand text.

## Require real browser and desktop in CI

On Ubuntu, install `xvfb`, `xdotool`, and `python3-tk`; install the project's extras
and Playwright Chromium with its OS dependencies:

```bash
python -m playwright install --with-deps chromium
python -m sit.run --output .state/sit-report.json \
  --require-case browser_vendor_quote --require-case desktop_real
```

The desktop scenario starts its own disposable display. It does not interact with
a user's active desktop. A missing required browser/desktop gate returns nonzero
and marks `gate_requirements_met: false`, even though its scenario is correctly
reported as skipped. `--require-all` also requires the live LLM gate. Use
`--case NAME` repeatedly to rerun a diagnosed failure without repeating the suite.

## Explicit live model gate

Set `SIT_LLM_ENDPOINT` to the full chat-completions URL and `SIT_LLM_MODEL` to the
chosen model. If required by that endpoint, supply `SIT_LLM_API_KEY` through the
environment; do not put credentials in the workflow or reports. Then run:

```bash
python -m sit.run --case live_semantic_provider --require-all \
  --output .state/sit-live-report.json
```

This opt-in sends the fixed synthetic Skill to the configured endpoint and can
consume provider quota. One passing case is evidence for that narrow extraction
and execution, not an estimate of general semantic accuracy. The fixed extractor
in `semantic_review` tests the integration/review contract and is never reported
as model-quality evidence. The separate compiler evaluation fixtures in
`examples/semantic/` exercise accept/reject and full-IR comparisons.

## What this does not establish

Local HTTP/process tests do not establish multi-host network availability,
production scale, cloud deployment health, or vendor account integration. Lease
fencing protects coordinator writes; it cannot undo an external action already
accepted by a third-party service. Recovery stays explicit for uncertain effects.
Cancellation assertions use a cooperative supervisory boundary plus process
termination; they do not promise rollback of completed external side effects.

The current backend proof selects LangGraph. Temporal, n8n, GitHub Actions as a
workflow engine, and Azure Durable Functions remain separate future backend
implementations. A CI YAML file alone is not evidence of those backend adapters.

The SIT artifact directory contains synthetic workflow inputs and runtime
checkpoints. API bearer tokens are generated per scenario and their temporary
auth file is removed during cleanup. Logs and report summaries never include
those tokens. Preserve JSON, JUnit, PNGs, and event files with the reviewed commit
when recording release evidence.
