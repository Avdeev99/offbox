# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Google Cloud provisioning with a dedicated per-host network, Ubuntu cloud-init, no attached VM service account, standard Google authentication and mocked Terraform contract tests.
- A Google Cloud benchmark of both sample apps and a same-VM comparison measuring a 12.2% median command latency reduction from batching checks, with verified cloud cleanup.
- Opt-in `--timings` summaries for provisioning, synchronization, setup, forwarding and command execution, without changing stdout or exit codes.
- A real Hetzner benchmark of both sample applications, including timings, resource observations, billing estimates and verified cleanup.

### Fixed

- Existing-host teardown validates resource ownership and preserves adopted networks; environment cleanup handles root-owned source directories.
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
