# Installation

Offbox runs on your laptop and drives one Ubuntu 24.04 amd64 host over SSH. You need a client (macOS, Linux, or Windows through WSL2) and either cloud credentials or an existing server.

- [macOS and Linux](#macos-and-linux)
- [Your first project](#your-first-project)
- [Windows (WSL2)](#windows-wsl2)
- [Hetzner](#hetzner)
- [Google Cloud](#google-cloud)
- [Existing Ubuntu server](#existing-ubuntu-server)
- [Troubleshooting](#troubleshooting)

## macOS and Linux

### 1. Check your laptop tools

You need Git 2.30+, OpenSSH 8+ and curl for the installer. Check `git --version` and `ssh -V`. You do **not** need local Docker, a separate Python installation, or a manually installed OpenTofu/Terraform. uv manages the CLI's Python; offbox can download its sync and provisioning tools.

| System | Install missing laptop tools |
| --- | --- |
| macOS | Run `xcode-select --install` and finish the installer; OpenSSH and curl normally ship with macOS. |
| Debian, Ubuntu | Run `sudo apt update`, then `sudo apt install curl git openssh-client`. |
| Fedora | Run `sudo dnf install curl git openssh-clients`. |

If you will use an existing passphrase-protected SSH key, load it into your local agent **before the installer starts setup**, for example `ssh-add ~/.ssh/id_ed25519`. If Linux or WSL reports that no agent is running, start one with `eval "$(ssh-agent -s)"`, then run `ssh-add` again. Offbox does not forward this agent to the VM. If you have no key, setup can create one; you do not need to create it manually first.

### 2. Prepare a host account

Choose one:

- [Hetzner](#hetzner): create a Read & Write API token and export `HCLOUD_TOKEN`, or save it in the private credentials file.
- [Google Cloud](#google-cloud): enable billing and Compute Engine, export `GOOGLE_PROJECT`, and configure Application Default Credentials.
- [Existing Ubuntu server](#existing-ubuntu-server): have its SSH key, address and passwordless sudo access ready; no cloud API token is needed.

Setup reads credentials; it never prompts for their values. If credentials for multiple clouds are present, choose the provider in the setup prompt or pass `--provider hetzner` or `--provider gcp`. Credentials do not select or switch the provider automatically.

### 3. Install the released CLI

The one-command route is:

```sh
curl -LsSf https://raw.githubusercontent.com/o-avdieiev/offbox/master/install.sh | OFFBOX_SOURCE=offbox sh
```

The [installer](../install.sh) installs [uv](https://docs.astral.sh/uv/getting-started/installation/) if missing, then installs offbox and starts guided setup on a terminal. `OFFBOX_SOURCE=offbox` selects the published PyPI package. The installer itself comes from this repository; you can inspect it before running it. Its default when `OFFBOX_SOURCE` is omitted is the development version from Git, not the PyPI release.

If you already have uv, the manual equivalent is:

```sh
uv tool install offbox
uv tool update-shell
```

Without uv, install it from [the official instructions](https://docs.astral.sh/uv/getting-started/installation/), open a new terminal as instructed, then run the two commands above. uv supplies a compatible Python if needed. `uv tool update-shell` adds the executable directory to supported shell profiles; open a new terminal for that change to take effect. See [uv's tool guide](https://docs.astral.sh/uv/guides/tools/).

Check installation:

```sh
offbox --version
```

If `offbox` is not found, reopen your terminal. For the current POSIX shell you can also run `export PATH="$(uv tool dir --bin):$PATH"` once uv is available.

### 4. Complete guided setup

If the installer did not already complete it:

```sh
offbox setup
```

Setup asks for a provider and whether to create a cloud host or bootstrap an existing server. To choose a custom host name or size, use the manual installation route above and pass setup flags. If the installer already opened setup, decline creation, then rerun setup with those flags. You can answer no to check prerequisites without creating a VM. It:

- checks Git, OpenSSH, configuration and state, and prints a `Fix:` for unmet prerequisites;
- offers a verified Mutagen 0.18.1 download if no compatible version is installed;
- downloads verified OpenTofu when neither `tofu` nor `terraform` is installed;
- offers a dedicated `~/.ssh/offbox_ed25519` key without a passphrase when no default key exists; use `--ssh-key PATH` to choose another;
- creates or registers a host, normally named `main`, and makes the first manual host the default.

**Cloud creation is billable.** Review the size and current provider pricing before approving: defaults are Hetzner `ccx33` and Google Cloud `e2-standard-4`. To choose another size or zone, pass `--size` and `--region`, for example `offbox setup --provider hetzner --size YOUR_SIZE --region YOUR_LOCATION`. GCP's `--region` accepts a zone. Setup-created hosts stay until explicitly destroyed; merely closing a terminal does not stop their billing.

Without a terminal, pass the provider and action explicitly, for example `offbox setup --provider hetzner --action check`. Tool downloads that require an interactive offer and missing SSH keys must be prepared first; `check` can still download provisioning tooling but creates no cloud host. Do not put an unattended `--action create` command in a script unless VM creation is intended. See `offbox setup --help`.

Verify the host with `offbox host list` and `offbox doctor`. Creating the first host needs cloud credentials or existing-server access; installing the CLI alone does not provide a host.

### 5. Start your project

From a Git checkout or linked worktree:

```sh
cd your-repo
offbox up
offbox exec -- uname -s
offbox ports
```

`uname` should print `Linux`: it ran in the remote environment. Your laptop Docker daemon is not used. `up` starts file sync and a root Compose stack when present. It installs project dependencies only when you declare how; continue with [your first project](#your-first-project). Use `offbox status` to inspect the current environment.

### 6. Clean up

```sh
offbox rm --yes
offbox host list
offbox host destroy main --yes
```

Use the actual host name from the list if you chose another name. `rm` deletes the worktree's remote source and Docker data, including databases. It leaves a manual host running; `host destroy` deletes the cloud VM and its environments. `offbox stop` retains data and does not stop VM billing. Back up important data first.

[Auto-managed hosts](configuration.md#auto-managed-hosts) are an alternative to manual setup: `up` creates a named preset on demand, and the last `rm` can remove it. They are opt-in. Idle destruction additionally needs `offbox host reap` or its timer, running on your laptop. A sleeping laptop delays cleanup; there is no provider-enforced budget cap.

### Update an installation

For the PyPI installation above:

```sh
uv tool upgrade offbox
```

If you previously installed from Git and want to switch to the published package, run `uv tool install --force offbox`. Destroy hosts before uninstalling; uninstalling the CLI is not cloud cleanup.

If you already use another Mutagen version, offbox reports it instead of downloading a second one: binaries for the same user share a daemon. Run `mutagen daemon stop`, remove the incompatible binary from `PATH`, and rerun `offbox setup` to use its managed download.

## Your first project

All configuration is optional. Offbox reads `offbox.toml` at the **Git worktree root**, not the directory containing an individual service in a monorepo. `offbox config` shows the effective settings and their sources.

For a Python project with a committed `uv.lock`, add:

```toml
[install]
command = "uv sync --frozen"
when = ["uv.lock"]
```

Then run `offbox up` and `offbox exec -- uv run pytest` (assuming pytest is a project dependency). For an npm project use `command = "npm ci"`, `when = ["package-lock.json"]`, then `offbox exec -- npm test`. The next `exec` reinstalls when the declared lock files or install command change.

Compose is auto-started only for recognized files at the worktree root. For a nested stack or a repository-specific developer CLI, declare a `stack.up` command in the [configuration reference](configuration.md#project-settings). Local `.venv` and `node_modules` directories are excluded from sync; dependencies are installed on Linux rather than copied from your laptop.

For complete runnable applications, copy the [Python/Postgres](../examples/python-compose/) or [Vite/Playwright](../examples/node-playwright/) example into its own Git repository as its README explains.

### Daily use and multiple worktrees

| Task | Command |
| --- | --- |
| Start or resume this worktree | `offbox up` |
| Run tests or builds remotely | `offbox exec -- YOUR_COMMAND` |
| Open an interactive remote shell | `offbox shell` |
| Find the browser/database URL | `offbox ports` (use the listed local port) |
| Inspect resolved settings | `offbox config` |
| Check the environment | `offbox status` |
| Stream Compose logs | `offbox logs --stack --follow` |
| Stop the environment and retain data | `offbox stop` |
| Delete the environment and its data | `offbox rm --yes` |

Create worktrees with Git as usual, then run `offbox up` in each. They share your selected VM but have separate Docker daemons and volumes; local port mappings can differ. Source sync is two-way and includes `.env` files unless you exclude them. Git mirroring is on by default, but commits and pushes stay local. Files outside the worktree and credentials need deliberate configuration; see [copied files](configuration.md#copied-files) and [the trust model](security.md).

## Windows (WSL2)

Offbox does not run in native PowerShell, cmd or Git Bash. Use WSL2:

1. In PowerShell opened as Administrator, run `wsl --install`, then restart Windows if requested. Follow [Microsoft’s WSL installation guide](https://learn.microsoft.com/en-us/windows/wsl/install) if WSL is unavailable.
2. Open **Ubuntu** from the Start menu and create your Linux user.
3. Run `wsl --list --verbose` in PowerShell and check that Ubuntu uses version **2**. Follow the [macOS and Linux](#macos-and-linux) steps inside Ubuntu; install offbox there, not into Windows Python.
4. Keep repositories in the Linux filesystem, for example `~/src/project`, not under `/mnt/c`. File watching and sync on `/mnt/c` are slow and unreliable.
5. Open those repositories with the VS Code WSL extension or JetBrains WSL support, and run coding agents inside WSL so they use the same files and the `offbox` command.

## Hetzner

1. Sign in to the [Hetzner Console](https://console.hetzner.com/). New accounts may need identity verification before servers can be created.
2. Create a project, or select an existing one.
3. Open **Security** → **API tokens** and choose **Generate API token**.
4. Give it a description, select **Read & Write**, and copy the token. It is shown only once.
5. Provide it to offbox in one of two ways:

   - Run this export in your **current terminal**. You can also add it to your shell profile (`~/.zshrc`, `~/.bashrc`) for future terminals:

     ```sh
     export HCLOUD_TOKEN=your-token
     ```

   - Or in `~/.config/offbox/credentials.toml` (`$XDG_CONFIG_HOME/offbox/credentials.toml`), which must not be readable by others:

     ```toml
     [hetzner]
     token = "your-token"
     ```

     ```sh
     chmod 600 ~/.config/offbox/credentials.toml
     ```

   `HCLOUD_TOKEN` takes precedence over the file. For optional scheduled idle cleanup, use the file: the reaper timer does not inherit a token exported only in your shell.

6. Credentials are ready. If offbox is not installed yet, return to [install the released CLI](#3-install-the-released-cli). After installation, run `offbox setup --provider hetzner` if setup has not completed, and approve creation. In a cloud project shared with other laptops, use a distinct name, for example `offbox setup --provider hetzner --name alex-laptop`. Servers bill until `offbox host destroy`; see [Hetzner pricing](https://www.hetzner.com/cloud/). More detail: [providers](providers.md#hetzner-cloud).

## Google Cloud

1. Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install) and run `gcloud auth login`.
2. Create or select a project and [enable billing](https://cloud.google.com/billing/docs/how-to/modify-project) for it.
3. Enable the Compute Engine API:

   ```sh
   gcloud services enable compute.googleapis.com --project your-project-id
   ```

4. Configure Application Default Credentials, which offbox's OpenTofu provider uses. A plain `gcloud auth login` is not enough:

   ```sh
   gcloud auth application-default login
   ```

5. Export the project in your **current terminal**. Optionally add the same line to your shell profile for future terminals:

   ```sh
   export GOOGLE_PROJECT=your-project-id
   ```

6. Make sure your identity has **Compute Instance Admin (v1)**, **Compute Network Admin** and **Compute Security Admin** on the project, or equivalent narrower permissions.
7. Credentials are ready. If offbox is not installed yet, return to [install the released CLI](#3-install-the-released-cli). After installation, run `offbox setup --provider gcp` if setup has not completed, and approve creation. Use `--name` to choose a name unique to this laptop in a shared cloud project. VMs, disks and external IPs bill until `offbox host destroy`. More detail: [providers](providers.md#google-cloud).

## Existing Ubuntu server

Requirements:

- Ubuntu 24.04 on amd64, reachable over SSH with your key;
- a login account with passwordless (noninteractive) `sudo`;
- UID 1000 unused, or already owned by an `offbox` account;
- no conflicting Docker packages and no Docker CE major other than 29.

Offbox installs Docker and an `offbox` account and leaves the owner's account and SSH settings alone; see [providers](providers.md#existing-ssh-server).

1. On the server, print its host key fingerprint:

   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   The output looks like `256 SHA256:AbC…xyz root@server (ED25519)`; copy the `SHA256:…` field. Get it from the server's console or another trusted channel, not over the connection you are about to trust.

2. If offbox is not installed yet, return to [install the released CLI](#3-install-the-released-cli) and choose the existing-server provider during setup. After installation, register and bootstrap the server with its verified fingerprint:

   ```sh
   offbox setup --provider ssh --action init --target user@host --host-key-fingerprint SHA256:...
   ```

   Offbox scans the server's key and stops if it does not match. Add `--port` for a nondefault SSH port and `--ssh-key` for a specific key. On a terminal you can omit the fingerprint and confirm the displayed one instead.

## Troubleshooting

Run `offbox doctor` for local prerequisites and `offbox status` inside a worktree. Every error ends with a `Fix:` line; [troubleshooting](troubleshooting.md) lists them by area.
