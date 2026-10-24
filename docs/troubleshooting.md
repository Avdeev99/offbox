# Troubleshooting

Start with `offbox status` for this worktree and `offbox doctor` for local prerequisites. Offbox sends diagnostics to stderr and includes a `Fix:` instruction. A remote command can return its own nonzero code without an Offbox failure; inspect its output and use `status --json` when the distinction matters.

Daily logs live under `$XDG_STATE_HOME/offbox/logs` (default `~/.local/state/offbox/logs`). `offbox logs` selects today’s records for the current environment. Use `-v` to echo tool commands with known secret values redacted. Review logs before sharing them: project stdout/stderr and arbitrary file contents can contain secrets.

For slow startup or execution, use `offbox --timings up` or `offbox --timings exec -- COMMAND` to separate sync, dependency checks, container/stack startup, forwarding and command time. The summary goes to stderr and excludes CLI startup. A slow no-op can reflect several sequential SSH operations; see the [measured Hetzner baseline](benchmarks/2026-10-23-hetzner.md).

## Recovery instruction reference

Each entry below corresponds to a recovery instruction Offbox can print. `<host>`, `<path>`, `<session>` and similar placeholders stand for the concrete value in your diagnostic. Instructions shared by several errors appear once. Follow the actual diagnostic’s values. The instruction describes the next action; the preceding error message supplies its cause.

## Command usage

Use `offbox --help` or the command’s `--help` to inspect flags. Destructive operations need an explicit `--yes` without a terminal. A command after `exec --` has its own exit status; first distinguish that status from an Offbox diagnostic.

- `Fix: Restart offbox.`
- `Fix: Run offbox gc --dry-run to review, then offbox gc --yes.`
- `Fix: Run offbox rm --yes to confirm removal.`
- `Fix: Run offbox shell in an interactive terminal.`
- `Fix: Run the command again when ready.`
- `Fix: Choose one TTY mode.`
- `Fix: Run offbox exec -- COMMAND [ARGS...].`
- `Fix: Create or initialise the host before selecting it.`
- `Fix: Pass a command after -- for noninteractive SSH.`
- `Fix: Run offbox host destroy <host> --yes.`
- `Fix: Run offbox host destroy <host> to clean up the pending host.`
- `Fix: Run offbox logs --stack SERVICE.`
- `Fix: Run offbox --help for valid arguments.`
- `Fix: Check XDG_STATE_HOME permissions and available disk space.`
- `Fix: Check the log and retry.`
- `Fix: Check the log and report this error.`

## Configuration, state and paths

Correct the named file or key, then retry. Keep provider workspaces and state until hosts are destroyed. The unsafe SSH-control-directory diagnostic refers to the private short control path, not the host’s provider workspace. Do not erase cloud state as a connectivity workaround.

- `Fix: Check the host CPU and memory information.`
- `Fix: Correct <path>; keep permissions at 0600.`
- `Fix: Correct these keys in <path>.`
- `Fix: Fix the TOML syntax and permissions in <path>.`
- `Fix: Set HCLOUD_TOKEN or hetzner.token in credentials.toml (mode 0600).`
- `Fix: chmod 600 <path>`
- `Fix: Check free space and state directory permissions.`
- `Fix: Check the state directory permissions.`
- `Fix: Repair or restore the state file before continuing.`
- `Fix: Remove the directory and retry.`

## Tools and local processes

Check `offbox doctor` first. Mutagen is a separate installation. OpenTofu downloads are verified before execution; install a supported binary on PATH if downloads are unavailable. Inspect an interrupted provisioning operation before retrying creation.

- `Fix: Check your network or install OpenTofu >=1.8 on PATH.`
- `Fix: Clear <directory> and retry, or install OpenTofu on PATH.`
- `Fix: Install <name>.`
- `Fix: Install Mutagen 0.18.x: brew install mutagen-io/mutagen/mutagen (macOS), or https://github.com/mutagen-io/mutagen/releases.`
- `Fix: Install OpenSSH >=8.0.`
- `Fix: Install OpenTofu >=1.8 on PATH.`
- `Fix: Install OpenTofu >=1.8 or Terraform >=1.6.`
- `Fix: Install git >=2.30.`
- `Fix: Remove <archive> and retry, or install OpenTofu on PATH.`
- `Fix: Check connectivity and try again.`
- `Fix: Check subprocess pipe support and retry.`
- `Fix: Check the local process and retry.`
- `Fix: Inspect and stop the local SSH process manually.`
- `Fix: Install <tool> and check its permissions.`
- `Fix: Run offbox host list and destroy the interrupted host.`
- `Fix: Stop the local SSH process and inspect the remote command.`
- `Fix: Supply a command to run.`
- `Fix: Check connectivity with offbox host ssh <selected>.`
- `Fix: Correct the reported problem and retry offbox doctor.`
- `Fix: Create the host or choose a registered host.`
- `Fix: Generate <key> or load it with ssh-add <key>.`
- `Fix: Check the command output and try again.`

