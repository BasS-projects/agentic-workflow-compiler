"""Real authenticated API and remote processes; workers never open server SQLite."""

from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from .support import available_port, evidence_json, fixture_server, json_request, leaf, process, require, wait_until, workflow


@contextmanager
def api_server(directory):
    identities = {'viewer': ('read-only', 'viewer'), 'operator': ('ops-user', 'operator'),
                  'approver': ('finance-reviewer', 'approver'), 'admin': ('admin-user', 'admin'),
                  'worker-a': ('worker-a', 'worker'), 'worker-b': ('worker-b', 'worker')}
    tokens = {key: secrets.token_urlsafe(32) for key in identities}
    auth = {tokens[key]: {'actor': actor, 'role': role} for key, (actor, role) in identities.items()}
    auth_path = directory / 'ephemeral-auth.json'
    auth_path.write_text(json.dumps(auth))
    auth_path.chmod(0o600)
    port = available_port()
    url = f'http://127.0.0.1:{port}'
    command = [sys.executable, '-m', 'agentic_workflow.server', '--host', '127.0.0.1', '--port', str(port),
               '--db', str(directory / 'coordinator.sqlite3'), '--auth-file', str(auth_path)]
    try:
        with process(command, directory / 'api-server.log') as child:
            def healthy():
                if child.poll() is not None:
                    raise AssertionError('API process exited: ' + (directory / 'api-server.log').read_text())
                try:
                    return json_request(url + '/healthz')[0] == 200
                except OSError:
                    return False
            wait_until(healthy)
            yield API(url, tokens, directory)
    finally:
        auth_path.unlink(missing_ok=True)


class API:
    def __init__(self, url, tokens, directory):
        self.url, self.tokens, self.directory = url, tokens, directory

    def request(self, path, method='GET', body=None, role='operator', status=200):
        actual, payload = json_request(self.url + path, method, body, self.tokens.get(role))
        require(actual == status, f'{method} {path}: expected HTTP {status}; got {actual} {payload}')
        return payload

    def submit(self, spec, run_id, **kw):
        return self.request('/v1/runs', 'POST', {'workflow': spec, 'inputs': {}, 'run_id': run_id, **kw}, status=201)

    def get(self, run_id):
        return self.request('/v1/runs/' + run_id, role='viewer')

    def worker_command(self, worker, workspace=None, plugins=None, lease_seconds=4):
        command = [sys.executable, '-m', 'agentic_workflow.remote_worker', '--url', self.url,
                '--token-env', 'SIT_WORKER_TOKEN', '--worker-id', worker,
                '--workspace', str(workspace or self.directory / worker), '--once', '--lease-seconds', str(lease_seconds)]
        return command + (['--plugins', str(plugins)] if plugins else [])

    @contextmanager
    def worker(self, worker, suffix='', plugins=None, lease_seconds=4):
        env = {**os.environ, 'SIT_WORKER_TOKEN': self.tokens[worker]}
        with process(self.worker_command(worker, plugins=plugins, lease_seconds=lease_seconds), self.directory / f'{worker}{suffix}.log', env) as child:
            yield child

    def run_worker(self, worker, suffix=''):
        with self.worker(worker, suffix) as child:
            child.wait(timeout=30)
            require(child.returncode == 0, f'{worker} failed: ' + (self.directory / f'{worker}{suffix}.log').read_text())


