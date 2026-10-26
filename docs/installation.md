# Installation

Offbox runs on your laptop and drives one Ubuntu 24.04 amd64 host over SSH. You need a client (macOS, Linux, or Windows through WSL2) and either cloud credentials or an existing server.

- [macOS and Linux](#macos-and-linux)
- [Windows (WSL2)](#windows-wsl2)
- [Hetzner](#hetzner)
- [Google Cloud](#google-cloud)
- [Existing Ubuntu server](#existing-ubuntu-server)
- [Troubleshooting](#troubleshooting)

## macOS and Linux

1. **Check git and ssh.** Both are usually present. `git --version` must report 2.30 or newer and `ssh -V` OpenSSH 8.0 or newer. To install them:

   | System | Command |
   | --- | --- |
   | macOS | `xcode-select --install`, or `brew install git` |
   | Debian, Ubuntu | `sudo apt install git openssh-client` |
   | Fedora | `sudo dnf install git openssh-clients` |

2. **Provide cloud credentials** for the host you want, or prepare an existing server:
   - [Hetzner](#hetzner): `export HCLOUD_TOKEN=...`
   - [Google Cloud](#google-cloud): `export GOOGLE_PROJECT=...` and `gcloud auth application-default login`
   - [Existing Ubuntu server](#existing-ubuntu-server): no cloud account needed

   `offbox setup` never asks for tokens; it reads them from the environment or the credentials file.

3. **Install offbox** with the installer:

   ```sh
   curl -LsSf https://raw.githubusercontent.com/o-avdieiev/offbox/master/install.sh | sh
   ```

   The [installer](../install.sh) checks git and ssh, installs [uv](https://docs.astral.sh/uv/) with its official installer if it is missing, installs offbox with `uv tool install`, and then starts `offbox setup` when run from a terminal. uv provides Python 3.12+ for the tool if your system lacks it.

   To install manually instead:

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   uv tool install git+https://github.com/o-avdieiev/offbox
   ```

4. **Run setup** if the installer did not start it:

   ```sh
   offbox setup
   ```

   Setup asks for a provider and whether to create a host, then:

   - checks git, ssh, Mutagen, OpenTofu, your configuration and state, and prints a `Fix:` line for each problem;
   - offers to download Mutagen 0.18.1 into `~/.cache/offbox/tools/mutagen/` if no Mutagen 0.18.x is on your `PATH`; the archive is verified against a pinned SHA-256 before it is unpacked;
   - downloads pinned, SHA-256-verified OpenTofu into the same cache if neither `tofu` nor `terraform` is installed;
   - offers to create a dedicated key `~/.ssh/offbox_ed25519` (no passphrase) when `~/.ssh/id_ed25519` does not exist, and uses an existing `~/.ssh/offbox_ed25519` automatically;
   - creates the cloud host, or registers your existing server; the first host becomes the default (`offbox host use NAME` changes it).

   **Creating a cloud host costs money.** It creates a VM in your own account that bills until you destroy it, even while it is stopped or idle. Check the size and current price first: the defaults are Hetzner `ccx33` and Google Cloud `e2-standard-4`. Answer no to only check prerequisites.

   Without a terminal, every answer must be a flag, for example `offbox setup --provider hetzner --action create` or `offbox setup --provider ssh --action check`. See `offbox setup --help`.

5. **First run.** In any Git worktree:

   ```sh
   cd your-repo
   offbox up
   offbox exec -- pytest
   offbox ports
   ```

   `up` creates the worktree's environment, starts source sync, installs dependencies and starts its Compose stack. `exec` runs a command after `--` in the environment with its real exit code. `ports` lists the `localhost` forwards. `offbox doctor` and `offbox status` explain problems.

6. **Clean up.** Remove a worktree's environment, and destroy the host to stop its charges:

   ```sh
   offbox rm --yes
   offbox host list
   offbox host destroy main --yes
   ```

   `offbox stop` keeps the environment's data and does not stop cloud billing.

If you already use another Mutagen version, offbox reports it instead of downloading a second one, because all Mutagen binaries of a user share one daemon. To switch to the offbox download, run `mutagen daemon stop`, remove the other Mutagen from `PATH` and rerun `offbox setup`.

## Windows (WSL2)

Offbox does not run in native PowerShell, cmd or Git Bash. Use WSL2:

1. In PowerShell opened as Administrator, run `wsl --install`, then restart Windows.
2. Open **Ubuntu** from the Start menu and create your Linux user.
3. Follow the [macOS and Linux](#macos-and-linux) steps inside Ubuntu.
4. Keep repositories in the Linux filesystem, for example `~/src/project`, not under `/mnt/c`. File watching and sync on `/mnt/c` are slow and unreliable.
5. Open those repositories with the VS Code WSL extension or JetBrains WSL support, and run coding agents inside WSL so they use the same files and the `offbox` command.

## Hetzner

1. Sign in to the [Hetzner Console](https://console.hetzner.com/). New accounts may need identity verification before servers can be created.
2. Create a project, or select an existing one.
3. Open **Security** → **API tokens** and choose **Generate API token**.
4. Give it a description, select **Read & Write**, and copy the token. It is shown only once.
5. Provide it to offbox in one of two ways:

   - In your shell profile (`~/.zshrc`, `~/.bashrc`):

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

   `HCLOUD_TOKEN` takes precedence over the file.

6. Run `offbox setup --provider hetzner` and approve creation. Servers bill until `offbox host destroy`; see [Hetzner pricing](https://www.hetzner.com/cloud/). More detail: [providers](providers.md#hetzner-cloud).

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

5. Tell offbox which project to use, for example in your shell profile:

   ```sh
   export GOOGLE_PROJECT=your-project-id
   ```

6. Make sure your identity has **Compute Instance Admin (v1)**, **Compute Network Admin** and **Compute Security Admin** on the project, or equivalent narrower permissions.
7. Run `offbox setup --provider gcp` and approve creation. VMs, disks and external IPs bill until `offbox host destroy`. More detail: [providers](providers.md#google-cloud).

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

2. On your laptop, register and bootstrap the server:

   ```sh
   offbox setup --provider ssh --action init --target user@host --host-key-fingerprint SHA256:...
   ```

   Offbox scans the server's key and stops if it does not match. Add `--port` for a nondefault SSH port and `--ssh-key` for a specific key. On a terminal you can omit the fingerprint and confirm the displayed one instead.

## Troubleshooting

Run `offbox doctor` for local prerequisites and `offbox status` inside a worktree. Every error ends with a `Fix:` line; [troubleshooting](troubleshooting.md) lists them by area.
