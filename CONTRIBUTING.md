# Contributing to CloudDeck

Thanks for helping. CloudDeck is alpha software that moves real money, so the bar for changes is "boring, tested and safe". Small,
focused pull requests are easiest to review. Please read the [Code of Conduct](CODE_OF_CONDUCT.md); report security problems privately
(see [SECURITY.md](SECURITY.md)), not as issues.

By contributing you agree that your contribution is licensed under the [MIT license](LICENSE). Much of this code base was written with
AI assistance and contributions may be too; you are responsible for understanding, testing and standing behind what you submit.

## The one rule

> **Tests never spend real money and never touch real state.**
> No Akash API key, no real lease, no real `secrets.env`, `state.json` or keys directory, no network call to the Akash API (not even a
> read-only one) from a test.

Tests use the simulated provider (`DemoProvider`, `clouddeck -p demo`) and the in-memory Akash and lease fake in `tests/fake_akash.py`
(the `fake` fixture in `tests/conftest.py`). External responses are saved fixtures. Use neutral names in tests, fixtures and docs
(`prod-box`, `web-app`, `my-project`, `provider-a.example`), and never real deployment numbers, account addresses, host names or paths.

## Development setup

You need Python 3.11+ and git. For the lease-script tests you also want bash, GnuPG, `flock` and `tar` (Linux or WSL; they are
skipped with a reason elsewhere).

```bash
git clone https://github.com/trexchery/clouddeck.git && cd clouddeck
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pip install ruff==0.17.1                               # the version CI uses
```

Optional: `pip install -e ".[pty-tests]"` for the terminal smoke test (`python tests/pty_smoke.py`), `pip install shellcheck-py` for
the lease scripts, `pip install pre-commit && pre-commit install` for the same checks as CI before every commit.

## Running the tests

```bash
python -m pytest -p no:cacheprovider
```

The suite is offline and takes a few minutes. Run plain `pytest` or a single file; mixing `tests/...` paths with the root `test_ddc.py`
in an unusual order can lose the per-test isolation fixture (there are two `conftest.py` files). The TUI tests are timing-sensitive on a
heavily loaded machine: rerun a lone failure before investigating.

The `ddc job` tests (`tests/test_job_*.py`) run the real runner on virtual time against a stubbed Akash API and a fake box
(`tests/job_fakes.py`, `tests/job_helpers.py`; the `jw` fixture) inside the same isolated state as every other test. `tests/job_sim.py` is the
old-vs-new simulation with assumed, synthetic inputs (from a checkout with the dev extra, `python tests/job_sim.py` runs it standalone in a throw-away state dir). `tests/test_job_scripts.py` runs the real
`jobremote/*.sh` scripts under bash and skips cleanly where bash (or curl, or shellcheck) is missing.

Opt-in extras:

* `CLOUDDECK_TEST_WHEEL=1 python -m pytest tests/test_packaging.py` builds a wheel in a temp copy and runs it in a clean venv (needs
  network for the build backend).
* `python tests/pty_smoke.py` drives the real TUI through a pseudo-terminal.

### Redirect the environment first (and why)

The controller reads its config, state and secrets locations from the environment **when it is imported**. A test run once resolved the
real directories and overwrote a live `state.json`: the file that records which leases are billing. Two protections exist now:

1. the root `conftest.py` points `DDC_STATE_DIR`, `DDC_SECRETS_DIR` and `DDC_CONFIG` at fresh temp directories before anything is
   imported, and the controller **refuses to load under pytest** unless `DDC_STATE_DIR` and `DDC_SECRETS_DIR` are both set;
2. *you*, every time you run pytest, a script that imports `clouddeck`, or the CLI during development, should also redirect the
   environment, so that a mistake cannot reach your real directories.

