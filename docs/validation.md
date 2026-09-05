# Validation record — 0.2.0

Executed on 2026-09-05 in the development workspace, Linux/Python3.12.13.
Source and dependency fingerprints are preserved in the
[local SIT report](evidence/sit-local.json); `git_dirty:true` records that source
was tested before publication. Its exact source hash identifies the implementation
independently of the previous Git commit shown by the working checkout.

| Check | Actual result |
| --- | --- |
| Package installation | Passed; version0.2.0, cloudpickle3.1.2, LangGraph1.2.11, Playwright1.62.0 |
| Combined regression suite | 143 tests:141 passed,2 explicitly skipped;0 failures |
| Independent SIT | 13 scenarios:10 passed,3 explicitly skipped;0 failures |
| Compilation/CLI/backend artifact | Real generated LangGraph artifact executed; review blocked effects and tamper rejected |
| Advanced runtime | Actual spawned parallel overlap, bounded concurrency, approval restart/denial/cancel, orphan recovery and shared engine identity exclusion |
| Distributed API/workers | Real HTTP and worker processes; exclusive claims, lease fencing, authenticated actors, cancellation and scheduling |
| Abrupt worker loss | Actual supervisor SIGKILL after accepted HTTP delivery, uncertain recovery, new worker takeover, same idempotency key; fixture ledger delivered once across2 attempts |
| Recovery edge cases | Expired lease cannot renew after SQL lock wait; approval immutable; missing reviewed checkpoint requires new run and fresh review |
| Spawned AI transport | Authenticated local HTTP fixture executed through advanced and LangGraph runtimes; no external model used |
| Deployment smoke locally | Actual API and remote worker:9 checks passed; container-specific file check not run locally |
| Python syntax and documentation links | Passed |

Reproduce:

```bash
python -m pip install -e '.[all]'
python -m unittest discover -s tests -v
python -m sit.run --output .state/sit-report.json
```

Evidence: [unit output](evidence/unit-local.txt), [SIT JSON](evidence/sit-local.json),
[JUnit XML](evidence/sit-local.xml), [scenario plan and oracles](sit-plan.md).
Artifact paths in the checked-in local report use `${REPOSITORY}` and refer to the
original local evidence directory; the GitHub CI evidence artifact contains its
own actual files and screenshots.

## Remote CI gates

[Verified GitHub run 33988780057](https://github.com/BasS-projects/agentic-workflow-compiler/actions/runs/33988780057)
completed successfully on 2026-09-05 for source commit
`4e492d8a39ad8ef776cf61f5695fc7be1fbd1d36`. All five jobs passed:

| Executed on GitHub | Observed result |
| --- | --- |
| Python3.11,3.12,3.13 regression jobs | All passed;143 tests per matrix job,2 optional RPA skips covered in the dedicated job |
| Independent SIT with real Chromium and X11 | 12 passed,0 failed,1 skipped: unconfigured live semantic provider |
| Browser/desktop tool policy suite | 8 tests passed with actual browser and disposable desktop |
| Chromium operations console | 11 checks passed: submit/approve/complete, result, audit, token clearing, inert hostile text and mobile overflow |
| Docker Compose deployment | 10 checks passed, including the actual worker output file's SHA256 |

The [sit-evidence artifact](https://github.com/BasS-projects/agentic-workflow-compiler/actions/runs/33988780057/artifacts/9975987194)
contains JSON/JUnit, workflow checkpoints, receipts and screenshots. Artifact
retention ends 2026-12-04 under the repository's current retention policy.
[Machine-readable CI verification](evidence/ci-verified.json) records the run,
job IDs, source commit and artifact digest.

[GitHub Actions](https://github.com/BasS-projects/agentic-workflow-compiler/actions)
is configured to run the Python3.11–3.13 matrix, SIT with **required** real Chromium
and X11 desktop scenarios, real RPA policy tests, Chromium console interaction,
and Docker Compose API/worker smoke including the written file's actual hash.
Results for the published commit must be read from Actions; configuration alone
is not a passing result. Reports/screenshots are uploaded in `sit-evidence`.

## Local skips and external acceptance

- **Browser:** Playwright was installed, but Chromium download was unavailable
  under the workspace network restrictions. Real browser execution was skipped
  locally; the dedicated console gate also correctly fails without a binary.
- **Desktop:** Local Xvfb/xdotool were initially unavailable. Extracted binaries
  still could not start Xvfb because the host denies Unix sockets. No desktop
  interaction is claimed from this workspace; GitHub's disposable X11 gate is
  required instead.
- **Live model:** No endpoint/model/key was configured. Recorded semantic cases
  and local HTTP transport prove interfaces, not language model quality. Run the
  live gate and full semantic evaluation corpus against the selected endpoint.
- **Production cloud:** Docker is absent in the local workspace. CI's actual
  containers prove the packaged deployment on a GitHub runner, not a public
  production endpoint. Cloud target, TLS, account credentials and customer RPA
  applications remain environment-specific acceptance work.

These limits do not turn skipped cases into passes. See the [roadmap](roadmap.md)
for the distinction between implemented phase scope and external rollout gates.
