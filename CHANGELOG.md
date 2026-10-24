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

### Changed

- Host bootstrap waits up to 10 minutes for apt/dpkg locks, enables unattended upgrades last, and keeps installed containerd and Docker plugin versions when rerun so `host init` does not restart Docker.
- The universal image base is pinned by digest, upgrades Ubuntu packages during the build and is rebuilt weekly without layer cache.
- The README describes the problem offbox solves, measured results, comparisons and common questions; development details moved to CONTRIBUTING.md.

### Fixed

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

[Unreleased]: https://github.com/Avdeev99/offbox/commits/master
[0.1.0]: https://github.com/Avdeev99/offbox/issues/1
[0.0.1]: https://github.com/Avdeev99/offbox/tree/37ba5be5c132161441ca045def3edb5da26e9065