## Hosts and providers

Keep a failed host’s copied provider workspace so `host destroy` can clean up its billable resources. A stopped cloud server can still bill. Verify a changed host fingerprint independently before accepting it; replacing a pinned key without verifying identity defeats SSH host checking. Public-key regeneration commands write the `.pub` sidecar, not a replacement private key.

- `Fix: Check state directory permissions.`
- `Fix: Correct the SSH public key file permissions and encoding.`
- `Fix: Generate a valid SSH key with ssh-keygen.`
- `Fix: Generate its .pub file with ssh-keygen -y.`
- `Fix: Generate the key or load it with ssh-add <key>.`
- `Fix: Regenerate <path> and load the matching key with ssh-add <key>.`
- `Fix: Regenerate <sidecar> with ssh-keygen -y -f <key>.`
- `Fix: Run offbox host create or offbox host use NAME.`
- `Fix: Run offbox host list or create this host.`
- `Fix: Wait for that operation to finish, then retry.`
- `Fix: Check state directory permissions and ssh-keygen.`
- `Fix: Inspect the existing directory before removing it or choose another host name.`
- `Fix: Run offbox host destroy <host> to clean up; its provider workspace was retained.`
- `Fix: Run offbox host destroy <host> or choose another name.`
- `Fix: Run offbox host init USER@ADDRESS.`
- `Fix: Set provider defaults or a matching host preset.`
- `Fix: Check the SSH address and its advertised host keys.`
- `Fix: Check the server's ed25519 key.`
- `Fix: Correct the reported problem and rerun the same host init command; its registration was retained.`
- `Fix: Correct the ssh provider manifest.`
- `Fix: Inspect the existing directory or choose another name.`
- `Fix: Review the host fingerprint and pass --accept-host-key.`
- `Fix: Verify the fingerprint and rerun offbox host init.`
- `Fix: Verify the server identity before changing its stored host key.`
- `Fix: Install OpenTofu or Terraform and retry.`
- `Fix: Remove the obsolete local host directory manually.`
- `Fix: Repair the environment state before destroying its host.`
- `Fix: Restore the host's saved provider workspace before destroying it.`
- `Fix: Run host destroy again after other operations finish.`
- `Fix: Run offbox host ssh <name>; inspect <log-path>.`
- `Fix: Check the provider manifest syntax, fields and permissions.`
- `Fix: Correct the provider manifest name.`
- `Fix: Install a provider in <custom>.`
- `Fix: Replace provider symlinks with regular files before creating a host.`
- `Fix: Set <name> before retrying.`
- `Fix: Use 1–32 lowercase letters, digits or hyphens, starting with a letter.`
- `Fix: Use valid environment variable names.`
- `Fix: Check provider outputs; destroy the host before retrying.`
- `Fix: Inspect the host Docker daemon before retrying.`
- `Fix: Inspect the host Docker volumes before retrying.`
- `Fix: Check OpenSSH multiplexing output and retry.`
- `Fix: Check SSH configuration and control socket permissions for offbox-<name>, then retry.`
- `Fix: Check SSH configuration permissions.`
- `Fix: Check directory permissions and free disk space.`
- `Fix: Choose a path without control characters.`
- `Fix: Correct its UTF-8 encoding.`
- `Fix: Run ssh -O exit offbox-<name>, then retry cleanup.`
- `Fix: Use a hostname or IP address without SSH options or whitespace.`
- `Fix: Use a valid SSH username.`
- `Fix: Use an ed25519 host key and retry.`

## Environment lifecycle

Run commands inside the intended Git worktree. Ownership mismatches and inaccessible worktree paths stop deletion deliberately: inspect labels and state before retrying. `up --fresh` discards environment Docker data while retaining source; `rm` also removes remote source. Back up valuable data first.

