# Third-party notices

CloudDeck itself is released under the [MIT License](LICENSE). It does not copy or vendor third-party source code. The software below is
installed separately (by `pip`, or on the rented box at runtime) and keeps its own license. This list was produced from the metadata
of a fresh virtual environment (`pip install clouddeck`, Python 3.12, 2026-10-17) and checked against `pip-audit` (no known
vulnerabilities at that time). Versions are the ones resolved then; the declared ranges are in `pyproject.toml`.

## Runtime dependencies (installed with CloudDeck)

| Package | Version | License | Project |
|---|---|---|---|
| textual | 8.2.8 | MIT | <https://github.com/Textualize/textual> |
| rich | 15.0.0 | MIT | <https://github.com/Textualize/rich> |
| tomlkit | 0.15.2 | MIT | <https://github.com/python-poetry/tomlkit> |
| markdown-it-py | 4.2.0 | MIT | <https://github.com/executablebooks/markdown-it-py> |
| mdit-py-plugins | 0.6.1 | MIT | <https://github.com/executablebooks/mdit-py-plugins> |
| mdurl | 0.1.2 | MIT | <https://github.com/executablebooks/mdurl> |
| linkify-it-py | 2.2.0 | MIT | <https://github.com/tsutsu3/linkify-it-py> |
| platformdirs | 4.13.4 | MIT | <https://github.com/tox-dev/platformdirs> |
| Pygments | 2.21.0 | BSD-2-Clause | <https://pygments.org> |
| typing_extensions | 4.16.0 | PSF-2.0 | <https://github.com/python/typing_extensions> |

All of these are permissive licenses that are compatible with MIT. Everything else CloudDeck imports is the Python standard library.

## Development and test only (not installed for users, not distributed)

| Package | License | Used for |
|---|---|---|
| pytest (with pluggy, iniconfig, packaging, colorama) | MIT; MIT; MIT; Apache-2.0 OR BSD-2-Clause; BSD-3-Clause | the test suite (`.[dev]`) |
| pywinpty | MIT | terminal smoke test on Windows (`.[pty-tests]`) |
| pyte (with wcwidth) | **LGPL-3.0**; MIT | terminal smoke test (`.[pty-tests]`). Optional test-only extra: it is not vendored, bundled or required at runtime and must stay that way |

Tools run in CI or by contributors, never shipped: ruff (MIT), gitleaks (MIT), shellcheck (GPL-3.0-or-later; run as a separate program),
actionlint (MIT), pip-audit, twine and coverage (Apache-2.0), build and pre-commit (MIT), and the GitHub Actions `checkout`,
`setup-python` and `dependency-review-action` (MIT).

## Software on the rented box (not distributed by this project)

CloudDeck never ships or redistributes these. The controller orders a container image and the order's boot script installs packages
from the image's package mirrors when the box starts; they run on the provider's machine, not on yours.

| Software | License (summary) | Where it comes from |
|---|---|---|
| Container image, default `node:22-bookworm` (configurable with `image`) | Node.js: MIT; Debian packages: each package's own license; Docker Hub terms and pull limits apply | Docker Hub |
| openssh-server | BSD | `apt` in the SDL boot script |
| tmux | ISC | `apt` |
| rsync | GPL-3.0-or-later | `apt` |
| GNU parallel | GPL-3.0-or-later (it prints a request to cite it when used for research: `parallel --citation`) | `apt` |
| htop | GPL-2.0-or-later | `apt` |
| procps, time, bc, less | GPL / LGPL family | `apt` |
| pigz | zlib | `apt` |
| jq | MIT | `apt` |
| python3-venv | PSF-2.0 | `apt` |
| vim-tiny | Vim license | `apt` |
| ca-certificates | MPL-2.0 (Mozilla bundle) and others | `apt` |
| git, gnupg, tar, bash, coreutils | GPL family | already in the image |
| **Claude Code** (`@anthropic-ai/claude-code`), optional | **proprietary**, Anthropic | installed from the public npm registry at an exact pinned version when `install_claude_code = true` (default); set it to `false` to skip. CloudDeck does not redistribute it. You must accept Anthropic's terms yourself |

Each Debian package ships its license text under `/usr/share/doc/<package>/copyright` on the box.

## Services and trademarks

* CloudDeck is **not affiliated with, endorsed by, or sponsored by** Akash Network, Anthropic, GitHub or any provider on the Akash
  network. Use of the Akash Console API and of Akash providers is governed by Akash's and the providers' own terms, and you are
  responsible for complying with them.
* "Claude" and "Claude Code" are trademarks of Anthropic, PBC. "Akash" and "Akash Network" belong to their owners. GitHub, Docker,
  Node.js, Debian, Ubuntu, Windows and macOS are trademarks of their respective owners. Names are used only to say what CloudDeck works
  with.
