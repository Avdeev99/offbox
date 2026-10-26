# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Google Cloud provisioning with a dedicated per-host network, Ubuntu cloud-init, no attached VM service account, standard Google authentication and mocked Terraform contract tests.
- A Google Cloud benchmark of both sample apps and a same-VM comparison measuring a 12.2% median command latency reduction from batching checks, with verified cloud cleanup.
- Opt-in `--timings` summaries for provisioning, synchronization, setup, forwarding and command execution, without changing stdout or exit codes.
- A real Hetzner benchmark of both sample applications, including timings, resource observations, billing estimates and verified cleanup.
- Committed provider lock files for Hetzner and Google Cloud with Terraform and OpenTofu checksums for Linux and macOS on amd64/arm64.
- A mocked Hetzner provider contract test, plus OpenTofu validation and contract tests in CI.
- Package metadata (authors, classifiers, keywords, documentation and changelog links) and upper bounds for runtime dependencies.
- Issue and pull request templates and a code of conduct.
- A one-line installer (`install.sh`) for macOS, Linux and WSL2 that installs uv if needed, installs offbox and starts `offbox setup`, and an installation guide with step-by-step platform and provider instructions.
- `offbox setup` downloads pinned, SHA-256-verified Mutagen 0.18.1 when no Mutagen 0.18.x is installed, and offers to create a dedicated `~/.ssh/offbox_ed25519` key when no key exists.
- `host init --host-key-fingerprint` (also on `setup`) verifies an existing server's key against a fingerprint obtained out of band.
- `host destroy --forget` removes the local record of an unreachable existing-SSH host.
- `exec.env` names are approved by `offbox up`; `exec` refuses names that changed since and refuses provider credential variables.
- Help text for every CLI option, and `exec --help` shows the `-- COMMAND` form.
- A git mirror pushes `HEAD`, the remote's default branch and `git.refs` into `/work/.git` so git-based tooling works in the environment, and `[files] copy` copies named home-directory files such as gcloud credentials into it; new copies and refs need approval by `offbox up`.
- Commands run by offbox see `OFFBOX_ENV` set to the environment name.
- Project settings can also come from `[project]` tables in the user `config.toml`, an uncommitted `offbox.local.toml` and `OFFBOX_<SECTION>_<KEY>` environment variables, merged per key with later sources winning.
- `offbox config [--json]` shows every effective project setting with its source and variable name.

### Changed

- The git mirror is on by default and also mirrors the remote's default branch (`origin/HEAD`); set `git.mirror = false` to opt out. Environments without a recorded mirror approval accept the default mirror without a new `up`; others need one `offbox up`.
- Fix lines that named `offbox.toml` now point to `offbox config`, because a value can come from any settings source.
- Host bootstrap waits up to 10 minutes for apt/dpkg locks, enables unattended upgrades last, and keeps installed containerd and Docker plugin versions when rerun so `host init` does not restart Docker.
- The universal image base is pinned by digest, upgrades Ubuntu packages during the build and is rebuilt weekly without layer cache.
- The README describes the problem offbox solves, measured results, comparisons and common questions; development details moved to CONTRIBUTING.md.
- `host destroy` always runs the cloud destroy, treating local Mutagen and SSH cleanup failures as warnings, and reinitializes the provider workspace first so a cleared cache cannot block it.
- `uninstall --yes` refuses while cloud hosts remain unless `--abandon-cloud-hosts` is given; existing-SSH host teardown also removes the `offbox` sudoers entry and authorized key.
- `up --fresh` requires confirmation or `--yes`, `doctor` fails when no host is configured, and `setup --action check` no longer edits SSH configuration.
- Warm `exec` runs one remote preflight instead of three probes and restores port forwards alongside the command; readiness polling is faster.
- `up` warns when CPU, memory, shared-memory or image settings differ from the running container.
- Automatic Compose forwards are capped at 32 per environment.
- State and cache directories are private (0700), and the previous state file is kept as `state.json.bak`.

### Fixed

- Ctrl-C, SIGTERM and SIGHUP relay into remote process groups works again: `offbox-run` used a `kill -s NAME -- -PGID` form that the image's procps-ng rejects.
- A non-TTY `offbox exec` killed without a chance to relay (SIGKILL, lost network) no longer leaves its remote command running.
- Changing `install.command` reinstalls dependencies on the next `exec`, not only changes to the `install.when` files.