- `Fix: Repair the environment state before continuing.`
- `Fix: cd into a git worktree`
- `Fix: Check Dockerfile permissions and retry.`
- `Fix: Check its permissions and run offbox up.`
- `Fix: Check the host Docker daemon and offbox logs.`
- `Fix: Check the host Docker daemon.`
- `Fix: Create the configured Dockerfile before running offbox up.`
- `Fix: Finish host initialisation before running an environment.`
- `Fix: Inspect its labels or choose a different worktree path.`
- `Fix: Inspect its ownership labels before running GC.`
- `Fix: Inspect offbox logs and run offbox up --fresh.`
- `Fix: Inspect resource ownership before GC.`
- `Fix: Inspect state and ownership before GC.`
- `Fix: Inspect the Docker build log and retry offbox up.`
- `Fix: Inspect the Docker daemon before running GC.`
- `Fix: Inspect the Docker volumes before running GC.`
- `Fix: Inspect the environment ownership and paths before retrying.`
- `Fix: Provide a valid environment configuration.`
- `Fix: Remove it before selecting a different host.`
- `Fix: Restore access to the worktree before running GC.`
- `Fix: Restore its host registration before running GC.`
- `Fix: Restore the registered host before managing this environment.`
- `Fix: Retry offbox up.`
- `Fix: Review a new GC plan before removing it.`
- `Fix: Review offbox list and run cleanup again.`
- `Fix: Review the GC plan and retry.`
- `Fix: Run offbox host ssh <host> and check connectivity.`
- `Fix: Run offbox list and retry up.`
- `Fix: Run offbox list and retry.`
- `Fix: Run offbox list before retrying.`
- `Fix: Run offbox up --fresh after source sync completes.`
- `Fix: Run offbox up again.`
- `Fix: Run offbox up before opening a shell.`
- `Fix: Run offbox up in this worktree.`
- `Fix: Set env.<field> between <minimum> and <9223372036854775807> bytes.`
- `Fix: Set env.<field> to a finite Docker byte size.`
- `Fix: Set env.<field> to a positive Docker byte size, for example 8g.`
- `Fix: Set env.cpus between 0.01 and the host CPU count.`
- `Fix: Set env.dockerfile to a file inside the worktree.`
- `Fix: Set env.image to a prebuilt image until source sync is available.`
- `Fix: Set env.image to a valid prebuilt Docker image reference.`
- `Fix: Use a different worktree directory name.`
- `Fix: Use a regular Dockerfile path inside the worktree.`
- `Fix: offbox up`

## Execution and dependency installation

Install commands and declared hash files are explicit configuration. A failed installation retains the environment and does not mark installation successful. Inspect a lock’s owner before removing it. When signal cleanup is unconfirmed, follow the printed command with its exact host, container and execution ID.

- `Fix: Check local state directory permissions and retry.`
- `Fix: Check the host sha256sum command and retry.`
- `Fix: Configure exec.env as environment variable names.`
- `Fix: Remove the conflicting install lock and retry.`
- `Fix: Remove the host .offbox/install.sha256 marker and retry.`
- `Fix: Restore an owned directory at <directory>.`
- `Fix: Retry after the current installation finishes.`
- `Fix: Retry offbox up or the requested command when ready.`
- `Fix: Retry the command.`
- `Fix: Run offbox exec inside the worktree.`
- `Fix: Run offbox list and resolve conflicting resources.`
- `Fix: Run the command inside this worktree.`
- `Fix: Use --env NAME=value with a valid environment variable name.`
- `Fix: Use relative file paths inside the worktree without dot components.`
- `Fix: Use valid environment variable names and values.`
- `Fix: offbox status`
- `Fix: inspect and stop the local SSH process.`
- `Fix: offbox host ssh <host> -- docker exec <container> offbox-run --signal KILL <execution-id>`

## Source synchronization

Resolve conflicting files locally, then reset the exact session identifier shown. Inspect `mutagen sync list` before terminating a session; names alone are not proof of ownership. Offbox-managed sessions disable global configuration and apply mandatory Git exclusion after project patterns.