def api_rbac_identity(directory):
    spec = workflow([leaf('value', 'core.value', {'value': 'invoice'})])
    with api_server(directory) as api:
        api.request('/v1/runs', role=None, status=401)
        api.request('/metrics', role=None, status=401)
        api.request('/v1/runs', 'POST', {'workflow': spec, 'inputs': {}}, role='viewer', status=403)
        api.request('/v1/jobs/claim', 'POST', {'worker_id': 'worker-a'}, role='operator', status=403)
        api.request('/v1/jobs/claim', 'POST', {'worker_id': 'worker-b'}, role='worker-a', status=403)
        submitted = api.submit(spec, 'rbac', actor='forged-admin', role='admin')
        events = api.request('/v1/runs/rbac/events', role='viewer')['events']
        require(events[0]['actor'] == 'ops-user', 'submission body forged authenticated identity')
        api.request('/v1/runs/rbac/approve', 'POST', {'step_path': 'release', 'approved': True},
                    role='operator', status=403)
        changed = workflow([leaf('value', 'core.value', {'value': 'tampered'})])
        api.request('/v1/runs', 'POST', {'workflow': changed, 'inputs': {}, 'run_id': 'rbac'}, status=409)
        duplicate = api.submit(spec, 'rbac')
        require(duplicate['run_id'] == submitted['run_id'], 'identical resubmission created another run')
        require(len(api.request('/v1/runs', role='viewer')['runs']) == 1, 'identity deduplication failed')
        metrics = api.request('/metrics', role='viewer')
        require('agentic_runs_total 1' in metrics, 'metrics did not reflect accepted submission')
        evidence_json(directory, 'audit-events.json', events)
    return {'unauthenticated': 401, 'viewer_write': 403, 'operator_claim': 403,
            'worker_impersonation': 403, 'forged_actor_ignored': True, 'identity_conflict': 409,
            'deduplicated_count': 1, 'authenticated_metrics_verified': True}


def remote_workers_exclusive(directory):
    spec = workflow([leaf('deliver', 'files.write_text', {'path': 'invoice.txt', 'text': 'ACME invoice 001'})],
                    outputs={'receipt_path': {'$ref': 'steps.deliver.path'}})
    with api_server(directory) as api:
        for name in ('invoice_a', 'invoice_b'):
            api.submit(spec, name)
        with ExitStack() as stack:
            children = [stack.enter_context(api.worker(worker)) for worker in ('worker-a', 'worker-b')]
            for child in children:
                child.wait(timeout=30)
                require(child.returncode == 0, 'remote worker process exited unsuccessfully')
        states = [api.get(name) for name in ('invoice_a', 'invoice_b')]
        require(all(item['status'] == 'completed' for item in states), f'remote deliveries failed: {states}')
        owners = {state['worker_id'] for state in states}
        require(owners == {'worker-a', 'worker-b'}, f'two workers did not both execute: {owners}')
        claim_counts = {}
        for name in ('invoice_a', 'invoice_b'):
            events = api.request(f'/v1/runs/{name}/events')['events']
            claim_counts[name] = sum(event['event'] == 'claimed' for event in events)
            require(claim_counts[name] == 1, 'a completed job was claimed more than once')
        files = list((directory / 'worker-a').rglob('invoice.txt')) + list((directory / 'worker-b').rglob('invoice.txt'))
        require(len(files) == 2, f'expect exactly two delivery files, observed {len(files)}')
        require(all(path.read_text() == 'ACME invoice 001' for path in files), 'delivered file content incorrect')
        # Compete for exactly one remaining job using two actual remote worker processes.
        api.submit(spec, 'contended')
        with ExitStack() as stack:
            children = [stack.enter_context(api.worker(worker, '-contention')) for worker in ('worker-a', 'worker-b')]
            for child in children:
                child.wait(timeout=30)
                require(child.returncode == 0, 'contending worker failed')
        require(api.get('contended')['status'] == 'completed', 'contended invoice never completed')
        events = api.request('/v1/runs/contended/events')['events']
        require(sum(event['event'] == 'claimed' for event in events) == 1, 'both workers owned the same lease')
        evidence_json(directory, 'remote-run-states.json', states)
        evidence_json(directory, 'contention-events.json', events)
    return {'transport': 'actual HTTP over loopback; two independent OS worker processes',
            'workers': sorted(owners), 'claims_per_initial_run': claim_counts, 'contended_claim_count': 1,
            'delivered_content': 'ACME invoice 001', 'coordinator_db_shared_with_workers': False}