- Interrupting provisioning no longer kills OpenTofu mid-apply; Ctrl-C after a VM exists prints the `host destroy` command and exits 130.
- An unreadable state file reports the backup and the manual `tofu destroy` commands for each provider workspace.
- Generated SSH configuration disables agent and X11 forwarding and offers only the configured key, so user-wide `Host *` settings no longer apply to offbox hosts; `host init` quotes the known-hosts path.
- Every `--env` and `exec.env` value is redacted from logs and verbose output as `NAME=value`, regardless of the variable name; secret-named and long values are also redacted on their own.
- `exec` no longer waits for another worktree's `up`, concurrent `exec` calls in one worktree wait for a running installation instead of failing, and port-restore errors no longer prevent the command from running.
- Closing the terminal relays termination to the remote command instead of orphaning it.
- Concurrent invocations no longer fail while pruning old log files; `logs --follow` reads incrementally.
- `ports --swap` works when the port's current owner is stopped.
- The image freshness check recognises Docker's containerd image store digests.
- Fix lines on non-terminal stderr are no longer wrapped at 80 columns.
- `uninstall` stops a Mutagen daemon started from the downloaded copy before removing it.
- A failed host bootstrap writes `/var/lib/offbox/failed`, and `host create` reports it immediately with the end of the cloud-init log instead of waiting for the 15-minute readiness timeout.
- Host bootstrap and the image verify the Docker apt signing key fingerprint before trusting it.
- Host bootstrap reloads SSH with `try-reload-or-restart`, which works with Ubuntu 24.04 socket activation, and no longer locks the `offbox` account, which sshd rejects when `UsePAM` is off.
- Commands run through `offbox exec` use umask 022 instead of inheriting 077, so build outputs are no longer owner-only.
- The inner Docker daemon log is capped at 10 MiB with one rotated file.
- Wheels and host provider workspaces no longer include local `.terraform/` directories, Terraform state, editor swap files or provider tests.
- Existing-host teardown validates resource ownership, preserves adopted networks and refuses foreign network members before deletion; environment cleanup handles root-owned source directories.
- Environment shutdown forwards signals to dockerd and waits within the outer stop deadline.
- Routine command and forwarding lock contention uses bounded waits; unconfirmed SSH completion includes per-run recovery guidance.
- Public contribution workflows use restricted credentials and publication jobs; release tags remain blocked while publishing is deferred.
- Batched port discovery streams large responses instead of passing them as process arguments; empty install markers report the correct recovery action.
- Cloud host creation refreshes early SSH connections and verifies Docker access before reporting readiness, so bootstrap's Docker-group changes take effect.
- `up` no longer holds the host lock while waiting for another installation, a busy host lock no longer prints a port-restore warning during `exec`, and an interrupted `exec` waits at most one second for port restore.
- `up` no longer reports a settings change for containers created before settings were recorded; `host create` and `host init` default to `~/.ssh/offbox_ed25519` when `~/.ssh/id_ed25519` is missing, matching `setup`.
- The installer prints the PATH line to add when it cannot update the shell profile, finds uv in `UV_INSTALL_DIR` or `XDG_BIN_HOME`, and reports an unfinished `offbox setup` without failing.
- Execution combines dependency hash/marker reads and owned-container/Compose port discovery into fresh probes, removing two sequential SSH round trips without skipping checks.

## [0.1.0] - 2026-10-23

PyPI publication and release tags are deferred. This version identifies the v0.1 source implementation.

### Added

- Hetzner/OpenTofu provisioning and existing-SSH host initialization with pinned SSH host keys and recoverable local provider state.
- Per-worktree privileged environments with separate Docker daemons, resource limits, persistent data, ownership checks and garbage collection.
- Public universal amd64 image with pinned development tools, Docker Compose, browser dependencies and execution/readiness shims.
- Two-way-safe Mutagen synchronization, mandatory Git exclusion, dependency-file hashing and explicit project setup commands.
- Streaming command execution with scoped environment variables, stdin/TTY modes, preserved exit codes and bounded signal cleanup.
- Stable loopback port allocation, SSH forward restoration and port swaps across environments.
- Typed health reports, scoped logs, guided setup and local uninstall that preserves remote hosts.
- Configuration, architecture, provider, security and troubleshooting guides, plus FastAPI/Postgres and Vite/Playwright examples.
- Focused Linux Docker/SSH smoke validation without cloud credentials or billable cloud resources.

## [0.0.1] - 2026-10-23

Initial source skeleton; not published to PyPI.

### Added

- Typed Python CLI skeleton with `offbox --version`, `offbox version` and `python -m offbox`.
- Locked uv dependencies, formatting, linting, strict type checks and unit tests.
- Linux/macOS CI and trusted-publisher release workflow.
- Contributor instructions and packaging configuration for future host, image and provider resources.

[Unreleased]: https://github.com/o-avdieiev/offbox/commits/master
[0.1.0]: https://github.com/o-avdieiev/offbox/issues/1
[0.0.1]: https://github.com/o-avdieiev/offbox/tree/37ba5be5c132161441ca045def3edb5da26e9065
