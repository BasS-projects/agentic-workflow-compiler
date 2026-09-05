# Validation record

Validated on 2026-09-05 in the cloud workspace, Linux with Python 3.12.13.

| Check | Result |
| --- | --- |
| Editable package installation | Passed using available setuptools without external dependencies |
| `python -m unittest discover -s tests -q` | 66 tests passed |
| `python -m compileall -q src` | Passed |
| Document example compile and validation | Passed |
| Document example execution | Completed; 192 UTF-8 bytes written |
| Resume of the completed example | Reused the same saved outputs |
| CLI, input/reference validation, file containment | Covered by passing tests |
| Retry, timeout, process cleanup, run lock, recovery | Covered by passing tests |
| HTTP AI provider and semantic extraction | Covered with mocked HTTP responses; no live model requests |

The CI configuration targets Python 3.11, 3.12, and 3.13 on GitHub-hosted Linux.
This record captures verification before publication; see the
[Actions page](https://github.com/BasS-projects/agentic-workflow-compiler/actions)
for current remote matrix results. A Codespace or hosted service has not been
provisioned.

## Optional provider contract

The generic provider sends `model` and `messages` to an explicitly configured
chat-completions endpoint. The compatible endpoint format is described in the
[llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
This is a protocol reference, not a claim that a particular server or model was
tested in this workspace.

AI tasks forward the stable step idempotency key in the `Idempotency-Key` header.
The destination may ignore this header; the client cannot guarantee remote
deduplication. Semantic extraction has no runtime step identity and omits it.
