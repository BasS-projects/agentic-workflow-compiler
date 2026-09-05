# Trusted tool plugins and real RPA

The operator chooses which tools a worker can execute. Workflow IR names a
registered tool, such as `browser.run`; it never contains Python imports, shell
commands, module paths or browser JavaScript to evaluate. Plugin configuration is
a local, operator-owned file and must not be accepted from an API submission.

## Configuration and Python SDK

```json
{
  "plugins": [
    {
      "builtin": "browser",
      "config": {"allowed_origins": ["http://127.0.0.1:8081"]}
    }
  ]
}
```

```python
import json
from agentic_workflow.plugins import load_tool_plugins
from agentic_workflow.runtime import Runtime

registry = load_tool_plugins(json.load(open("plugins.json")))
runtime = Runtime(".state/runs.sqlite3", ".state/workspace", tools=registry)
print(registry.metadata)  # Names, versions, module SHA-256; no config secrets.
```

A custom trusted package exports a factory receiving an explicit configuration
dictionary and returning `{tool_name: callable}`. Each callable receives
`(args, TaskContext)` and returns a JSON-compatible dictionary. Context contains
`workspace`, `run_id`, `step_id`, and a stable `idempotency_key`. Use the key with
an external service that supports idempotency; external side effects are not
automatically exactly-once. Tool names must be dotted identifiers. Duplicate
names and overrides of built-in core/file tools are rejected.

```python
# company_tools.py — installed and reviewed by the operator
__version__ = "1.0.0"

def factory(config):
    factor = config["factor"]
    def multiply(args, context):
        return {"value": args["value"] * factor}
    return {"company.multiply": multiply}
```

```json
{"plugins": [{"entrypoint": "company_tools:factory", "config": {"factor": 7}}]}
```

An optional `sha256` field pins the entrypoint module file. A mismatch is rejected
before that module is imported. Python package discovery can import parent
packages, so the whole installed package remains trusted. The hash identifies
only that file; it does not cover dependencies or establish publisher identity.
Factories execute with the worker's OS privileges. Restrict package installation
and configuration permissions and use a worker/container with appropriate access.

## Browser worker

Install the optional dependencies and a real Chromium executable:

```bash
python -m pip install '.[rpa]'
python -m playwright install --with-deps chromium
```

`browser_tools(allowed_origins)` returns `{"browser.run": callable}`. Each call
launches real Chromium in a fresh, ephemeral browser context. No real user login,
browser profile, cookie store or saved credential is reused. Missing Playwright,
Chromium or OS dependencies produces `BrowserUnavailableError`; no mock success
is returned.

Tool arguments:

```json
{
  "url": "http://127.0.0.1:8081/vendor-quote",
  "headless": true,
  "timeout_ms": 10000,
  "actions": [
    {"action": "fill", "selector": "#vendor", "value": "ACME"},
    {"action": "fill", "selector": "#amount", "value": "1250"},
    {"action": "click", "selector": "#submit"},
    {"action": "assert_text", "selector": "#receipt", "value": "Accepted ACME quote: 1250.00 THB"},
    {"action": "text", "selector": "#receipt", "name": "receipt"},
    {"action": "screenshot", "path": "proof/quote.png"},
    {"action": "download", "selector": "#download", "path": "proof/receipt.txt"}
  ]
}
```

The result contains `url`, `title`, `texts` by action name, `screenshots` paths,
`downloads` with path and byte count, and `action_count`. Screenshot captures the
1280 × 720 viewport. Downloads must be triggered by the selected element.

Every HTTP(S) request, including subresources and redirect destinations, must
match an exact configured origin (scheme, host, effective port). Redirects are
inspected before contacting the next origin. Service workers and WebSockets are
disabled. Non-HTTP navigation and requests outside the allowlist fail the tool.
Sites needing other resource origins must explicitly add them. An origin policy
does not pin DNS or IPs or sandbox a compromised browser: restrict the worker's
network independently when processing untrusted websites.

All actions are validated before launch. Limits are 100 actions, 100–30000 ms per
action/navigation, and 16 MiB per saved artifact. Set the workflow step's
`timeout_seconds` to bound the entire run; tool-level limits alone can add up.
Browser subprocesses inherit the runtime process group and are terminated by
its timeout/cancellation cleanup. Downloads may consume temporary browser disk
space before their final size is checked; use container disk quotas as needed.

Artifact paths stay inside the workflow workspace; traversal, symlinks and
nonregular destinations are rejected. Bytes are written atomically using
descriptor-relative POSIX operations. Page content never supplies a filesystem
path. Assertions compare independently specified expected text; clicking a
button alone is not proof of a business outcome.

## Optional Linux desktop worker

Install `xdotool`, `xvfb`, Python Tk (for the disposable fixture), and Pillow with
XCB support. Start an isolated local display and opt in to the plugin:

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
export DISPLAY=:99
python examples/desktop_fixture.py /tmp/desktop-receipt.txt &
```

```json
{"plugins": [{"builtin": "desktop", "config": {}}]}
```

`desktop_tools()` exposes `desktop.run`, which receives an `actions` list:

```json
{
  "actions": [
    {"action": "focus", "title": "Workflow Desktop SIT"},
    {"action": "type", "text": "ACME 1250"},
    {"action": "key", "keys": "Return"},
    {"action": "assert_title", "value": "Saved: ACME 1250"},
    {"action": "assert_pixel", "x": 460, "y": 220, "rgb": [25, 135, 84]},
    {"action": "screenshot", "path": "proof/desktop.png"}
  ]
}
```

`click` also accepts absolute screen coordinates `x` and `y`; `assert_pixel` can
take a `tolerance` from 0 to 255. `type` supports printable ASCII with a 4096
character limit; use named key actions for Tab, Return and key chords. Focus
requires exactly one visible window with the specified exact title. Input stops
if that window loses focus. A per-user display lock prevents this worker's
concurrent tool calls from interleaving input. A call allows up to 100 actions
and each xdotool command has a 10 second subprocess timeout.

This adapter performs real keyboard/mouse events and real screen captures. It
requires an explicitly configured local `DISPLAY`; it never silently starts or
attaches to a user's desktop. It is not a cross-platform RPA engine, OCR service,
or protection against other applications on the same X server. Dedicate one
Xvfb display to a desktop worker. Missing display/dependencies produce
`DesktopUnavailableError`.

## Verification gates

```bash
python -m unittest tests.test_plugins tests.test_rpa tests.test_desktop -v
AGENTIC_DESKTOP_SIT=1 DISPLAY=:99 python -m unittest tests.test_desktop -v
python -m sit.run --output .state/sit-report.json
```

Browser integration submits an actual form, independently checks the rendered
receipt, checks the downloaded receipt, and verifies PNG output. A second test
asserts an off-origin redirect never reaches a separate HTTP server. Desktop
integration launches the real Tk fixture, types and submits a quote, checks the
file written by the application, and asserts screen color/title. Missing browser
or desktop prerequisites are explicit skipped gates, not evidence of success.

Playwright references: [browser contexts and request routing](https://playwright.dev/python/docs/api/class-browsercontext),
[page actions and screenshots](https://playwright.dev/python/docs/api/class-page).
