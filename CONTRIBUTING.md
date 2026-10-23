# Contributing

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 or newer, then run:

```sh
uv sync --frozen
make check
make test
```

`uv.lock` is committed. Change dependencies with uv and include the updated lockfile.

| Command | Purpose |
| --- | --- |
| `make fmt` | Format Python and apply Ruff lint fixes |
| `make check` | Check formatting, lint and strict mypy/Pyright types |
| `make test` | Deterministic unit tests; excludes the subprocess marker |
| `uv build` | Build the source distribution and wheel |

Unit CI covers Ubuntu and macOS with Python 3.12 and 3.13. Validation uses unit tests, lint, strict type checks and distribution builds. Do not add paid cloud or end-to-end tests or require real credentials for tests.

Local pre-commit hooks use the locked Ruff installation:

```sh
uvx pre-commit install
uvx pre-commit run --all-files
```

Read [AGENTS.md](AGENTS.md) for code rules. Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), one PR per milestone, targeting `master`. Include the milestone's acceptance criteria as a checked list with command-output evidence in each PR. Run `make check` and `make test` before pushing. Never commit secrets. Tests never need real credentials or billable cloud resources.

The repository keeps host scripts, image files and provider definitions at the root. Hatch force-includes them in wheels under `offbox/_data/host`, `offbox/_data/images` and `offbox/_data/providers`; runtime code must use `importlib.resources` to locate them. Their product implementations arrive in later milestones.

## One-time PyPI setup before v0.0.1

The maintainer must configure a [pending trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) before the first release. A pending publisher does not reserve the project name; the first successful upload creates it.

1. Sign in to the maintainer's PyPI account with two-factor authentication enabled.
2. Open [PyPI publishing settings](https://pypi.org/manage/account/publishing/) and add a pending publisher with these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `offbox` |
   | GitHub owner | `Avdeev99` |
   | Repository | `offbox` |
   | Workflow filename | `release.yaml` |
   | Environment name | `pypi` |

3. In the repository's GitHub Settings → Environments, create `pypi`. Restrict deployments to release tags matching `v*`; optionally add required reviewers.
4. Ensure GitHub Actions is enabled. The release workflow requests `id-token: write` for OIDC and `contents: write` for the GitHub release. No PyPI API token or password is needed.

For later releases, manage the publisher under the existing PyPI project's Publishing settings. See [PyPI's trusted-publisher documentation](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## Release

1. Set `__version__` in `src/offbox/__init__.py` and add the matching dated section in `CHANGELOG.md`.
2. Run `uv sync --frozen`, `make check`, `make test` and `uv build`. Install the wheel in an isolated environment and check both version forms.
3. Merge the milestone PR after CI succeeds. For the first release, confirm that the PyPI setup above is complete.
4. Create and push a matching tag, starting with `v0.0.1`. This starts `.github/workflows/release.yaml`.
5. The workflow checks the tag against the package version, extracts that version's changelog section, builds and smoke-tests the wheel, then runs `uv publish --trusted-publishing always`. It creates the GitHub release using the same changelog section and attaches the distributions.
6. Verify `uvx --refresh offbox --version` prints the released version and inspect the release on PyPI and GitHub.

Release publication requires the maintainer's PyPI account setup; building and testing locally does not publish anything. The universal-image workflow and one-time public GHCR setup arrive in milestone 3.
