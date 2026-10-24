# Host providers

Offbox includes `hetzner` and `gcp` for a new Ubuntu 24.04 VM, and `ssh` for an existing Ubuntu 24.04 server. Host creation stores its registry and current default in the local state directory. User configuration remains read-only and supplies named presets.

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

## Google Cloud

Enable the Compute Engine API and billing in your own project. Set the project explicitly and configure [Application Default Credentials](https://docs.cloud.google.com/docs/terraform/authentication), for example:

```sh
export GOOGLE_PROJECT="your-project-id"
gcloud auth application-default login
offbox host create gcp-main --provider gcp --size e2-standard-4 --region us-east4-c
offbox host ssh gcp-main -- docker info
offbox host destroy gcp-main --yes
```

For GCP, `--region` takes a **zone**, such as `us-east4-c`; the provider derives the subnet region. The defaults are `e2-standard-4`, `us-east4-c`, Ubuntu 24.04 amd64 and a 50 GB balanced persistent boot disk. These are billable resources; review [Compute pricing](https://cloud.google.com/products/compute/pricing), [disk pricing](https://cloud.google.com/compute/disks-image-pricing) and [network/IP pricing](https://cloud.google.com/vpc/network-pricing). The boot disk is deleted with the VM. The ephemeral external address is released with the instance.

The provisioning identity needs Compute Instance Admin (v1), Compute Network Admin and Compute Security Admin, or equivalent narrower custom permissions. Credentials stay on the client. Google's provider supports ADC, service-account impersonation, `GOOGLE_APPLICATION_CREDENTIALS`, and `GOOGLE_OAUTH_ACCESS_TOKEN`; it reads them through its normal credential chain. An access token is short-lived and must still be valid for destruction; refresh it before retrying cleanup. A normal `gcloud auth login` alone does not configure ADC. Prefer ADC or impersonation for repeat use; a private service-account key is not required.

The pinned `hashicorp/google` minor series is `>=8.7,<8.8`. Each host owns a separate VPC, one regional subnet and a targeted firewall allowing SSH and ICMP. It uses instance `user-data` for the common cloud-init bootstrap, disables OS Login for that instance and blocks project-wide SSH keys. It neither changes project-wide SSH settings nor attaches a service account to the VM. Development workloads therefore do not inherit a Google cloud identity. Organizations that enforce OS Login need a compatible custom provider; Offbox does not change organization policies.

Provider project/authentication values are environment/ADC settings, not Terraform input variables or `offbox.toml` secrets. Saved cloud-init and Terraform state still contain the generated SSH host private key; preserve the local state permissions described in [security](security.md). The copied provider workspace is retained after failed creation/destruction, just as for Hetzner.

`offbox setup --provider gcp --action create` supports the same size, zone, name and SSH-key flags. Noninteractive setup requires an explicit action and never silently creates a host. CI validates the provider schema and runs mocked Terraform contracts without cloud credentials or real resources.

## Existing SSH server

```sh
offbox host init dev@192.0.2.10 --name home --port 22 --accept-host-key
```

The connecting account needs noninteractive sudo. Offbox displays the server's Ed25519 fingerprint for confirmation on a terminal; noninteractive use requires `--accept-host-key`. Verify that fingerprint through a trusted channel. Subsequent connections use the pinned host key and the `offbox` account.

Existing-host bootstrap sets `OFFBOX_HARDEN=0`. It installs the offbox account, passwordless sudo, Docker and the required directories/network. It preserves the owner's account, SSH settings, UFW settings and existing security-service configuration. UID 1000 must be available or already belong to `offbox`; a conflicting account produces an actionable error before changes. Existing conflicting Docker packages or a different Docker CE major also require a manual migration before bootstrap.

The shared network is `10.89.0.0/16`; an existing network named `offbox` with a different subnet is rejected. Bootstrap preserves unrelated containers and volumes. Destroying a registered SSH host validates container/volume ownership labels before deletion and removes owned host directories, while retaining Docker and the `offbox` user. New Offbox networks carry `offbox.managed=1`; adopted or older unlabelled networks are preserved during destruction.

## Provider workspaces and recovery

Built-in providers ship inside the wheel. A provider folder under `$XDG_CONFIG_HOME/offbox/providers/<name>` overrides a built-in provider of the same name. See [the provider contract](writing-a-provider.md).

Before provisioning, offbox copies the selected provider into the host's state workspace and records its hash. Destroy uses this copy, so later edits to a provider do not change the teardown instructions. Failed provisioning or destroy keeps the workspace for recovery. Do not delete it while its remote resources still exist.

Validation uses deterministic unit tests, ShellCheck and Terraform schema validation. The focused Linux Docker runtime smoke uses an already-prepared SSH fixture and does not provision or bootstrap a live cloud server.
