"""Run independent system scenarios and emit JSON plus JUnit evidence.

Usage: python -m sit.run --output .state/sit-report.json
External gates are reported as skipped with a reason, never silently passed.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

from . import distributed_scenarios as distributed
from . import scenarios
from .support import ROOT


CASES = {
    'semantic_review': (1, scenarios.semantic_review),
    'langgraph_parity': (2, scenarios.langgraph_parity),
    'advanced_batch_parallel': (3, scenarios.advanced_batch_parallel),
    'advanced_approval_cancellation': (3, scenarios.advanced_approval_cancellation),
    'api_rbac_identity': (4, distributed.api_rbac_identity),
    'remote_workers_exclusive': (3, distributed.remote_workers_exclusive),
    'lease_fencing_recovery_schedule': (4, distributed.lease_fencing_recovery_schedule),
    'remote_approval_restart': (3, distributed.remote_approval_restart),
    'remote_active_cancellation': (3, distributed.remote_active_cancellation),
    'remote_abrupt_crash_takeover': (3, distributed.remote_abrupt_crash_takeover),
    'browser_vendor_quote': (4, scenarios.browser_vendor_quote),
    'desktop_real': (4, scenarios.desktop_real),
    'live_semantic_provider': (1, scenarios.live_semantic_provider),
}


def git_revision():
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_dirty():
    try:
        return bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True,
                                            stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def package_versions():
    result = {}
    for name in ('agentic-workflow-compiler', 'langgraph', 'playwright', 'cloudpickle'):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def source_fingerprint():
    """Identify tested local code even before the integration commit is made."""
    digest = hashlib.sha256()
    files = sorted([*ROOT.glob('src/**/*.py'), *ROOT.glob('sit/**/*.py'), ROOT / 'pyproject.toml'])
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def junit(report, path):
    suite = ET.Element('testsuite', name='agentic-workflow-independent-sit', tests=str(len(report['scenarios'])),
                       failures=str(report['summary']['failed']), skipped=str(report['summary']['skipped']),
                       time=str(report['duration_seconds']))
    for row in report['scenarios']:
        case = ET.SubElement(suite, 'testcase', name=row['id'], classname='sit.phase' + str(row['phase']),
                             time=str(row['duration_seconds']))
        if row['status'] == 'failed':
            ET.SubElement(case, 'failure', message=row['error']).text = row.get('traceback', '')
        elif row['status'] == 'skipped':
            ET.SubElement(case, 'skipped', message=row['reason'])
        else:
            ET.SubElement(case, 'system-out').text = json.dumps(row['evidence'], ensure_ascii=False, indent=2)
    ET.indent(suite)
    ET.ElementTree(suite).write(path, encoding='utf-8', xml_declaration=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.state/sit-report.json'))
    parser.add_argument('--case', action='append', choices=sorted(CASES), help='Run only named scenario(s)')
    parser.add_argument('--require-case', action='append', choices=sorted(CASES), default=[], help='Return nonzero if this scenario is skipped')
    parser.add_argument('--require-all', action='store_true', help='Return nonzero for any skipped external gate too')
    args = parser.parse_args(argv)
    selected = args.case or list(CASES)
    if set(args.require_case) - set(selected):
        parser.error('--require-case must also be included in the selected cases')
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    artifacts = output.parent / 'sit-artifacts' / stamp
    artifacts.mkdir(parents=True)
    report = {'schema_version': '1.0', 'started_at': datetime.now(timezone.utc).isoformat(),
              'git_revision': git_revision(), 'git_dirty': git_dirty(), 'source_tree_sha256': source_fingerprint(),
              'source_fingerprint_scope': 'Sorted relative path + NUL + bytes + NUL for src/**/*.py, sit/**/*.py, pyproject.toml; excludes .state and build.',
              'python': platform.python_version(), 'platform': platform.platform(),
              'packages': package_versions(), 'artifact_directory': str(artifacts), 'scenarios': [],
              'required_cases': args.require_case, 'require_all': args.require_all, 'full_suite': args.case is None,
              'scope': 'Actual local processes, HTTP, durable files, optional installed browser/desktop; no production cloud claim.'}
    for name in selected:
        phase, function = CASES[name]
        directory = artifacts / name
        directory.mkdir()
        before = time.monotonic()
        row = {'id': name, 'phase': phase, 'artifact_directory': str(directory)}
        try:
            row['evidence'] = function(directory)
            row['status'] = 'passed'
        except scenarios.GateSkipped as exc:
            row.update(status='skipped', reason=str(exc))
        except Exception as exc:
            row.update(status='failed', error=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())
        row['duration_seconds'] = round(time.monotonic() - before, 3)
        report['scenarios'].append(row)
        print(f"{row['status'].upper():7} {name} ({row['duration_seconds']:.3f}s)"
              + (': ' + row.get('error', row.get('reason', '')) if row['status'] != 'passed' else ''), flush=True)
    report['duration_seconds'] = round(time.monotonic() - started, 3)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    report['summary'] = {status: sum(row['status'] == status for row in report['scenarios'])
                         for status in ('passed', 'failed', 'skipped')}
    required_skips = any(row['status'] == 'skipped' and row['id'] in args.require_case for row in report['scenarios'])
    report['gate_requirements_met'] = not (required_skips or (args.require_all and report['summary']['skipped']))
    report['status'] = ('failed' if report['summary']['failed'] else 'required_gate_missing' if not report['gate_requirements_met']
                        else 'passed_with_skips' if report['summary']['skipped'] else 'passed')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    xml_path = output.with_suffix('.xml')
    junit(report, xml_path)
    print(json.dumps({'status': report['status'], **report['summary'], 'json': str(output), 'junit': str(xml_path)}))
    return 1 if report['summary']['failed'] or required_skips or (args.require_all and report['summary']['skipped']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
