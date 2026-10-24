# Configuration

Offbox reads a project `offbox.toml` at the Git worktree root, user `config.toml`, and an optional protected `credentials.toml`. Missing files use defaults. Unknown keys and invalid TOML fail with the filename/key and a `Fix:`. Offbox does not rewrite the user configuration when changing the active host.

## Locations

| Content | Default path | XDG override |
| --- | --- | --- |
| User configuration | `~/.config/offbox/config.toml` | `$XDG_CONFIG_HOME/offbox/config.toml` |
| Credentials | `~/.config/offbox/credentials.toml` | `$XDG_CONFIG_HOME/offbox/credentials.toml` |
| Custom providers | `~/.config/offbox/providers/` | `$XDG_CONFIG_HOME/offbox/providers/` |
| State and provider workspaces | `~/.local/state/offbox/` | `$XDG_STATE_HOME/offbox/` |
| Logs | `~/.local/state/offbox/logs/` | `$XDG_STATE_HOME/offbox/logs/` |
| Cached tools/provider plugins | `~/.cache/offbox/` | `$XDG_CACHE_HOME/offbox/` |
| SSH control sockets | `/tmp/offbox-<uid>/` | Fixed short private path |

## Project file

```toml
[env]
cpus = 2
memory = "4g"
shm_size = "2g"

[install]
command = "uv sync --frozen"
when = ["uv.lock"]

[stack]
up = "docker compose up -d --wait"

[ports]
forward = [8000]
auto = true

[sync]
ignore = [".env", "large-fixtures/"]

[exec]
env = ["CI", "NPM_TOKEN"]
```

| Key | Default | Behavior |
| --- | --- | --- |
| `env.image` | Package `DEFAULT_IMAGE` | Registry image for the environment. Omit to use the packaged universal image. |
| `env.dockerfile` | Unset | Alternative Dockerfile inside this worktree; mutually exclusive with `image`. Source is flushed before the host builds it. |
| `env.cpus` | `max(1, host CPUs - 1)` | Finite CPU limit, from 0.01 through the host CPU count. |
| `env.memory` | 75% of host memory | Positive Docker byte size; at least 6 MiB. Example: `"8g"`. |
| `env.shm_size` | `"2g"` | Docker shared-memory size, useful for browsers. |
| `install.command` | Unset | Shell command run as `dev` in `/work`; no command means no dependency-install action. |
| `install.when` | `[]` | Ordered relative files controlling automatic reinstall before `exec`. Missing files count as empty; the first install still runs. |
| `stack.up` | Root Compose fallback | Explicit shell command, or `docker compose up -d` if a recognized root Compose file exists. |
| `stack.down` | Root Compose fallback | Declares the corresponding down command; `stop`/`rm` stop or remove the whole environment directly and do not run an extra teardown action. |
| `ports.forward` | `[]` | Explicit remote TCP ports, each 1–65535. |
| `ports.auto` | `true` | Add published TCP ports from a root Compose stack; deduplicate IPv4/IPv6 entries. |
| `sync.ignore` | `[]` additional patterns | Add Mutagen ignore patterns; each value remains one pattern, including commas/quotes. Mandatory Git exclusion is applied last. |
| `exec.env` | `[]` | Names copied from the local environment when set; values are not stored in state. |

The default image has one definition, `DEFAULT_IMAGE` in [offbox/__init__.py](../src/offbox/__init__.py). Custom images must preserve the ready-file, `dev` user, Docker and `offbox-run` contracts; see [the universal image](universal-image.md).

Recognized root Compose filenames are `compose.yaml`, `compose.yml`, `docker-compose.yaml` and `docker-compose.yml`. Automatic setup runs only `mise install` when `.tool-versions` or `mise.toml` exists, the declared install command, and the declared/default stack command. `up --no-stack` skips stack startup. There is no additional project detection.

