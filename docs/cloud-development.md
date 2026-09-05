# Cloud development

This MVP is a Python CLI/runtime that can execute in a cloud development
workspace. It does not provision an always-on API, hosted UI, cloud account,
or distributed workflow service.

The repository includes `.devcontainer/devcontainer.json` for GitHub Codespaces
and compatible dev containers. It installs the package with `pip install -e .`.
Open the [repository](https://github.com/BasS-projects/agentic-workflow-compiler),
then use its Code menu to create a Codespace. Use the README quickstart inside
its terminal.

The `.github/workflows/ci.yml` workflow is ready to install the package, test it
on Python 3.11–3.13, and run the document pipeline on GitHub-hosted Linux runners.
See the [Actions page](https://github.com/BasS-projects/agentic-workflow-compiler/actions)
for actual GitHub CI results.

Keep `.state/` and workflow input/output files within the workspace's durable
volume when you need later resumes. SQLite and run locks assume one local host
and local filesystem. Do not share one runtime database across cloud workers.

Cloud development configuration sources:

- [Python dev container image](https://mcr.microsoft.com/en-us/artifact/mar/devcontainers/python/tag/3.12-bookworm)
- [actions/checkout](https://github.com/actions/checkout)
- [actions/setup-python](https://github.com/actions/setup-python)

## Clone the project

```bash
git clone https://github.com/BasS-projects/agentic-workflow-compiler.git
cd agentic-workflow-compiler
```

Use the machine's normal GitHub authentication. Do not place tokens in source
files or paste tokens into chat. Existing repositories with commits require
normal fetch/merge handling; do not force-push over them.
