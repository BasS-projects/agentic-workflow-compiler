"""Business outcome oracles independent of production runtime implementation."""

import copy
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

from .fixture_tools import get_quote, record_once, slow_effect, timed_branch
from .support import evidence_json, fixture_server, leaf, require, wait_until, workflow


class GateSkipped(Exception):
    pass


class KnownSkillExtractor:
    """Fixed contract fixture. It does not measure language-model quality."""

    def extract(self, text):
        if text != 'Normalize the supplied document and return normalized text.':
            raise ValueError('ambiguous or unsupported request')
        return normalization_workflow()


def normalization_workflow():
    return workflow([
        leaf('normalize', 'text.normalize', {'text': {'$ref': 'inputs.text'}}),
        leaf('optional', 'core.value', {'value': 'not enabled'}, when={'equals': [False, True]}),
        leaf('write', 'files.write_text', {'path': 'normalized.txt', 'text': {'$ref': 'steps.normalize.text'}}),
    ], inputs={'text': {'type': 'string'}}, outputs={'normalized': {'$ref': 'steps.normalize.text'},
                                                    'optional': {'$ref': 'steps.optional.value'}})


def semantic_review(directory):
    from agentic_workflow.compilation import approve_bundle, compile_bundle, verify_bundle
    from agentic_workflow.runtime import Runtime
    text = 'Normalize the supplied document and return normalized text.'
    bundle = compile_bundle(text, extractor=KnownSkillExtractor(), optimize=True)
    try:
        verify_bundle(bundle)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError('unreviewed semantic output was executable')
    approved = approve_bundle(bundle, actor='sit-reviewer')
    validated = verify_bundle(approved)
    runtime = Runtime(directory / 'runtime.sqlite3', directory / 'workspace')
    result = runtime.run(validated, {'text': '  Purchase order\n  ACME  '}, run_id='reviewed')
    require(result['status'] == 'completed', f'reviewed bundle failed: {result}')
    require(result['outputs']['normalized'] == 'Purchase order\nACME', 'reviewed workflow changed business content')
    tampered = copy.deepcopy(approved)
    tampered['workflow']['steps'][0]['args']['text'] = 'unapproved replacement'
    try:
        verify_bundle(tampered)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError('IR tampering retained approval')
    source_tampered = copy.deepcopy(approved)
    source_tampered['provenance']['source_sha256'] = '0' * 64
    try:
        verify_bundle(source_tampered)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError('source hash tampering retained approval')
    for ambiguous in ('Process it appropriately.', 'Run an arbitrary shell command.'):
        try:
            compile_bundle(ambiguous, extractor=KnownSkillExtractor())
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError('unsupported fixture request was accepted')
    evidence_json(directory, 'approved-bundle.json', approved)
    return {'normalized': result['outputs']['normalized'], 'ir_tamper_rejected': True,
            'source_tamper_rejected': True, 'unreviewed_rejected': True,
            'semantic_provider': 'deterministic contract fixture; not a model quality evaluation'}