- `Fix: Check host connectivity with offbox host ssh, then run offbox up.`
- `Fix: Check mutagen sync list and retry uninstall.`
- `Fix: Check sync conflicts and host connectivity, then run offbox up.`
- `Fix: Check the Mutagen daemon and SSH connectivity, then run offbox up.`
- `Fix: Check the Mutagen daemon and install a supported Mutagen 0.18.x version, then run offbox up.`
- `Fix: Correct sync access problems (<paths>) and run offbox up.`
- `Fix: Inspect mutagen sync list and remove duplicate managed sessions.`
- `Fix: Inspect mutagen sync list before changing this environment.`
- `Fix: Inspect mutagen sync list; terminate <session> explicitly only if it belongs to offbox.`
- `Fix: Install Mutagen 0.18.x and inspect the sync daemon.`
- `Fix: Resolve sync conflicts locally (<paths>), then run mutagen sync reset <session> and offbox up.`
- `Fix: Restore the worktree root, then run mutagen sync reset <session> and offbox up.`
- `Fix: Run mutagen sync terminate <session>, then offbox up to apply the new configuration.`
- `Fix: Run offbox up to resume source sync.`
- `Fix: Set sync.ignore to nonempty, single-line patterns without NUL characters.`
- `Fix: Wait for other sync operations and retry offbox up.`

## Ports and SSH masters

Do not terminate an unrelated local service just because it holds a preferred port. Identify the listener first, or use another mapping. Closing an affected Offbox SSH master also closes its other forwards; restore them with `offbox ports` in the affected worktrees. A failed swap reports whether its rollback completed.

- `Fix: Check SSH connectivity and retry offbox ports.`
- `Fix: Check docker compose ps --format json inside the environment.`
- `Fix: Check offbox host ssh <host> and retry offbox ports.`
- `Fix: Check offbox host ssh <host> and retry.`
- `Fix: Check the offbox state directory permissions.`
- `Fix: Choose a port between 1 and 65535.`
- `Fix: Close the offbox SSH master and run offbox ports again.`
- `Fix: Close the offbox SSH master and run offbox ports to restore its forwards.`
- `Fix: Finish host initialization before restoring ports.`
- `Fix: Inspect the offbox network and container ownership before retrying.`
- `Fix: Local port <local-port> could not be forwarded; stop its competing listener or retry offbox ports.`
- `Fix: Repair duplicate local port mappings before swapping.`
- `Fix: Repair duplicate port mappings before retrying.`
- `Fix: Repair the environment's port state before continuing.`
- `Fix: Restore its host registration before swapping ports.`
- `Fix: Retry offbox ports; the previous listener could not be cancelled safely.`
- `Fix: Run ssh -O exit offbox-<host> for each affected host, then run offbox ports in the affected worktrees before retrying --swap.`
- `Fix: Run offbox ports after configuring or publishing that port.`
- `Fix: Run offbox ports again after checking SSH connectivity.`
- `Fix: Run offbox ports again after the host operation finishes.`
- `Fix: Run offbox ports again to reconcile the listener.`
- `Fix: Run offbox ports and retry the swap.`
- `Fix: Run offbox ports in both worktrees and retry the swap.`
- `Fix: Run offbox ports in both worktrees before retrying.`
- `Fix: Run offbox ports in both worktrees to recover the listeners.`
- `Fix: Run offbox ports in the affected worktree.`
- `Fix: Run offbox ports in the affected worktrees before retrying --swap.`
- `Fix: Run offbox up and check the environment's Docker/Compose configuration.`
- `Fix: Run offbox up before restoring ports.`
- `Fix: Run offbox up in the correct worktree.`
- `Fix: Stop a competing local listener or remove unused offbox port mappings.`
- `Fix: Stop that local listener before retrying the swap.`
- `Fix: Wait for that operation and retry offbox ports.`

## Health reports

`offbox status --json` exits 3 for errors. Warnings, such as an unavailable registry update check or aggregate memory overcommit, do not establish that the image is current. Disk warnings start at 80%; errors at 95%. Status restores forwards but does not flush source or run installation.

- `Fix: Check df -P /srv/offbox on the host.`
- `Fix: Check docker manifest inspect --verbose for the configured image.`
- `Fix: Check the image registry and retry.`
- `Fix: Correct offbox.toml.`
- `Fix: Inspect offbox list and the container ownership labels.`
- `Fix: Inspect offbox logs and retry.`
- `Fix: Inspect the host Docker daemon and running environment limits.`
- `Fix: Reinitialize the host with offbox host init.`
- `Fix: Run offbox gc or offbox rm in unused worktrees.`
- `Fix: Run offbox logs --stack and offbox up.`
- `Fix: Run offbox up --fresh.`
- `Fix: Run offbox up and inspect offbox logs if readiness fails.`
- `Fix: Run offbox up to build the configured Dockerfile.`
- `Fix: Run offbox up to create source sync.`
- `Fix: Run offbox up.`
- `Fix: Stop unused environments or reduce env.memory and run offbox up --fresh.`

## Logs, setup and uninstall

