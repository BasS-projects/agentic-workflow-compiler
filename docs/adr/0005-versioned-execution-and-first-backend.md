# ADR 0005: Versioned execution, LangGraph, and a leased coordinator

Status: accepted for version 0.2.0. Extends the original MVP decisions without
changing the meaning of existing IR0.1 workflows.

## Context

The design requires semantic review, a first executable backend, advanced control
flow and operational deployment. Implicit loops or shared worker SQLite would
make failure and resume semantics ambiguous.

## Decision

Use an explicitly versioned IR0.2 for structured foreach, named parallel branches
and approval checkpoints. The advanced runtime uses spawned task processes and
bounded concurrency with durable local checkpoints. Compile bundles retain the
source and hash-bound review; only constant conditions may be optimized.

Choose LangGraph for the first backend. Native StateGraph nodes execute the
supported IR0.1 sequence and use durable reference-compatible step state. Reject
IR0.2 instead of silently flattening loops, approvals or concurrency.

Use a central HTTP coordinator with transactional SQLite queue ownership,
expiring leases, monotonically increasing fencing tokens and role-based action
permissions. Each worker persists its own state. Approval resumes retain worker
affinity; explicit recovery can release affinity after effect reconciliation.

Ship a same-origin operations console, interval scheduler, metrics and Compose
API/worker stack. Browser and desktop tools are explicit locally configured
plugins. Their selectors and actions are never compiler-side execution.

## Consequences

A single coordinator is deployable and recoverable from a consistent backup, but
is not a highly available consensus service. Leases fence coordinator writes and
do not establish exactly-once effects in third-party systems. A lost worker's
local checkpoint may require reconciled replay. Python plugin code is trusted;
subprocess isolation and browser origin policy are not tenant security sandboxes.

SIT must prove actual concurrency, restart, denial, cancellation, stale ownership
and output effects, plus explicitly distinguish live external gates from fixtures.
