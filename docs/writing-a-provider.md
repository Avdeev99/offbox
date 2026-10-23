# Writing a provider

Create a folder at `$XDG_CONFIG_HOME/offbox/providers/<name>/` (default `~/.config/offbox/providers/<name>/`). User providers override built-in providers with the same name. Offbox v0.1 supports only the `tofu` and `none` drivers; `none` registers existing SSH servers.

A `provider.toml` manifest declares the name, description, driver, required credential environment variable names and machine defaults:

```toml
name = "example"
description = "An Ubuntu 24.04 VM"
driver = "tofu"
credentials_env = ["EXAMPLE_TOKEN"]

[defaults]
size = "example-size"
region = "example-region"
image = "ubuntu-24.04"
```

Do not put credential values in the manifest. Missing required credentials fail before provisioning. Hetzner additionally supports the protected `credentials.toml` file; other provider credentials come from the environment.

The provider must declare these Terraform variables in `variables.tf`:

| Variable | Type | Content |
| --- | --- | --- |
| `name` | `string` | Server name such as `offbox-main` |
| `size` | `string` | Provider-specific machine size |
| `region` | `string` | Provider-specific location |
| `image` | `string` | Ubuntu 24.04 image identifier |
| `ssh_public_key` | `string` | User's OpenSSH public key |
| `user_data` | `string`, sensitive | Complete rendered cloud-init configuration |
| `labels` | `map(string)` | Includes `offbox = "1"` and `offbox-host = <name>` |

Declare outputs `ip` (public IPv4 string) and `ssh_user` (`"offbox"`). Offbox parses the standard `tofu output -json` objects containing `value` fields. Use `user_data` directly on the server resource, apply labels to every owned billable resource, and let cloud-init install the supplied public key. Do not create a globally unique cloud SSH-key resource.

Pin the cloud provider to a supported minor release in `versions.tf`. Credentials must be read from the provider's environment. Mark `user_data` sensitive to reduce terminal exposure; this does not encrypt Terraform state.

The `tofu` driver uses OpenTofu first, Terraform second, and verified cached OpenTofu when neither is installed. It copies the provider folder into the host's state directory and writes `offbox.auto.tfvars.json` there. Every command runs in that workspace with:

```text
TF_IN_AUTOMATION=1
TF_INPUT=0
TF_CLI_ARGS=-no-color
TF_PLUGIN_CACHE_DIR=<offbox cache>/tofu-plugins
```

Creation runs `init -input=false`, `apply -auto-approve -input=false` and `output -json` in that order. Destroy runs `destroy -auto-approve -input=false` against the saved copy. The copied workspace is retained after failure and removed only after successful teardown.

Run `terraform fmt -check` and `terraform init -backend=false` followed by `terraform validate` in a disposable copy to check provider syntax and schemas. These commands do not create cloud resources. Keep actual provisioning out of the unit test suite.