```bash
# Linux / macOS / WSL
S=$(mktemp -d)
export HOME=$S/home XDG_CONFIG_HOME=$S/xdg/config XDG_STATE_HOME=$S/xdg/state XDG_DATA_HOME=$S/xdg/data TMPDIR=$S/tmp
export DDC_STATE_DIR=$S/state DDC_SECRETS_DIR=$S/secrets PYTHONDONTWRITEBYTECODE=1
mkdir -p "$HOME" "$TMPDIR"
python -m pytest -p no:cacheprovider
```

```powershell
# Windows PowerShell
$S = Join-Path $env:TEMP ("clouddeck-" + [guid]::NewGuid()); New-Item -ItemType Directory $S\home, $S\local, $S\roaming, $S\tmp | Out-Null
$env:USERPROFILE = "$S\home"; $env:HOME = "$S\home"; $env:LOCALAPPDATA = "$S\local"; $env:APPDATA = "$S\roaming"
$env:TEMP = "$S\tmp"; $env:TMP = "$S\tmp"
$env:DDC_STATE_DIR = "$S\state"; $env:DDC_SECRETS_DIR = "$S\secrets"; $env:PYTHONDONTWRITEBYTECODE = "1"
python -m pytest -p no:cacheprovider
```

Do the same in a throwaway shell when you try `ddc` or `clouddeck` by hand, unless you really mean to use your real setup.

## Adding or changing things

* **Every behaviour change gets an offline test**, and a bug fix gets a test that fails without the fix. Spending, ownership,
  checkpoint and watchdog paths are the most important to cover: look at `tests/test_safety_*.py` for the pattern.
