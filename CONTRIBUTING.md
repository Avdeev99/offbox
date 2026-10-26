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

Unit CI covers Ubuntu and macOS with Python 3.12 and 3.13. Validation also runs lint, strict type checks, distribution builds, ShellCheck, provider schema validation with mocked Terraform and OpenTofu tests, image builds and one focused native Linux Docker smoke job. Do not add real-cloud tests or require real cloud credentials for tests. Cloud API provisioning and VM bootstrap are not exercised by CI; browser, OOM and performance tests are outside its scope.

Provider checks run without cloud credentials:

```sh
terraform fmt -check -recursive providers
terraform -chdir=providers/hetzner init -backend=false
terraform -chdir=providers/hetzner validate
terraform -chdir=providers/hetzner test
terraform -chdir=providers/gcp init -backend=false
terraform -chdir=providers/gcp validate
terraform -chdir=providers/gcp test
```

Each provider commits a `.terraform.lock.hcl` covering Linux and macOS on amd64/arm64. The file holds entries for both `registry.terraform.io` and `registry.opentofu.org`; `init` drops the other tool's entry, so restore the file before committing. After changing a provider version constraint, refresh both entries with `terraform providers lock` and `tofu providers lock` for `linux_amd64`, `linux_arm64`, `darwin_amd64` and `darwin_arm64`, then combine them. CI checks providers with both Terraform and OpenTofu.

Separately approved [Hetzner](docs/benchmarks/2026-10-23-hetzner.md) and [Google Cloud](docs/benchmarks/2026-10-23-gcp.md) benchmarks exercised provisioning and both sample apps, including browser tests, then verified cloud cleanup. Measurements and comparison limits are recorded in the reports.

The Docker smoke job builds and loads the universal image from the current checkout. It uses disposable SSH keys and containers to exercise real SSH, Mutagen, stream/status/signal handling, forwarding and two isolated Compose stacks. It does not create cloud resources or establish VM bootstrap or browser behavior. Its cleanup uses fixture ownership labels and markers; failure logs are retained as a short-lived Actions artifact. The fixture is intended for a disposable native amd64 Linux runner with a Docker daemon and `/srv/offbox/envs` available. Its commands are in [smoke.yaml](.github/workflows/smoke.yaml); do not run it on an existing Offbox host. Unit tests remain independent of Docker and network access.

Runtime tool checks target OpenSSH >=8.0, Git >=2.30 and Mutagen 0.18.x. Provisioning uses OpenTofu >=1.8 or Terraform >=1.6, with a checksum-verified OpenTofu 1.10.6 fallback for macOS/Linux on amd64/arm64. Unit tests inject tool results and downloads; they do not require these tools or network access. See [the agent guide](docs/agents.md) for diagnostics and exit codes.

Local pre-commit hooks use the locked Ruff installation:

```sh
uvx pre-commit install
uvx pre-commit run --all-files
```

## Pull requests

Read [AGENTS.md](AGENTS.md) for code rules. Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`) and keep each PR focused on one change, targeting `master`. Describe what changed and how you verified it, including the commands you ran and relevant output. Run `make check` and `make test` before pushing. Never commit secrets. Tests never need real credentials or billable cloud resources.

The repository keeps host scripts, image files and provider definitions at the root. Hatch maps them into wheels under `offbox/_data/host`, `offbox/_data/images` and `offbox/_data/providers`, excluding local `.terraform/` directories, state files, editor swap files and provider tests; runtime code must use `importlib.resources` to locate them. The universal image is built separately from the Python wheel.

## Future one-time PyPI setup

The package is not yet published to PyPI, and `v*` release tags are blocked until the maintainer enables publishing. Source installs, wheel builds and master-branch GHCR publication are available. The following setup applies to the first release.

The maintainer must configure a [pending trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) before the first release. A pending publisher does not reserve the project name; the first successful upload creates it.

1. Sign in to the maintainer's PyPI account with two-factor authentication enabled.
2. Open [PyPI publishing settings](https://pypi.org/manage/account/publishing/) and add a pending publisher with these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `offbox` |
   | GitHub owner | `o-avdieiev` |
   | Repository | `offbox` |
   | Workflow filename | `release.yaml` |
   | Environment name | `pypi` |

3. The `pypi` environment is restricted to `v*` tags. Review its approval policy and explicitly update the release-tag ruleset that currently blocks those tags before the first release.
4. Ensure GitHub Actions is enabled. Only the artifact-consuming publish job requests `id-token: write` for OIDC and `contents: write` for the GitHub release; build/test jobs use read-only permissions. No PyPI API token or password is needed.

For later releases, manage the publisher under the existing PyPI project's Publishing settings. See [PyPI's trusted-publisher documentation](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## Release

1. Set `__version__` in `src/offbox/__init__.py` and add the matching dated section in `CHANGELOG.md`.
2. Run `uv sync --frozen`, `make check`, `make test` and `uv build`. Install the wheel in an isolated environment and check both version forms.
3. Merge the release PR after CI succeeds. For the first release, confirm that the PyPI setup above is complete.
4. Once publishing is enabled, create and push a tag matching the package version. This starts `.github/workflows/release.yaml`.
5. The workflow checks the tag against the package version, extracts that version's changelog section, builds and smoke-tests the wheel, then runs `uv publish --trusted-publishing always`. It creates the GitHub release using the same changelog section and attaches the distributions.
6. Verify `uvx --refresh offbox --version` prints the released version and inspect the release on PyPI and GitHub.

Release publication requires the maintainer's PyPI account setup; building and testing locally does not publish anything. See the GHCR setup below before the first universal-image publication.


## Universal image and one-time GHCR setup

The image workflow builds `linux/amd64` from `images/universal/Dockerfile`. Pull requests build without pushing. Changes to the image, its workflow or the package image constant on `master` publish the compatibility tag from `DEFAULT_IMAGE` and `sha-<short commit>`. Release tags also publish the corresponding semantic version. The workflow reads the repository name and compatibility tag from the package constant, so an image default changes in one place.

After the first successful publication, open the repository's linked `offbox-universal` package on GitHub, choose **Package settings → Change visibility → Public**, and confirm the change. This is a one-time maintainer action; hosts need anonymous pull access. Ensure the package is linked to this repository and its Actions access allows this repository to write. The workflow uses only `GITHUB_TOKEN` with `packages: write`; no cloud credential or registry password is needed. See [GitHub's package visibility instructions](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

Image compatibility series `1` is distinct from the CLI release number. A future CLI `v0.1.0` release publishes image aliases `:0.1.0` and `:1.0.0` in addition to the compatibility and commit tags. A weekly scheduled run on `master` rebuilds and republishes the compatibility and commit tags so the image picks up Ubuntu security updates.

Each successful build writes the actual compressed OCI image size to its Actions summary and an `image-size` artifact. Include the measured result in PRs that change the image; the target is below 3 GB. The report sums the compressed layer and configuration descriptors for the amd64 image and excludes attestations. Build/static checks do not prove nested Docker, browser or signal behavior at runtime. See [the image build guide](docs/universal-image.md) for contents and process contracts.
