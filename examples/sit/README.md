# Reproducible SIT examples

These workflows use only built-in tools and IR 0.2 controls. Run them from the
repository checkout with the CLI documented in the main README. They are also
business fixtures for the independently asserted scenarios in `sit/`.

- `document-release.ir.json`: prepare an invoice, pause for finance approval,
  then release it into the runtime workspace. Denial must prevent publication.
- `document-batch.ir.json` with `document-batch.inputs.json`: normalize exactly
  three invoice documents in their original order. A fourth input exceeds the
  declared bound and must fail.
- `vendor-parallel.ir.json`: gather named vendor values through bounded parallel
  branches. The SIT runner replaces these deterministic values with explicit
  timing tools to prove actual overlap and the concurrency bound.

Run the full independent proof suite:

```bash
python -m sit.run --output .state/sit-report.json
```

The browser form is an executable fixture rather than a static HTML screenshot:

```bash
python -m sit.fixture_server --port 8765 --state .state/vendor-fixture
```

Open `http://127.0.0.1:8765/vendor-quote` and submit a quote. A real HTTP POST
creates `.state/vendor-fixture/receipt.txt`; the download link returns that file.
`browser_vendor_quote` starts its own server, performs the interaction in Chromium,
and independently verifies rendered receipt, server file, download, and PNG.

See `docs/sit-plan.md` for expected outcomes and explicit external gate rules.