* **Keep the compatibility promises** ([ARCHITECTURE.md](docs/ARCHITECTURE.md#compatibility-promises)): the `ddc-` lease prefix and `ours`
  semantics, the `state.json` / `ledger.jsonl` / `custom_profiles.json` formats, the checkpoint layout, the lease-side paths, and the
  `DDC_*`, `AKASH_API_KEY` and `CLOUDDECK_*` names. If a change must break one, say so in the PR and in the changelog.
* **Fail closed.** When something is unknown (an empty API answer, a damaged file, an unreadable balance), do not assume the safe-looking
  answer; refuse to spend and keep close/stop paths working.
* **No new runtime dependency** without a discussion: the controller and the lease scripts are standard-library only on purpose.
* **Lease scripts** (`lease/`): every file in that directory is uploaded to every lease, so no subdirectories; LF line endings; bash
  (`set -euo pipefail`); `shellcheck -x -S warning -P lease lease/*.sh` must be clean; document new environment variables in
  `lease/README.md`. Most tests for them need bash, `gpg`, `flock` and `python3` (Linux/WSL).
* **Job scripts** (`jobremote/`): the same rules as lease scripts (LF, executable bit in git: `git update-index --chmod=+x`, shellcheck clean);
  `boot.sh` is inlined into the SDL, `step.sh` / `fetch.sh` are uploaded. Anything that creates or closes a lease must go through
  `launch_lease` / `close_box`, never a direct API call (a test greps for it).
* **A new provider**: see [Extension points](docs/ARCHITECTURE.md#extension-points). It must route spending through `Provider.require`.
* **Docs**: update the relevant file under `docs/`, the README command table and `config.example.toml` when behaviour or configuration
  changes. Do not document a flag you have not run `--help` on.

## Style

* Python: `ruff check .` must pass (configuration in `pyproject.toml`: rules E, F, W, I, B, UP, target 3.11, long lines are allowed).
  `ruff format` is enforced only for `scripts/` and `tests/test_release_check.py`; elsewhere match the surrounding code (the code base
  is compact), and do not reformat files you are not changing.
* Prefer the shortest change that works; no abstractions for a single caller, no speculative configuration.
* Error messages say what happened and what to do next. Never print secrets, tokens or key material, not even in debug output.

## Release hygiene checks

```bash
python scripts/release_check.py --no-history   # working tree: tracked and untracked-not-ignored files
python scripts/release_check.py                # plus every commit, every blob, every commit message
python scripts/release_check.py --strict       # also fail on placeholders and commit identities (before publishing)
```

It fails on real-looking secrets, API keys, account addresses, personal absolute paths, private Claude session links, forbidden file
names and missing required files. It prints locations and rule names only, never the matched text. With `--strict` it also fails on an
`OWNER` placeholder left in this project's own GitHub URL and on unfilled release-manager markers (the README's live-verification
table); placeholders meant for the user, such as `OWNER/your-private-state-repo`, are fine. Private words (names, private project
names, deployment ids) are never written into the public script: maintainers keep them in `scripts/release_denylist.local.txt`
(gitignored; one case-insensitive regex per line, `#` comments) or point `CLOUDDECK_RELEASE_DENYLIST` at such a file. The few legacy
compatibility strings that are allowed live in `scripts/release_allowlist.toml`, and every entry needs a `reason`: keep it small. The
allowlist is scanned too, except inside its own `contains` values.

Secret scanning uses [gitleaks](https://github.com/gitleaks/gitleaks) (CI pins version 8.30.1):

```bash
gitleaks dir --config .gitleaks.toml --redact --no-banner .
gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts="--all" .
```

`pre-commit` runs gitleaks, ruff, shellcheck, `detect-private-key` and the tree-only release check.

Building and checking the artifacts (CI does this too):

```bash
pip install build twine && python -m build && python -m twine check --strict dist/*
python scripts/check_dist.py dist/*.whl dist/*.tar.gz
```

Remove `build/`, `dist/` and `*.egg-info/` afterwards; they are ignored by git but should not linger.

With `SOURCE_DATE_EPOCH` set to the commit time (`git log -1 --format=%ct`), the wheel is bit-for-bit reproducible. The sdist is
content-reproducible only: the same members, order and contents, but tar and gzip timestamps differ between builds. Release artifacts
are built on Linux (`.github/workflows/release.yml`) so the sdist keeps the executable bits of the shell scripts.

## Running the CI gates locally

```bash
python scripts/ci_local.py              # ruff, pytest (redirected env, -n auto with pytest-xdist), release_check --no-history,
                                        # build + twine check + check_dist, shellcheck if installed
python scripts/ci_local.py --release    # the same with release_check --strict over tree + history, as the release workflow runs it
python scripts/ci_local.py --no-build   # skip the build
```

It needs `pip install -e ".[dev]" ruff==0.17.1 build twine` (and optionally `pytest-xdist`, `shellcheck-py`). It does not run the
Windows/Linux matrix, the clean-venv wheel smoke test, actionlint, gitleaks or pip-audit; CI does.

## Releasing

Push a `vX.Y.Z` tag on a commit that is green on `main`, after moving the `[Unreleased]` entries of `CHANGELOG.md` into a dated `[X.Y.Z]`
section, setting the version in `pyproject.toml` and `clouddeck/__init__.py` and filling in the README's live-verification table.
`.github/workflows/release.yml` checks that the tag, the version and the changelog agree, runs `release_check.py --strict`, builds and
checks the artifacts, publishes to PyPI through Trusted Publishing when the repository variable `PYPI_PUBLISH` is `true`, and creates the
GitHub release with the wheel, the sdist, `SHA256SUMS` and that changelog section as notes.

## Pull request checklist

- [ ] Offline tests added or updated, and `python -m pytest -p no:cacheprovider` passes (with the redirected environment above)
- [ ] `ruff check .` is clean (and `shellcheck` for `lease/` changes)
- [ ] `python scripts/release_check.py --no-history` passes: no secrets, personal paths, account names or real host names
- [ ] No test, fixture or doc depends on real money, real credentials or real state
- [ ] Compatibility kept, or the break is called out
- [ ] Docs, `config.example.toml` and `CHANGELOG.md` updated if behaviour or configuration changed
- [ ] The change is as small as it can be
