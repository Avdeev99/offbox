# Universal image

The package's `DEFAULT_IMAGE` selects the universal image compatibility series. The Dockerfile targets Ubuntu 24.04 on `linux/amd64`, pinned by image digest, and deliberately rejects other build architectures. The build upgrades base packages; a weekly scheduled workflow rebuilds and republishes the image without layer cache so it picks up Ubuntu security updates. Host bootstrap and the image both install Docker Engine/CLI major 29 from Docker's signed Ubuntu apt repository. Buildx and Compose plugins are included.

The `dev` user has UID/GID 1000, passwordless sudo and Docker access. This image runs privileged and is intended for a single user's trusted development projects. `/work` is the synchronized project directory. `/run/offbox/exec` is writable by `dev`; `/var/lib/docker` is the per-environment Docker volume.

## Included tools

| Tool | Build selection |
| --- | --- |
| Python | uv-managed Python 3.13, default Python command |
| uv | 0.13.5, including uvx |
| Python tooling | pipx from Ubuntu packages; Poetry installed as a uv tool |
| Node.js | 22.23.4 LTS, with Corepack for pnpm/yarn |
| Bun | 1.4.4, amd64 baseline build |
| Go | 1.27.3 |
| Rust | Stable 1.99.1, rustup 1.29.2 minimal profile |
| mise | 2026.10.18, with shims on noninteractive PATH |
| Playwright | Chromium OS dependencies selected by 1.64.1 |

The image includes C/C++ build tools, Git, SSH client, jq, Make, CMake, PostgreSQL/Redis/SQLite clients, ffmpeg, and SSL/PQ development libraries. UTF-8 locale data is generated. It contains no browser download: projects install the matching Playwright browser into `/home/dev/.cache/ms-playwright`.

Direct Node, Go, uv, Bun, mise and rustup artifacts have embedded SHA256 checksums checked before extraction or execution. Rustup then verifies the selected Rust distribution through its upstream manifests. Refresh versions and checksums together from their official release sources. Docker's apt signing key is accepted only when its primary fingerprint is `9DC858229FC7DD38854AE2D88D81803C0EBFCD88`; host bootstrap applies the same check. Apt and npm-managed dependencies follow their package-manager verification mechanisms.

## Process scripts

`offbox-init` configures overlay2, separate inner Docker address pools and the local log driver. Startup clears volatile Docker PID/socket/exec-root files so a stopped environment can restart without stale containerd process identities; `/var/lib/docker` remains on its retained data volume. On cgroup v2 it moves processes into an init subgroup before enabling available CPU, memory, PID and I/O controllers for nested containers. It starts dockerd with Unix-socket access only, waits up to approximately 60 seconds for a successful bounded `docker info` call, and creates `/run/offbox/ready`. Daemon output goes to `/var/log/dockerd.log`, capped at 10 MiB with one previous file kept as `dockerd.log.1`. Startup failure prints the daemon log tail. Tini reaps orphaned children while the entrypoint remains alive.

`offbox-run [--tty] ID CWD -- ARGV...` executes as `dev`. Non-TTY commands start in a new session/process group; TTY commands keep their controlling terminal. Both inherit stdin explicitly and reset ignored interrupt/termination/hangup signals before executing the requested command. The command runs with umask `022`; the wrapper keeps `077` only for its own state files. The wrapper waits for completion and records the child PID, process start time and actual exit code under `/run/offbox/exec/`. Signal exits retain the shell's `128 + signal` status.

A non-TTY command joins its own cgroup, `/sys/fs/cgroup/offbox/<ID>`, before it executes, so every process it starts stays in that cgroup even after `setsid` or daemonising. Joining needs root; the wrapper calls its internal `offbox-run --cgroup join|release|stop|prune` mode through `sudo -n` with stdin from `/dev/null`. It requires cgroup v2 with `cgroup.kill` (Linux 5.14 or later) and passwordless sudo for `dev`; without them the command runs as before and a stop reaches its process group only.

`offbox-run --signal INT|TERM|KILL ID` targets a non-TTY process group after checking its recorded start time and current group identity. It signals with `kill -SIGNAL -- -PGID`, the form procps-ng accepts for process groups, and first records that the run is being stopped. When the command then exits, the wrapper sends TERM to the run's remaining processes outside its process group, waits up to three seconds, kills whatever is left with `cgroup.kill` and removes the cgroup, and only then records the exit code. KILL also kills the whole cgroup at once. A command that exits on its own keeps any background processes it started; a later run removes its cgroup once they are gone. Completed runs are harmless to signal again and their background processes are not signalled. TTY commands use terminal controls; the external helper refuses to signal their shared terminal group. Completed records and orphaned metadata older than one day are cleaned on new runs; valid active records are retained.

Custom images used with `env.image` or `env.dockerfile` must keep these scripts, the `dev` user with UID 1000 and home `/home/dev`, the inner Docker daemon and its ready file, and `git` on `PATH` for the [git mirror](configuration.md#git-mirror). Without `git`, offbox warns and skips the mirror; set `git.mirror = false` to silence it.

## Build and publication

The GitHub image workflow builds amd64 natively, checks both scripts with ShellCheck and exports an OCI archive to measure compressed image content. Pull requests do not publish. `master` changes publish compatibility/commit tags; version tags add release aliases. See [contributor publication setup](../CONTRIBUTING.md#universal-image-and-one-time-ghcr-setup).

Validation includes static checks, unit tests for Python orchestration, native image builds and one focused Linux Docker smoke job. The smoke fixture uses the current checkout image with real SSH and Mutagen, nested Compose stacks, command streams/status/signals and port forwards. It uses disposable local containers and no cloud credentials. Its host registration is seeded, so cloud provisioning and VM bootstrap remain outside its coverage. Browser, OOM and performance behavior are also outside the focused job; a successful image build alone establishes installation, not those runtime paths.

The container init process supervises dockerd, forwards TERM/INT and waits for daemon shutdown. `offbox stop` allows a 30-second outer grace period; inner services should handle termination and persist their data. An unexpected daemon exit stops the environment instead of leaving a stale ready marker.
