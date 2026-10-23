# Security model

Offbox is for one user's trusted development projects on a personal VM. Each worktree runs in a privileged container with its own Docker daemon. Privileged containers and Docker-group membership confer host-level control; environment containers do not provide a security boundary against hostile code or other users.

## SSH trust and accounts

Cloud provisioning generates an Ed25519 host key locally, supplies it through cloud-init, and writes its public half to the local `known_hosts` file before the first connection. Connections require that pinned key. Existing SSH hosts use explicit fingerprint confirmation instead: verify the displayed fingerprint through a trusted channel before accepting it. Replacing a host key requires a deliberate trust update.

Cloud hosts accept key-based SSH only for `offbox`; root login, passwords and keyboard-interactive authentication are disabled. The account has passwordless sudo and Docker access. Bootstrap also enables UFW, fail2ban and unattended security upgrades. The cloud firewall permits inbound SSH and ICMP. Development ports are reached through SSH forwards rather than public container-port publishing.

Existing-host initialization preserves the owner's account, SSH and firewall settings and does not enable security services. It refuses to take over an occupied UID 1000 or automatically replace a conflicting Docker installation. Offbox still creates its own password-locked account with passwordless sudo and Docker access. Review that access before registering a shared or sensitive server.

Docker-published ports can bypass UFW. Keep project services inside the per-worktree network and use offbox's SSH forwards. See [Docker's Ubuntu firewall guidance](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations).

## Cloud metadata and local provisioning state

Cloud-init `user_data` contains the generated private SSH host key. On Hetzner, cloud metadata is available at `169.254.169.254`; processes on the VM, including environment containers, can read that data. The private host key is therefore visible to the VM's user and their workloads. This is accepted for the single-user trust model. Do not run untrusted workloads or reuse that host key on another machine.

Google Cloud also serves instance `user-data` through its metadata service, so the same host-key visibility applies. The GCP provider attaches no service account to the VM, blocks project SSH keys and uses instance-scoped SSH configuration; it does not grant workloads the local provisioning identity. Google authentication stays in local ADC or provider environment variables. Known Google credential-variable values are redacted from Offbox diagnostics.

Offbox deletes the temporary generated private-key file after the host becomes ready. The copied tfvars, OpenTofu/Terraform state and cloud-init data can still retain `user_data` and its private host key. Treat the host workspace and backups as sensitive. The local state directory has restricted permissions; preserve those restrictions when copying it. Terraform's `sensitive` marker hides values in ordinary output but does not encrypt state.

Provider API tokens come from environment variables or a `credentials.toml` file no more permissive than `0600`. They are passed through process environments and never intentionally serialized into offbox configuration, tfvars or state. Logs redact known secret environment values and credentials. Do not place unrelated secrets in provider defaults, command arguments or user-data templates.

Code synchronization applies mandatory `.git` exclusion after project ignore patterns and also ignores VCS directories; ordinary repositories and linked-worktree pointer files stay on the laptop. Local editing and Git history remain local. Source files and any project secrets included in the synchronized worktree are accessible to the VM and its privileged workloads. Destroying a cloud host removes its environments and their remote data; keep important files on the laptop or in independent backups.

Cloud credentials are not forwarded to environments by default. Explicit `[exec].env`/`--env` values and synchronized files can still carry secrets you choose to send. `.env` files are synchronized unless explicitly ignored; `.gitignore` does not replace `sync.ignore`. Client state and copied provider workspaces remain local.
