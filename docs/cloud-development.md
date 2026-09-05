# Cloud development and deployment

The repository includes `.devcontainer/devcontainer.json` for GitHub Codespaces
and compatible Python3.12 dev containers. Open the
[repository](https://github.com/BasS-projects/agentic-workflow-compiler), create a
Codespace from the Code menu, then use the README quickstart. Install optional
backend dependencies with `python -m pip install -e '.[langgraph]'`.

For an always-on coordinator and remote worker, use the supplied Docker Compose
stack on a host you control. [Operations](operations.md) describes generated
credentials, a private console, durable volumes, health checks, smoke testing and
backup/restore. A Codespace is a development environment and may stop when idle;
it is not the production availability model.

[GitHub Actions](https://github.com/BasS-projects/agentic-workflow-compiler/actions)
runs Python3.11–3.13 tests, independent SIT with installed Chromium/X11, and an
actual Compose API/worker smoke test. Test evidence is uploaded as a CI artifact.
The live LLM gate requires separately configured endpoint/model credentials.

The coordinator database belongs to one server. Workers keep independent durable
runtime state and never mount the central database. Keep volumes and credentials
private, back up SQLite consistently, and restore-test on the selected platform.
Deploying beyond loopback additionally requires target-specific TLS, access
routing, secret management and backup destinations.

```bash
git clone https://github.com/BasS-projects/agentic-workflow-compiler.git
cd agentic-workflow-compiler
python deploy/init.py
docker compose up --build -d --wait
python deploy/smoke.py --docker
```

These commands prepare and run a concrete service on the selected host. They do
not allocate a cloud account or publish a public endpoint automatically. Current
verification and external gates are recorded in [validation](validation.md).

Configuration references:
[Python devcontainer](https://mcr.microsoft.com/en-us/artifact/mar/devcontainers/python/tag/3.12-bookworm),
[checkout](https://github.com/actions/checkout),
[setup-python](https://github.com/actions/setup-python).
