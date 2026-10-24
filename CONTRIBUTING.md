# Contributing

Open a pull request from a fork for changes; no cloud credentials are needed. Fork CI requires maintainer approval before it runs. See [repository safeguards](docs/repository-safety.md) and [private security reporting](SECURITY.md).

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

Unit CI covers Ubuntu and macOS with Python 3.12 and 3.13. Validation also runs lint, strict type checks, distribution builds, image builds and one focused native Linux Docker smoke job. Do not add real-cloud tests or require real cloud credentials for tests.

The Docker smoke job builds and loads the universal image from the current checkout. It uses disposable SSH keys and containers to exercise real SSH, Mutagen, stream/status/signal handling, forwarding and two isolated Compose stacks. It does not create cloud resources or establish VM bootstrap or browser behavior. Its cleanup uses fixture ownership labels and markers; failure logs are retained as a short-lived Actions artifact. The fixture is intended for a disposable native amd64 Linux runner with a Docker daemon and `/srv/offbox/envs` available. Its commands are in [smoke.yaml](.github/workflows/smoke.yaml); do not run it on an existing Offbox host. Unit tests remain independent of Docker and network access.

Runtime tool checks target OpenSSH >=8.0, Git >=2.30 and Mutagen 0.18.x. Provisioning uses OpenTofu >=1.8 or Terraform >=1.6, with a checksum-verified OpenTofu 1.10.6 fallback for macOS/Linux on amd64/arm64. Unit tests inject tool results and downloads; they do not require these tools or network access. See [the agent guide](docs/agents.md) for diagnostics and exit codes.

Local pre-commit hooks use the locked Ruff installation:

```sh
uvx pre-commit install
uvx pre-commit run --all-files
```

Read [AGENTS.md](AGENTS.md) for code rules. Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), one PR per milestone, targeting `master`. Include the milestone's acceptance criteria as a checked list with command-output evidence in each PR. Run `make check` and `make test` before pushing. Never commit secrets. Tests never need real credentials or billable cloud resources.

The repository keeps host scripts, image files and provider definitions at the root. Hatch force-includes them in wheels under `offbox/_data/host`, `offbox/_data/images` and `offbox/_data/providers`; runtime code must use `importlib.resources` to locate them. The universal image is built separately from the Python wheel.

## Future one-time PyPI setup

PyPI publishing is deferred by the maintainer. Do not create `v*` release tags until publishing is explicitly resumed. Source installs, wheel builds and master-branch GHCR publication remain available. The following setup is retained for that future release.

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

3. The `pypi` environment is restricted to `v*` tags. Review its approval policy and explicitly update the release-tag ruleset that currently blocks those tags before resuming publication.
4. Ensure GitHub Actions is enabled. Only the artifact-consuming publish job requests `id-token: write` for OIDC and `contents: write` for the GitHub release; build/test jobs use read-only permissions. No PyPI API token or password is needed.

For later releases, manage the publisher under the existing PyPI project's Publishing settings. See [PyPI's trusted-publisher documentation](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## Release

1. Set `__version__` in `src/offbox/__init__.py` and add the matching dated section in `CHANGELOG.md`.
2. Run `uv sync --frozen`, `make check`, `make test` and `uv build`. Install the wheel in an isolated environment and check both version forms.
3. Merge the milestone PR after CI succeeds. For the first release, confirm that the PyPI setup above is complete.
4. Once publishing is resumed, create and push a tag matching the package version. This starts `.github/workflows/release.yaml`.
5. The workflow checks the tag against the package version, extracts that version's changelog section, builds and smoke-tests the wheel, then runs `uv publish --trusted-publishing always`. It creates the GitHub release using the same changelog section and attaches the distributions.
6. Verify `uvx --refresh offbox --version` prints the released version and inspect the release on PyPI and GitHub.

Release publication requires the maintainer's PyPI account setup; building and testing locally does not publish anything. See the GHCR setup below before the first universal-image publication.


## Universal image and one-time GHCR setup

The image workflow builds `linux/amd64` from `images/universal/Dockerfile`. Pull requests build without pushing. Changes to the image, its workflow or the package image constant on `master` publish the compatibility tag from `DEFAULT_IMAGE` and `sha-<short commit>`. Release tags also publish the corresponding semantic version. The workflow reads the repository name and compatibility tag from the package constant, so an image default changes in one place.

After the first successful publication, open the repository's linked `offbox-universal` package on GitHub, choose **Package settings → Change visibility → Public**, and confirm the change. This is a one-time maintainer action; hosts need anonymous pull access. Ensure the package is linked to this repository and its Actions access allows this repository to write. The workflow uses only `GITHUB_TOKEN` with `packages: write`; no cloud credential or registry password is needed. See [GitHub's package visibility instructions](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

Image compatibility series `1` is distinct from the CLI release number. A future CLI `v0.1.0` release publishes image aliases `:0.1.0` and `:1.0.0` in addition to the compatibility and commit tags, as recorded in the implementation issue's version clarification. Those release-tag aliases are deferred along with PyPI publication; the public compatibility image is already available.

Each successful build writes the actual compressed OCI image size to its Actions summary and an `image-size` artifact. Copy that measured result into the milestone PR; the target is below 3 GB. The report sums the compressed layer and configuration descriptors for the amd64 image and excludes attestations. Build/static checks do not prove nested Docker, browser or signal behavior at runtime. See [the image build guide](docs/universal-image.md) for contents and process contracts.
