"""Explicit trusted test tools; real I/O fixtures, never advertised as connectors."""

import json
import os
from pathlib import Path
import time
import urllib.request


def get_quote(args, context):
    with urllib.request.urlopen(args['url'], timeout=5) as response:
        return json.load(response)


def timed_branch(args, context):
    start = time.monotonic()
    time.sleep(args.get('seconds', 0.4))
    end = time.monotonic()
    result = {'name': args['name'], 'pid': os.getpid(), 'start': start, 'end': end}
    (Path(context.workspace) / f"branch-{args['name']}.json").write_text(json.dumps(result))
    return result


def record_once(args, context):
    path = Path(context.workspace) / args.get('path', 'effects.jsonl')
    with path.open('a') as output:
        output.write(json.dumps({'step': context.step_id, 'key': context.idempotency_key}) + '\n')
    return {'recorded': True}


def slow_effect(args, context):
    (Path(context.workspace) / 'entered').write_text('ready')
    time.sleep(args.get('seconds', 3))
    (Path(context.workspace) / 'late-effect').write_text('must not occur after cancellation')
    return {'finished': True}


def plugin_tools(config):
    """Explicit local fixture plugin factory used to test remote execution."""
    if config:
        raise ValueError('SIT plugin accepts no configuration')
    return {'sit.slow_effect': slow_effect, 'sit.publish_then_delay': publish_then_delay}


def publish_then_delay(args, context):
    """Real fixture service accepts the effect before local step completion."""
    request = urllib.request.Request(args['url'], json.dumps({'invoice': args['invoice']}).encode(),
                                     {'Content-Type': 'application/json', 'Idempotency-Key': context.idempotency_key},
                                     method='POST')
    with urllib.request.urlopen(request, timeout=5) as response:
        receipt = json.load(response)
    (Path(context.workspace) / 'effect-accepted').write_text('accepted before local completion')
    time.sleep(0.8)
    return receipt