def langgraph_parity(directory):
    from agentic_workflow.backends.langgraph import LangGraphBackend, LangGraphRuntime
    from agentic_workflow.runtime import Runtime
    from agentic_workflow.worker import default_tools
    if importlib.util.find_spec('langgraph') is None:
        raise AssertionError('required LangGraph integration dependency missing; install .[backends]')
    artifacts = {}
    with fixture_server(directory) as (url, fixture_state):
        for name, factory in [('python', Runtime), ('langgraph', LangGraphRuntime)]:
            workspace = directory / name
            runtime = factory(directory / (name + '.sqlite3'), workspace,
                              tools={**default_tools(), 'sit.get_quote': get_quote})
            spec = normalization_workflow()
            success = runtime.run(spec, {'text': '  Invoice\n ACME 001  '}, run_id='normalize')
            require(success['status'] == 'completed', f'{name} normalization failed: {success}')
            require(success['outputs'] == {'normalized': 'Invoice\nACME 001', 'optional': None},
                    f'{name} produced incorrect normalized invoice')
            require((workspace / 'normalized.txt').read_text() == 'Invoice\nACME 001', 'file output wrong')
            steps = runtime.inspect('normalize')['steps']
            require([item['status'] for item in steps] == ['completed', 'skipped', 'completed'],
                    f'{name} skipped action incorrectly executed')
            missing = workflow([leaf('read', 'files.read_text', {'path': 'source.txt'}),
                                leaf('write', 'files.write_text', {'path': 'recovered.txt',
                                                                  'text': {'$ref': 'steps.read.text'}})])
            failed = runtime.run(missing, {}, run_id='outage')
            require(failed['status'] == 'failed', f'{name} missing input silently succeeded')
            require(not (workspace / 'recovered.txt').exists(), 'dependent side effect ran after failure')
            (workspace / 'source.txt').write_text('Recovered invoice')
            recovered = runtime.run(missing, {}, run_id='outage', resume=True)
            require(recovered['status'] == 'completed', f'{name} could not resume after source arrived')
            require((workspace / 'recovered.txt').read_text() == 'Recovered invoice', 'resume content differs')
            retry_spec = workflow([leaf('quote', 'sit.get_quote', {'url': url + '/retry?channel=' + name},
                                        retry={'max_attempts': 2, 'delay_seconds': 0})],
                                  outputs={'amount_thb': {'$ref': 'steps.quote.amount_thb'}})
            quote = runtime.run(retry_spec, {}, run_id='vendor_outage')
            require(quote['status'] == 'completed' and quote['outputs']['amount_thb'] == 1250,
                    f'{name} failed transient vendor outage: {quote}')
            retry_steps = runtime.inspect('vendor_outage')['steps']
            require(retry_steps[0]['attempts'] == 2, f'{name} did not retry exactly once')
            artifacts[name] = {'success_outputs': success['outputs'], 'step_statuses': [s['status'] for s in steps],
                               'outage_initial': failed['status'], 'outage_resumed': recovered['status'],
                               'retry_outputs': quote['outputs'], 'retry_attempts': retry_steps[0]['attempts']}
        require(artifacts['python'] == artifacts['langgraph'], 'native graph and Python outcomes differ')
        counts = json.loads((fixture_state / 'request-counts.json').read_text())
        require(counts == {'python': 2, 'langgraph': 2}, 'HTTP service observed unexpected request counts')
    artifact = LangGraphBackend().compile(normalization_workflow())
    evidence_json(directory, 'backend-artifact.json', artifact)
    evidence_json(directory, 'parity.json', artifacts)
    return {'engines': ['Python runtime', 'actual LangGraph StateGraph'], 'business_outcomes': artifacts,
            'real_http_request_counts': counts}