`up` explicitly runs the install stage. Later `exec` calls rerun it only when its stored ordered hash changes. The marker lives outside source at `/srv/offbox/envs/<env>/.offbox/install.sha256`; see [the hash and coordination contract](agents.md#dependency-installation). `exec --no-sync` uses the already-synced remote files without a hidden flush.

A custom Dockerfile image is cached by project-path and Dockerfile-content hashes. Other build-context changes alone do not invalidate this cache. An existing container retains its image until `up --fresh`; that command replaces its Docker data volume while keeping synchronized code, so it asks for confirmation on a terminal and needs `--yes` otherwise.

### Sync and environment variables

Default ignores cover dependency/cache/build directories: `node_modules`, `.venv`, `venv`, `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `dist`, `build`, `.next`, `.nuxt`, `target`, `.terraform`, `.DS_Store`, and mandatory `.git`. Project patterns retain their order and repetitions so later patterns can override earlier ones. Mandatory `.git` is moved to the end once. Offbox also enables VCS-directory ignoring. Linked worktrees' regular `.git` files are excluded too, including when project patterns use negation. Offbox sessions ignore unrelated global Mutagen defaults.

Patterns use Mutagen's default ignore syntax. A pattern without a `/`, such as `build` or `target`, matches that name at any depth, so the defaults also exclude source directories like `src/build/` or `packages/api/dist/`. A pattern with a `/` is relative to the worktree root, and a leading `!` re-includes a path matched by an earlier pattern. Project patterns are applied after the defaults, so a nested source directory can be re-included:

```toml
[sync]
ignore = ["!src/build", "!tools/target"]
```

A negated pattern re-includes only the named path; files inside a directory that is still ignored stay ignored. Use `/build` to ignore a root-level directory only.

`.gitignore` is not a secret filter. Add `.env` or other sensitive paths explicitly to `sync.ignore` when they must stay local. Files created remotely, such as lockfiles and formatter output, sync back under two-way-safe conflict handling.

`exec --env NAME=value` overrides configured names. Automatic install receives these scoped values too; `up` stages receive configured `exec.env` values. TTY execution additionally forwards `TERM`, `COLORTERM` and `LANG` when set. Offbox does not forward the whole local environment. Known secret-named values are redacted from diagnostics; command stdout/stderr remain the command's own streams.

## User host presets

```toml
default_host = "main"

[hosts.main]
provider = "hetzner"
size = "ccx33"
region = "fsn1"
image = "ubuntu-24.04"
ssh_key = "~/.ssh/id_ed25519"

```

| Key | Default | Meaning |
| --- | --- | --- |
| `default_host` | Unset | Fallback when state has no active host. `host use NAME` changes state and takes precedence. |
| `hosts` | `{}` | Named preset tables. `host create` flags take precedence over its preset values. |
| `hosts.NAME.provider` | `"hetzner"` | Built-in/custom provider name. |
| `hosts.NAME.size` | Provider default | Cloud machine size; Hetzner currently defaults to `ccx33`. |
| `hosts.NAME.region` | Provider default | Cloud location; Hetzner currently defaults to `fsn1`. |
| `hosts.NAME.image` | Provider default | Cloud operating-system image, distinct from `env.image`; Hetzner defaults to `ubuntu-24.04`. |
| `hosts.NAME.address` | Unset | Accepted existing-host address field; `host init` requires its target explicitly and does not read this preset. |
| `hosts.NAME.port` | `22` | Accepted existing-host port field; pass `--port` explicitly to `host init` when different from 22. |
| `hosts.NAME.ssh_key` | `~/.ssh/id_ed25519`, or `~/.ssh/offbox_ed25519` when only that key exists | Local private-key path used by `host create`; `host init` uses the same default. It must be usable without an interactive passphrase prompt, or loaded into an agent. |

`host init` does not load host presets. Pass `USER@ADDRESS`, any nondefault `--port`, and `--ssh-key` explicitly, for example `offbox host init dev@192.0.2.10 --name home --port 2222 --ssh-key ~/.ssh/home --host-key-fingerprint SHA256:...`.

A registered environment always uses its recorded host. Changing the default does not move it. Remove that environment before selecting another host with `up --host NAME`.

## Credentials

Prefer a local `HCLOUD_TOKEN` environment variable. Alternatively, create the credentials file at the path above:

```toml
[hetzner]
token = "replace-locally"
```

Set its permissions to `0600`. The environment takes precedence. Never commit the file or put cloud-token values in project configuration/provider defaults. Other providers declare required environment names in their manifests; see [writing a provider](writing-a-provider.md).