def lease_fencing_recovery_schedule(directory):
    spec = workflow([leaf('deliver', 'files.write_text', {'path': 'recovered-invoice.txt', 'text': 'Recovered ACME invoice'})])
    with api_server(directory) as api:
        api.submit(spec, 'uncertain')
        old = api.request('/v1/jobs/claim', 'POST', {'worker_id': 'worker-a', 'lease_seconds': 0.2}, role='worker-a')['job']
        require(old['run_id'] == 'uncertain', 'claim selected incorrect run')
        # A real lease elapsed without heartbeat; no SQLite state is edited by the test.
        time.sleep(0.25)
        replacement = api.request('/v1/jobs/claim', 'POST', {'worker_id': 'worker-b'}, role='worker-b')['job']
        require(replacement is None, 'uncertain external effects were automatically replayed')
        require(api.get('uncertain')['status'] == 'needs_recovery', 'expired lease was not surfaced for recovery')
        stale = {'worker_id': 'worker-a', 'lease_token': old['lease_token'],
                 'result': {'status': 'completed', 'run_id': 'uncertain', 'outputs': {'forged': True}}}
        api.request('/v1/jobs/uncertain/complete', 'POST', stale, role='worker-a', status=409)
        api.request('/v1/jobs/uncertain/heartbeat', 'POST', stale, role='worker-a', status=409)
        api.request('/v1/runs/uncertain/recover', 'POST', {'retry': True}, role='viewer', status=403)
        api.request('/v1/runs/uncertain/recover', 'POST', {'retry': True})
        api.run_worker('worker-b', '-recovery')
        current = api.get('uncertain')
        require(current['status'] == 'completed', f'authorized recovery failed: {current}')
        require(current['lease_token'] > old['lease_token'], 'recovery did not advance fencing token')
        api.request('/v1/jobs/uncertain/complete', 'POST', stale, role='worker-a', status=409)
        delivered = list((directory / 'worker-b').rglob('recovered-invoice.txt'))
        require(len(delivered) == 1 and delivered[0].read_text() == 'Recovered ACME invoice', 'recovery delivery wrong')
        # Cancellation advances the token and rejects a previously valid completion as well.
        api.submit(spec, 'cancelled_lease')
        active = api.request('/v1/jobs/claim', 'POST', {'worker_id': 'worker-a'}, role='worker-a')['job']
        api.request('/v1/runs/cancelled_lease/cancel', 'POST', {})
        api.request('/v1/jobs/cancelled_lease/complete', 'POST',
                    {'worker_id': 'worker-a', 'lease_token': active['lease_token'],
                     'result': {'status': 'completed', 'run_id': 'cancelled_lease', 'outputs': {}}},
                    role='worker-a', status=409)
        schedule = api.request('/v1/schedules', 'POST', {'workflow': spec, 'inputs': {},
                              'interval_seconds': 0.7, 'schedule_id': 'invoice_schedule'}, status=201)
        time.sleep(max(0, schedule['next_at'] - time.time()) + 0.03)
        first = api.request('/v1/schedules/tick', 'POST', {})['runs']
        second = api.request('/v1/schedules/tick', 'POST', {})['runs']
        require(len(first) == 1 and second == [], 'repeated schedule tick duplicated one due occurrence')
        api.run_worker('worker-a', '-scheduled')
        require(api.get(first[0]['run_id'])['status'] == 'completed', 'scheduled invoice did not execute')
        events = api.request('/v1/runs/uncertain/events')['events']
        evidence_json(directory, 'lease-events.json', events)
        evidence_json(directory, 'scheduled-run.json', api.get(first[0]['run_id']))
    return {'real_lease_expiry': True, 'uncertain_effects_require_recovery': True,
            'stale_complete_and_heartbeat_http': 409, 'recovery_completed': True,
            'cancelled_lease_complete_http': 409, 'same_due_tick_run_counts': [len(first), len(second)],
            'scheduled_delivery_completed': True}