def advanced_batch_parallel(directory):
    from agentic_workflow.advanced_ir import validate_workflow_v2
    from agentic_workflow.advanced_runtime import AdvancedRuntime
    from agentic_workflow.worker import default_tools
    docs = ['  First invoice ', '  Second invoice\n', '  Third invoice  ']
    spec = workflow([{'id': 'batch', 'kind': 'foreach', 'items': {'$ref': 'inputs.documents'}, 'max_items': 3,
                      'steps': [leaf('normalize', 'text.normalize', {'text': {'$ref': 'loop.item'}})]}],
                    version='0.2', inputs={'documents': {'type': 'array'}},
                    outputs={'items': {'$ref': 'steps.batch.items'}, 'count': {'$ref': 'steps.batch.count'}})
    runtime = AdvancedRuntime(directory / 'batch.sqlite3', directory / 'batch')
    result = runtime.run(spec, {'documents': docs}, run_id='batch')
    require(result['status'] == 'completed', f'batch failed: {result}')
    actual = [entry['normalize']['text'] for entry in result['outputs']['items']]
    require(actual == ['First invoice', 'Second invoice', 'Third invoice'], 'batch lost/reordered documents')
    require(result['outputs']['count'] == 3, 'batch count wrong')
    try:
        overflow = runtime.run(spec, {'documents': docs + ['fourth']}, run_id='overflow')
    except ValueError:
        overflow = {'status': 'rejected'}
    require(overflow['status'] in ('failed', 'rejected'), 'batch maximum was not enforced')
    branches = {name: [leaf('quote', 'sit.timed_branch', {'name': name, 'seconds': 0.4})]
                for name in ('alpha', 'beta', 'gamma', 'delta')}
    parallel = workflow([{'id': 'vendors', 'kind': 'parallel', 'branches': branches, 'max_workers': 2}],
                        version='0.2', outputs={'vendors': {'$ref': 'steps.vendors.branches'}})
    workers = AdvancedRuntime(directory / 'parallel.sqlite3', directory / 'parallel',
                              tools={**default_tools(), 'sit.timed_branch': timed_branch})
    outcome = workers.run(parallel, {}, run_id='parallel')
    require(outcome['status'] == 'completed', f'parallel failed: {outcome}')
    intervals = [values['quote'] for values in outcome['outputs']['vendors'].values()]
    timeline = sorted([(row['start'], 1) for row in intervals] + [(row['end'], -1) for row in intervals])
    active = peak = 0
    for _, delta in timeline:
        active += delta
        peak = max(peak, active)
    require(peak == 2, f'expected real overlapping work bounded at 2; observed {peak}')
    require(len({row['pid'] for row in intervals}) >= 2, 'branches did not run in separate processes')
    invalid = copy.deepcopy(parallel)
    invalid['steps'][0]['max_workers'] = 9
    try:
        validate_workflow_v2(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError('unsafe concurrency bound accepted')
    evidence_json(directory, 'parallel-timing.json', {'intervals': intervals, 'peak_active': peak})
    return {'normalized_documents': actual, 'batch_overflow_rejected': True, 'peak_active': peak,
            'distinct_processes': len({row['pid'] for row in intervals}), 'timing': intervals}


def _run_slow_advanced(db, workspace, spec):
    from agentic_workflow.advanced_runtime import AdvancedRuntime
    from agentic_workflow.worker import default_tools
    runtime = AdvancedRuntime(db, workspace, tools={**default_tools(), 'sit.slow_effect': slow_effect})
    result = runtime.run(spec, {}, run_id='active_cancel')
    (Path(workspace) / 'cancel-result.json').write_text(json.dumps(result))


def advanced_approval_cancellation(directory):
    from agentic_workflow.advanced_runtime import AdvancedRuntime
    from agentic_workflow.worker import default_tools
    tools = {**default_tools(), 'sit.record': record_once}
    spec = workflow([leaf('prepare', 'sit.record'), {'id': 'release', 'kind': 'approval',
                     'prompt': 'Release normalized invoice to accounting?'},
                     leaf('publish', 'files.write_text', {'path': 'released.txt', 'text': 'Invoice released'})],
                    version='0.2')
    workspace, db = directory / 'approval', directory / 'approval.sqlite3'
    runtime = AdvancedRuntime(db, workspace, tools=tools)
    initial = runtime.run(spec, {}, run_id='approved')
    require(initial['status'] == 'waiting_approval', f'invoice not paused: {initial}')
    require(not (workspace / 'released.txt').exists(), 'invoice released before approval')
    runtime.close()
    reopened = AdvancedRuntime(db, workspace, tools=tools)
    require(reopened.inspect('approved')['status'] == 'waiting_approval', 'approval not durable across restart')
    reopened.approve('approved', 'release', True, actor='finance-reviewer')
    finished = reopened.run(spec, {}, run_id='approved', resume=True)
    require(finished['status'] == 'completed', f'approved invoice failed: {finished}')
    require((workspace / 'released.txt').read_text() == 'Invoice released', 'approved invoice missing')
    require(len((workspace / 'effects.jsonl').read_text().splitlines()) == 1, 'completed preparation replayed')
    denied_workspace = directory / 'denied'
    denied = AdvancedRuntime(directory / 'denied.sqlite3', denied_workspace, tools=tools)
    denied.run(spec, {}, run_id='denied')
    denied.approve('denied', 'release', False, actor='finance-reviewer')
    denied_result = denied.run(spec, {}, run_id='denied', resume=True)
    require(denied_result['status'] == 'failed', 'denied invoice did not terminate as failed')
    require(not (denied_workspace / 'released.txt').exists(), 'denied invoice was published')
    cancel_workspace, cancel_db = directory / 'cancelled', directory / 'cancelled.sqlite3'
    cancellation = AdvancedRuntime(cancel_db, cancel_workspace)
    slow_spec = workflow([leaf('slow', 'sit.slow_effect', {'seconds': 3}, timeout_seconds=8),
                          leaf('publish', 'files.write_text', {'path': 'released.txt', 'text': 'bad'})], version='0.2')
    child = multiprocessing.get_context('spawn').Process(target=_run_slow_advanced,
                                                         args=(str(cancel_db), str(cancel_workspace), slow_spec))
    child.start()
    try:
        wait_until(lambda: (cancel_workspace / 'entered').exists(), timeout=12, message='slow action never started')
        start = time.monotonic()
        cancellation.cancel('active_cancel')
        child.join(4)
        require(not child.is_alive(), 'active cancellation did not stop executor within 4 seconds')
        require(child.exitcode == 0, 'cancelled executor crashed')
        duration = time.monotonic() - start
        result = json.loads((cancel_workspace / 'cancel-result.json').read_text())
        require(result['status'] == 'cancelled', f'cancel status was {result}')
        # Wait beyond the original action's completion time: a leaked child would write this file.
        time.sleep(max(0, 3.2 - duration))
        require(not (cancel_workspace / 'late-effect').exists(), 'cancelled child left a delayed side effect')
        require(not (cancel_workspace / 'released.txt').exists(), 'dependent publication ran after cancellation')
    finally:
        if child.is_alive():
            child.kill()
            child.join()
        child.close()
    evidence_json(directory, 'approval-events.json', reopened.events('approved'))
    return {'approved_after_restart': True, 'preparation_side_effect_count': 1, 'denied_status': denied_result['status'],
            'active_cancel_status': result['status'], 'cancel_stop_seconds': round(duration, 3),
            'late_effect_absent_after_original_deadline': True}


def browser_vendor_quote(directory):
    if importlib.util.find_spec('playwright') is None:
        raise GateSkipped('Playwright is not installed; real browser was not exercised. Install .[rpa] and Chromium.')
    from agentic_workflow.rpa import browser_tools
    from agentic_workflow.runtime import Runtime
    from agentic_workflow.worker import default_tools
    with fixture_server(directory) as (url, state):
        workspace = directory / 'browser'
        tools = {**default_tools(), **browser_tools([url])}
        spec = workflow([leaf('intake', 'browser.run', {'url': url + '/vendor-quote', 'actions': [
            {'action': 'fill', 'selector': '#vendor', 'value': 'ACME'},
            {'action': 'fill', 'selector': '#amount', 'value': '1250'},
            {'action': 'click', 'selector': '#submit'},
            {'action': 'assert_text', 'selector': '#receipt', 'value': 'Accepted ACME quote: 1250.00 THB'},
            {'action': 'text', 'selector': '#receipt', 'name': 'receipt'},
            {'action': 'screenshot', 'path': 'vendor-quote.png'},
            {'action': 'download', 'selector': '#download', 'path': 'receipt.txt'},
        ]}, timeout_seconds=40)], outputs={'texts': {'$ref': 'steps.intake.texts'}})
        result = Runtime(directory / 'browser.sqlite3', workspace, tools=tools).run(spec, {}, run_id='vendor_quote')
        if result['status'] != 'completed' and ('BrowserUnavailableError' in str(result)):
            raise GateSkipped('Playwright package exists but Chromium is unavailable; real browser gate not executed.')
        require(result['status'] == 'completed', f'browser intake failed: {result}')
        require(result['outputs']['texts']['receipt'] == 'Accepted ACME quote: 1250.00 THB',
                'rendered vendor receipt incorrect')
        require((state / 'receipt.txt').read_text() == 'Accepted ACME quote: 1250.00 THB', 'server did not accept quote')
        require((workspace / 'receipt.txt').read_text() == 'Accepted ACME quote: 1250.00 THB', 'download content incorrect')
        png = (workspace / 'vendor-quote.png').read_bytes()
        require(png[:8] == b'\x89PNG\r\n\x1a\n' and len(png) > 1000, 'browser screenshot is not real PNG evidence')
        width, height = struct.unpack('>II', png[16:24])
        require(width >= 640 and height >= 400, 'browser screenshot unexpectedly small')
    return {'engine': 'real Playwright Chromium', 'server_receipt': 'Accepted ACME quote: 1250.00 THB',
            'download_verified': True, 'screenshot': str(workspace / 'vendor-quote.png'),
            'screenshot_dimensions': [width, height]}


def desktop_real(directory):
    required = ['Xvfb', 'xdotool']
    missing = [name for name in required if not shutil.which(name)]
    if importlib.util.find_spec('tkinter') is None:
        missing.append('Python tkinter')
    if importlib.util.find_spec('PIL') is None:
        missing.append('Pillow')
    if missing:
        raise GateSkipped('Real desktop gate requires a disposable X11 display; missing: ' + ', '.join(missing))
    from agentic_workflow.desktop import desktop_tools
    from agentic_workflow.runtime import Runtime
    from agentic_workflow.worker import default_tools
    from .support import ROOT, process
    display = ':' + str(100 + os.getpid() % 500)
    environment = {**os.environ, 'DISPLAY': display}
    receipt = directory / 'desktop-receipt.txt'
    with process(['Xvfb', display, '-screen', '0', '1024x768x24', '-nolisten', 'tcp'],
                 directory / 'xvfb.log', environment):
        def display_ready():
            result = subprocess.run(['xdotool', 'getdisplaygeometry'], env=environment, capture_output=True)
            return result.returncode == 0
        wait_until(display_ready, message='disposable Xvfb display did not start')
        fixture = ROOT / 'examples' / 'desktop_fixture.py'
        with process([sys.executable, str(fixture), str(receipt)], directory / 'desktop-window.log', environment) as window:
            def window_ready():
                if window.poll() is not None:
                    raise AssertionError('desktop fixture exited: ' + (directory / 'desktop-window.log').read_text())
                result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', '^Workflow Desktop SIT$'],
                                        env=environment, capture_output=True)
                return result.returncode == 0
            wait_until(window_ready, message='real Tk window did not become visible')
            previous = os.environ.get('DISPLAY')
            os.environ['DISPLAY'] = display
            try:
                spec = workflow([leaf('review', 'desktop.run', {'actions': [
                    {'action': 'focus', 'title': 'Workflow Desktop SIT'},
                    {'action': 'type', 'text': 'ACME 1250'},
                    {'action': 'key', 'keys': 'Return'},
                    {'action': 'assert_title', 'value': 'Saved: ACME 1250'},
                    {'action': 'assert_pixel', 'x': 460, 'y': 220, 'rgb': [25, 135, 84]},
                    {'action': 'screenshot', 'path': 'desktop.png'},
                ]}, timeout_seconds=15)])
                result = Runtime(directory / 'desktop.sqlite3', directory / 'desktop',
                                 tools={**default_tools(), **desktop_tools()}).run(spec, {}, run_id='desktop')
            finally:
                if previous is None:
                    os.environ.pop('DISPLAY', None)
                else:
                    os.environ['DISPLAY'] = previous
            require(result['status'] == 'completed', f'real desktop failed: {result}')
            require(receipt.read_text() == 'ACME 1250', 'real desktop input did not save expected vendor quote')
            png = directory / 'desktop' / 'desktop.png'
            require(png.exists() and png.read_bytes().startswith(b'\x89PNG'), 'real desktop screenshot missing')
    return {'engine': 'real Xvfb X11 and Tk window', 'saved_receipt': 'ACME 1250',
            'title_and_success_pixel_verified': True, 'screenshot': str(png)}