`logs --stack [SERVICE]` reads Compose output without running setup. Noninteractive setup needs an explicit provider/action. Uninstall removes local runtime records; it does not delete remote hosts or stop their billing. Destroy hosts first when you want them gone, and preserve custom providers and credentials under the configuration directory.

- `Fix: Run offbox status and check host connectivity.`
- `Fix: Check log permissions and encoding.`
- `Fix: Inspect offbox list and the container labels.`
- `Fix: Pass a Compose service name after --stack.`
- `Fix: Run offbox up before reading stack logs.`
- `Fix: Pass --action check, --action create or --action init.`
- `Fix: Pass --provider hetzner, --provider gcp or --provider ssh.`
- `Fix: Pass --provider ssh --action init --target USER@ADDRESS --accept-host-key.`
- `Fix: Run offbox doctor and follow its Fix lines.`
- `Fix: Pass --provider hetzner, --provider gcp or --provider ssh with an explicit --action.`
- `Fix: Set GOOGLE_PROJECT before retrying.`
- `Fix: Use --provider ssh --action init --target USER@ADDRESS.`
- `Fix: Use --provider ssh --action init.`
- `Fix: Check directory permissions and rerun offbox uninstall --yes.`
- `Fix: Check local directory permissions.`
- `Fix: Restore an owned directory without symlinks before uninstalling.`
- `Fix: Restore the owned config directory before uninstalling.`
- `Fix: Run offbox host destroy <host> first for each remaining host, or offbox uninstall --yes to retain the hosts.`
- `Fix: Separate offbox config, state, cache and control directories.`
- `Fix: Wait for other offbox operations to finish and retry uninstall.`

## Host bootstrap and execution shims

These instructions can arrive through SSH from the packaged host/image scripts. Bootstrap changes a machine and requires the supported OS, account ownership and Docker version; resolve a conflict before rerunning it. Run identifiers and signal arguments are normally supplied by Offbox itself. A reused PID must never be signaled as if it were the old command.

- `Fix: Run offbox host init with passwordless sudo access.`
- `Fix: Invoke bootstrap through offbox host create or host init.`
- `Fix: Use 1 for cloud hosts or 0 for an existing host.`
- `Fix: Pass the installed offbox version through OFFBOX_VERSION.`
- `Fix: Pass the configured offbox image.`
- `Fix: Select an OpenSSH public key file.`
- `Fix: Select a single OpenSSH public key.`
- `Fix: Use Ubuntu 24.04.`
- `Fix: Use an Ubuntu 24.04 host.`
- `Fix: Use a dedicated host with UID 1000 available, or move the account manually before retrying.`
- `Fix: Use a fresh cloud host without an active ubuntu session.`
- `Fix: Resolve the account configuration before retrying.`
- `Fix: Use a dedicated host or resolve the conflicting offbox account manually.`
- `Fix: Review the existing Docker installation and remove conflicting packages before retrying.`
- `Fix: Review and migrate Docker to major 29 before retrying.`
- `Fix: Select a valid OpenSSH public key.`
- `Fix: Review sshd_config before retrying bootstrap.`
- `Fix: Check Docker repository access and package availability.`
- `Fix: Remove the conflicting offbox network after reviewing its attached containers.`
- `Fix: Use a unique alphanumeric run ID without path separators.`
- `Fix: Use at most 64 characters.`
- `Fix: Pass INT, TERM or KILL and the recorded run ID.`
- `Fix: Use INT, TERM or KILL.`
- `Fix: Use terminal controls for a TTY command.`
- `Fix: Check the run status before retrying.`
- `Fix: Check the run status; do not signal a reused PID.`
- `Fix: Pass the run ID, absolute work directory and command argv.`
- `Fix: Put -- before the command argv.`
- `Fix: Use the mapped /work directory.`
- `Fix: Use a new run ID.`
- `Fix: Flush sync and check that the remote directory exists.`
- `Fix: Review the bootstrap log and retry after resolving the failure.`
- `Fix: Check the privileged container settings and dockerd log.`

## SSH loss during a command

Exit 4 can mean command completion is unknown, even if the remote command is still running. Do not retry non-idempotent work blindly. The error includes the run ID and a scoped `offbox-run --signal TERM` cleanup command; inspect the environment and use that command only when you want to stop that run. A confirmed remote exit 255 still returns 255.

A contended routine host operation waits up to 120 seconds, and forwarding waits up to 30 seconds. A timeout retains exclusive locking and asks you to retry after the competing operation finishes. Same-environment dependency installation and destructive lifecycle conflicts still fail immediately.