def remote_approval_restart(directory):
    spec = workflow([leaf('prepare', 'files.write_text', {'path': 'prepared.txt', 'text': 'Prepared invoice'}),
                     {'id': 'release', 'kind': 'approval', 'prompt': 'Release invoice?'},
                     leaf('publish', 'files.write_text', {'path': 'released.txt', 'text': 'Released invoice'})], version='0.2')
    with api_server(directory) as api:
        api.submit(spec, 'approval_remote')
        api.run_worker('worker-a', '-approval-before')
        paused = api.get('approval_remote')
        require(paused['status'] == 'waiting_approval', f'remote invoice not awaiting review: {paused}')
        prepared = list((directory / 'worker-a').rglob('prepared.txt'))
        require(len(prepared) == 1, 'worker preparation missing')
        before_mtime = prepared[0].stat().st_mtime_ns
        require(not list((directory / 'worker-a').rglob('released.txt')), 'invoice released before review')
        api.request('/v1/runs/approval_remote/approve', 'POST', {'step_path': 'release', 'approved': True,
                    'actor': 'forged-user'}, role='operator', status=403)
        api.request('/v1/runs/approval_remote/approve', 'POST', {'step_path': 'release', 'approved': True,
                    'actor': 'forged-user'}, role='approver')
        # Same worker identity + its durable local state, but a brand new OS process.
        api.run_worker('worker-a', '-approval-after')
        completed = api.get('approval_remote')
        require(completed['status'] == 'completed', f'remote approved invoice failed: {completed}')
        require(prepared[0].stat().st_mtime_ns == before_mtime, 'completed prepare step replayed after approval restart')
        released = list((directory / 'worker-a').rglob('released.txt'))
        require(len(released) == 1 and released[0].read_text() == 'Released invoice', 'approved publication missing')
        events = api.request('/v1/runs/approval_remote/events', role='viewer')['events']
        approved = [event for event in events if event['event'] == 'approved']
        require(len(approved) == 1 and approved[0]['actor'] == 'finance-reviewer', 'approval audit trusted body actor')
        evidence_json(directory, 'remote-approval-events.json', events)
    return {'waiting_before_approval': True, 'new_process_resume': True, 'preparation_not_replayed': True,
            'published_after_approval': True, 'approval_actor': 'finance-reviewer'}


def remote_active_cancellation(directory):
    spec = workflow([leaf('remote_effect', 'sit.slow_effect', {'seconds': 3}, timeout_seconds=8),
                     leaf('publish', 'files.write_text', {'path': 'released.txt', 'text': 'must not publish'})])
    plugin_path = directory / 'trusted-fixture-plugin.json'
    plugin_path.write_text(json.dumps({'plugins': [{'entrypoint': 'sit.fixture_tools:plugin_tools', 'config': {}}]}))
    with api_server(directory) as api:
        api.submit(spec, 'active_remote_cancel')
        with api.worker('worker-a', '-cancel', plugins=plugin_path) as child:
            def entered():
                return bool(list((directory / 'worker-a').rglob('entered')))
            wait_until(entered, timeout=12, message='remote action never started')
            started = time.monotonic()
            api.request('/v1/runs/active_remote_cancel/cancel', 'POST', {})
            child.wait(timeout=5)
            require(child.returncode == 0, 'cancelled remote supervisor exited unsuccessfully')
            duration = time.monotonic() - started
        time.sleep(max(0, 3.2 - duration))
        require(api.get('active_remote_cancel')['status'] == 'cancelled', 'worker overwrote coordinator cancellation')
        require(not list((directory / 'worker-a').rglob('late-effect')), 'remote cancellation leaked delayed child effect')
        require(not list((directory / 'worker-a').rglob('released.txt')), 'remote downstream publication ran after cancellation')
        events = api.request('/v1/runs/active_remote_cancel/events')['events']
        evidence_json(directory, 'remote-cancellation-events.json', events)
    return {'transport': 'real coordinator and remote worker OS processes', 'tool_loading': 'explicit trusted fixture plugin',
            'coordinator_status': 'cancelled', 'worker_stop_seconds': round(duration, 3),
            'late_effect_absent_after_original_deadline': True, 'downstream_publication_absent': True}