def live_semantic_provider(directory):
    endpoint, model = os.environ.get('SIT_LLM_ENDPOINT'), os.environ.get('SIT_LLM_MODEL')
    if not endpoint or not model:
        raise GateSkipped('No SIT_LLM_ENDPOINT and SIT_LLM_MODEL configured; live semantic extraction and model quality remain unproved.')
    from agentic_workflow.compilation import approve_bundle, compile_bundle, verify_bundle
    from agentic_workflow.providers import ChatCompletionsProvider
    from agentic_workflow.runtime import Runtime
    provider = ChatCompletionsProvider(endpoint, model, api_key=os.environ.get('SIT_LLM_API_KEY'))
    text = ('Create workflow normalize_invoice. It has one required string input text. '
            'Use only text.normalize to trim each line and remove leading and trailing blank lines in inputs.text. '
            'Return the normalized text in workflow outputs.normalized. Do not write files or call other tools.')
    bundle = compile_bundle(text, extractor=provider)
    spec = verify_bundle(approve_bundle(bundle, actor='sit-outcome-oracle'))
    require(len(spec['steps']) == 1 and spec['steps'][0]['tool'] == 'text.normalize',
            'live extraction invented additional actions')
    result = Runtime(directory / 'live.sqlite3', directory / 'live').run(spec, {'text': '  ACME invoice 001  '})
    require(result['status'] == 'completed' and result['outputs'] == {'normalized': 'ACME invoice 001'},
            f'live semantic extraction failed held business oracle: {result}')
    # Keep the source and output hashes; the provider constructor contains a credential and is not serialized.
    evidence_json(directory, 'live-bundle.json', bundle)
    return {'model': model, 'live_http': True, 'cases': 1, 'expected': 'ACME invoice 001',
            'limitation': 'One narrow acceptance case; not evidence of general model quality.'}
