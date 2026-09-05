"""Small process/HTTP helpers shared by SIT scenarios."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def leaf(identifier, tool, args=None, **kw):
    return {'id': identifier, 'kind': 'tool', 'tool': tool, 'args': args or {}, **kw}


def workflow(steps, *, version='0.1', inputs=None, outputs=None, identifier='sit_workflow'):
    return {'ir_version': version, 'id': identifier, 'inputs': inputs or {},
            'steps': steps, 'outputs': outputs or {}}


def json_request(url, method='GET', body=None, token=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw.decode('utf-8', 'replace')
        return response.status, data


def available_port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


def wait_until(predicate, *, timeout=15, message='condition did not become true'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.04)
    raise AssertionError(message)


@contextmanager
def process(command, log_path, env=None):
    with Path(log_path).open('w') as log:
        child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                 env=env or os.environ.copy(), start_new_session=True)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


@contextmanager
def fixture_server(directory):
    port = available_port()
    state = directory / 'fixture'
    command = [sys.executable, '-m', 'sit.fixture_server', '--port', str(port), '--state', str(state)]
    url = f'http://127.0.0.1:{port}'
    with process(command, directory / 'fixture-server.log') as child:
        def healthy():
            if child.poll() is not None:
                raise AssertionError('fixture server exited: ' + (directory / 'fixture-server.log').read_text())
            try:
                return json_request(url + '/healthz')[0] == 200
            except OSError:
                return False
        wait_until(healthy)
        yield url, state


def evidence_json(directory, name, value):
    path = directory / name
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return str(path)