def remote_abrupt_crash_takeover(directory):
    plugin_path = directory / 'trusted-fixture-plugin.json'
    plugin_path.write_text(json.dumps({'plugins': [{'entrypoint': 'sit.fixture_tools:plugin_tools', 'config': {}}]}))
    with fixture_server(directory) as (service_url, service_state), api_server(directory) as api:
        spec = workflow([leaf('deliver', 'sit.publish_then_delay', {'url': service_url + '/publish',
                                                                  'invoice': 'ACME-001'}, timeout_seconds=6)],
                        outputs={'receipt': {'$ref': 'steps.deliver.receipt'}})
        api.submit(spec, 'crash_takeover')
        with api.worker('worker-a', '-crash', plugins=plugin_path, lease_seconds=1) as child:
            wait_until(lambda: bool(list((directory / 'worker-a').rglob('effect-accepted'))),
                       timeout=10, message='vendor never accepted initial delivery')
            old = api.get('crash_takeover')
            require(old['status'] == 'running', 'worker finished before crash could be injected')
            child.kill()  # SIGKILL: no graceful shutdown or coordinator fail call.
            child.wait(timeout=3)
            require(child.returncode < 0, 'supervisor was not killed by a signal')
        wait_until(lambda: api.get('crash_takeover')['status'] == 'needs_recovery', timeout=4,
                   message='abrupt worker death did not require recovery after real lease expiry')
        # Old local executor can still finish after supervisor loss. Observe it settle
        # before authorizing replay; fencing cannot undo an already accepted effect.
        wait_until(lambda: bool(list((directory / 'worker-a').rglob('result.json'))), timeout=3,
                   message='bounded orphan fixture executor did not settle')
        ledger_before = json.loads((service_state / 'delivery-ledger.json').read_text())
        require(len(ledger_before['attempts']) == 1 and len(ledger_before['deliveries']) == 1,
                'initial vendor delivery not independently recorded')
        no_job = api.request('/v1/jobs/claim', 'POST', {'worker_id': 'worker-b'}, role='worker-b')['job']
        require(no_job is None, 'replacement automatically replayed uncertain work')
        api.request('/v1/runs/crash_takeover/recover', 'POST', {'retry': True})
        with api.worker('worker-b', '-takeover', plugins=plugin_path) as replacement:
            replacement.wait(timeout=10)
            require(replacement.returncode == 0, 'authorized replacement failed')
        recovered = api.get('crash_takeover')
        require(recovered['status'] == 'completed' and recovered['worker_id'] == 'worker-b',
                f'new worker did not complete authorized takeover: {recovered}')
        ledger = json.loads((service_state / 'delivery-ledger.json').read_text())
        require(len(ledger['attempts']) == 2, 'replacement did not actually attempt delivery')
        require(len({attempt['key'] for attempt in ledger['attempts']}) == 1,
                'takeover changed the effect idempotency key')
        require(ledger['deliveries'] == [{'invoice': 'ACME-001', 'receipt': 'DELIVERED-001'}],
                'idempotent fixture service delivered duplicate invoice')
        api.request('/v1/jobs/crash_takeover/complete', 'POST', {
            'worker_id': 'worker-a', 'lease_token': old['lease_token'],
            'result': {'status': 'completed', 'run_id': 'crash_takeover', 'outputs': {'receipt': 'stale'}}},
                    role='worker-a', status=409)
        evidence_json(directory, 'takeover-events.json', api.request('/v1/runs/crash_takeover/events')['events'])
        evidence_json(directory, 'vendor-delivery-ledger.json', ledger)
    return {'crash': 'actual SIGKILL of remote supervisor after HTTP effect accepted',
            'lease_expiry_requires_recovery': True, 'replacement_worker': 'worker-b',
            'http_delivery_attempts': 2, 'unique_idempotency_keys': 1, 'vendor_deliveries': 1,
            'old_owner_completion_http': 409,
            'limitation': 'Deduplication proved for the idempotent fixture service; external services must implement that contract.'}
