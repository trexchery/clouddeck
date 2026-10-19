## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated; `python -m pytest` passes (tests are offline: temp dirs, simulated provider, no real Akash calls or credentials)
- [ ] `ruff check .` is clean
- [ ] No secrets, personal paths, account names or real hostnames in the diff, tests, fixtures or docs (`python scripts/release_check.py` passes)
- [ ] Compatibility kept: `ddc-` lease prefix, `state.json` and checkpoint formats, lease-side paths, `DDC_*` / `CLOUDDECK_*` environment names (or the change is called out above)
- [ ] Docs updated if behaviour or configuration changed
