# Host providers

Offbox includes `hetzner` for a new Ubuntu 24.04 VM and `ssh` for an existing Ubuntu 24.04 server. Host creation stores its registry and current default in the local state directory. User configuration remains read-only and supplies named presets.

## Hetzner Cloud

Set `HCLOUD_TOKEN` in the environment, or put `hetzner.token` in the XDG configuration directory's `offbox/credentials.toml` with permissions `0600`. Tokens are passed to the provider through its environment. They are never written to the project configuration, tfvars, registry or logs.

```sh
offbox host create main --provider hetzner --size ccx33 --region fsn1
offbox host ssh main -- docker info
offbox host destroy main --yes
```

These commands create and delete a billable server. A stopped Hetzner server is still billed: use `offbox host destroy` to stop server charges. Consult [Hetzner pricing](https://www.hetzner.com/cloud/) for current prices and review any other resources in your project separately.

The built-in provider pins the `hetznercloud/hcloud` provider to `>=1.68,<1.69`. It creates a labelled server and firewall, permits inbound SSH and ICMP, and injects the user's public key through cloud-init. It creates no account-level SSH-key resource, so the same public key can bootstrap several hosts. Cloud bootstrap disables root/password SSH access, enables UFW, fail2ban and security upgrades, and installs Docker Engine major 29 from Docker's official apt repository.

The bootstrap requires `IMAGE PUBLIC_KEY` arguments and `OFFBOX_VERSION` in its environment. `OFFBOX_HARDEN=1` selects cloud hardening. Bootstrap pulls the package's `DEFAULT_IMAGE`. A project's `env.image` or `env.dockerfile` is applied later by `offbox up`; the host preset's `image` selects the cloud operating system.

## Existing SSH server

```sh
offbox host init dev@192.0.2.10 --name home --port 22 --accept-host-key
```

The connecting account needs noninteractive sudo. Offbox displays the server's Ed25519 fingerprint for confirmation on a terminal; noninteractive use requires `--accept-host-key`. Verify that fingerprint through a trusted channel. Subsequent connections use the pinned host key and the `offbox` account.

Existing-host bootstrap sets `OFFBOX_HARDEN=0`. It installs the offbox account, passwordless sudo, Docker and the required directories/network. It preserves the owner's account, SSH settings, UFW settings and existing security-service configuration. UID 1000 must be available or already belong to `offbox`; a conflicting account produces an actionable error before changes. Existing conflicting Docker packages or a different Docker CE major also require a manual migration before bootstrap.

The shared network is `10.89.0.0/16`; an existing network named `offbox` with a different subnet is rejected. Bootstrap preserves unrelated containers and volumes. Destroying a registered SSH host removes offbox environments and owned host directories but retains Docker and the `offbox` user.

## Provider workspaces and recovery

Built-in providers ship inside the wheel. A provider folder under `$XDG_CONFIG_HOME/offbox/providers/<name>` overrides a built-in provider of the same name. See [the provider contract](writing-a-provider.md).

Before provisioning, offbox copies the selected provider into the host's state workspace and records its hash. Destroy uses this copy, so later edits to a provider do not change the teardown instructions. Failed provisioning or destroy keeps the workspace for recovery. Do not delete it while its remote resources still exist.

Validation uses deterministic unit tests, ShellCheck and Terraform schema validation. The focused Linux Docker runtime smoke uses an already-prepared SSH fixture and does not provision or bootstrap a live cloud server.
