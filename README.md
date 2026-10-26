# offbox

[![Test](https://github.com/o-avdieiev/offbox/actions/workflows/test.yaml/badge.svg?branch=master)](https://github.com/o-avdieiev/offbox/actions/workflows/test.yaml)
[![License: MIT](https://img.shields.io/github/license/o-avdieiev/offbox)](LICENSE)
[![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)](pyproject.toml)

Give every git worktree its own Docker stack on one cloud VM you own. Your compose files don't change.

Running several coding agents or worktrees in parallel means several full Docker Compose stacks: databases, browsers, builds and test runs competing for one laptop's CPU, memory and ports. Offbox moves those stacks to a single VM and gives each worktree its own Docker daemon, volumes and port forwards. Your editor, agents and Git stay on the laptop.

On a Google Cloud `e2-standard-4` VM, a warm `offbox exec -- true` took a median **1.07 s** end to end, including the sync flush and dependency checks; a first `up` of the FastAPI/Postgres sample took **39 s**, and the VM cost about **$0.15/hour**. See the [Google Cloud](docs/benchmarks/2026-10-23-gcp.md) and [Hetzner](docs/benchmarks/2026-10-23-hetzner.md) benchmark reports for method and limits.

## Quickstart

macOS, Linux, or Windows via WSL2 · git and ssh — `offbox setup` installs the rest.

```sh
curl -LsSf https://raw.githubusercontent.com/o-avdieiev/offbox/master/install.sh | sh   # installs uv + offbox, runs offbox setup
cd your-repo
offbox up
offbox exec -- pytest
offbox ports
```

`setup` checks your tools, downloads verified Mutagen and OpenTofu when they are missing, offers a dedicated SSH key if you have none, and then creates a Hetzner or Google Cloud host in your own account or registers an existing Ubuntu 24.04 server. Cloud credentials come from your environment, never from a prompt. Creating a cloud host is billable; review its size and current price before approving. The [installation guide](docs/installation.md) walks through each platform and provider step by step, including a manual install without the script.

When finished with a worktree, use `offbox rm --yes`. To delete a cloud host and stop its server charges:

```sh
offbox host list
offbox host destroy main --yes
```

`offbox stop` retains the environment's data. It does not delete the server or stop cloud billing. Hetzner bills servers until deletion, including powered-off servers. See [current pricing](https://www.hetzner.com/cloud/) and [billing rules](https://docs.hetzner.com/cloud/billing/faq/).

## How it works

```mermaid
flowchart LR
    subgraph Laptop
        Editor[Editor and coding agents]
        A[Git worktree A]
        B[Git worktree B]
        CLI[offbox CLI and local state]
    end
    subgraph Host[Personal SSH host]
        WA[Work directory A]
        WB[Work directory B]
        EA[Environment A: Docker daemon + volumes]
        EB[Environment B: Docker daemon + volumes]
        WA --> EA
        WB --> EB
    end
    A <-->|Mutagen: source files| WA
    B <-->|Mutagen: source files| WB
    CLI -->|SSH: commands and port forwards| EA
    CLI -->|SSH: commands and port forwards| EB
```

Both worktrees can publish remote port 8000 and use the same Compose container names. Offbox gives them separate Docker daemons and local forwards such as `localhost:8000` and `localhost:8001`. `offbox ports --swap 8000` exchanges the preferred local port when an app needs a fixed callback URL.

`up` creates or starts the environment, synchronizes files, installs declared dependencies, starts the declared stack and restores forwards. `exec` flushes sync, checks dependency hashes and streams the requested command with its actual exit code. Git metadata is excluded from sync; run Git locally.

Use `offbox --timings up` or `offbox --timings exec -- COMMAND` to see stage durations on stderr. Timing totals exclude Python/CLI startup; command output and exit codes are preserved.

## Why not DevPod, Codespaces or Docker over SSH?

- **Worktree-native.** Each worktree gets its own Docker daemon, volumes and port forwards on the shared VM, so several worktrees can run the same Compose project, with the same container names and ports, at the same time.
- **Nothing to adopt.** No `devcontainer.json`, no changes to your compose files and no offbox daemon or control plane to run. An optional `offbox.toml` declares install and stack commands when the defaults are not enough.
- **Your editor, agents and Git stay local.** Mutagen keeps files in sync both ways; commands and port forwards go over SSH.
- **One VM you own.** Hetzner, Google Cloud or any Ubuntu 24.04 server you can reach over SSH. You pay the provider directly.

Use DevPod or Codespaces if you want a full remote editor environment defined by `devcontainer.json`. Use sandbox APIs such as [E2B](https://e2b.dev/docs) for untrusted code.

## Requirements and scope

- macOS or Linux client, Python 3.12+, Git 2.30+, OpenSSH 8+ and Mutagen 0.18.x; `offbox setup` downloads a verified Mutagen 0.18.1 if none is installed. Windows clients use WSL2.
- An Ubuntu 24.04 amd64 host. The universal image is Linux amd64; an Apple Silicon laptop is supported as a client.
- OpenTofu 1.8+ or Terraform 1.6+ for cloud provisioning. If neither is installed, Offbox can download verified pinned OpenTofu.
- Trusted personal projects. Environment containers are privileged and share the host kernel; they are not a sandbox for untrusted code. Two-way sync also lets code on the VM change files in your worktree. Read the [security model](docs/security.md).

See [limits](docs/limits.md) for the full operating model.

## FAQ

**Does it work on Apple Silicon or arm64?** An arm64 laptop works as a client. Hosts must be amd64, because the universal image is built only for `linux/amd64`; arm64 host images are not provided.

**Does an idle host stop automatically?** No. There is no automatic idle stop or destroy. `offbox stop` stops an environment but not the VM or its billing; run `offbox host destroy NAME --yes` when you are done.

**What does it cost?** Whatever your provider charges for the VM. The benchmarked Google Cloud `e2-standard-4` was about $0.15/hour plus disk and IP charges; the default Hetzner size is `ccx33`. Check current provider pricing before creating a host.

**Can I use another cloud?** The built-in providers are Hetzner, Google Cloud and existing SSH servers. Any Ubuntu 24.04 amd64 server works through `ssh`, and you can add an OpenTofu/Terraform provider; see [writing a provider](docs/writing-a-provider.md).

**Does it support Windows?** Use WSL2 as the client; see [Windows (WSL2)](docs/installation.md#windows-wsl2).

**Do I need a devcontainer?** No. Offbox runs your existing Compose files in the universal image. Set `env.image` or `env.dockerfile` in `offbox.toml` to use your own image.

## Configuration and examples

An `offbox.toml` at the Git worktree root configures resources, dependency installation, stack commands, forwards, sync ignores, forwarded variables, copied files and the git mirror. Every key is optional, and each can also come from your user config (for every repository), an uncommitted `offbox.local.toml` or an `OFFBOX_*` variable; `offbox config` shows the result and where each value came from. The environment gets a real Git repository by default. With `auto = true` on a host preset, offbox creates the VM when a worktree first needs it and destroys it after the last one is removed or it sits idle, so a forgotten VM does not keep billing. See the [complete configuration reference](docs/configuration.md).

- [FastAPI + Postgres](examples/python-compose/): real database-backed API and tests, with ports 8000 and 5432.
- [Vite + Playwright](examples/node-playwright/): a small web app and browser test, with port 3000.
- [Agent instructions and JSON contracts](docs/agents.md).
- [Architecture and lifecycle](docs/architecture.md).
- [Installation](docs/installation.md), [providers](docs/providers.md), [writing a provider](docs/writing-a-provider.md), and [troubleshooting](docs/troubleshooting.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, checks and CI, and [SECURITY.md](SECURITY.md) for private vulnerability reporting. Offbox is not yet published to PyPI; install it from this repository as shown above. Licensed under [MIT](LICENSE).
