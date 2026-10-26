# Configuration

Offbox has two kinds of settings. **Project settings** describe a worktree's environment: its image, dependency install, stack, ports, sync, forwarded variables, copied files and git mirror. **User settings** describe hosts and credentials. Project settings can come from five places; user settings live in `config.toml` and `credentials.toml`. Missing files use defaults. Unknown keys and invalid TOML fail with the source, the key and a `Fix:` line.

## Locations

| Content | Default path | XDG override |
| --- | --- | --- |
| Project file (usually committed) | `offbox.toml` at the Git worktree root | none |
| Local project overrides (not committed) | `offbox.local.toml` at the Git worktree root | none |
| User configuration and project defaults | `~/.config/offbox/config.toml` | `$XDG_CONFIG_HOME/offbox/config.toml` |
| Credentials | `~/.config/offbox/credentials.toml` | `$XDG_CONFIG_HOME/offbox/credentials.toml` |
| Custom providers | `~/.config/offbox/providers/` | `$XDG_CONFIG_HOME/offbox/providers/` |
| State and provider workspaces | `~/.local/state/offbox/` | `$XDG_STATE_HOME/offbox/` |
| Logs | `~/.local/state/offbox/logs/` | `$XDG_STATE_HOME/offbox/logs/` |
| Cached tools/provider plugins | `~/.cache/offbox/` | `$XDG_CACHE_HOME/offbox/` |
| SSH control sockets | `/tmp/offbox-<uid>/` | Fixed short private path |

The worktree root is the top directory of the Git checkout or linked worktree you run offbox in, the one `git rev-parse --show-toplevel` prints. A committed `offbox.toml` reaches every worktree and teammate through Git, so it is written once per repository.

## How project settings combine

Each project setting is resolved from these sources; a later source replaces an earlier one, key by key:

| Order | Source | Where | Use it for |
| --- | --- | --- | --- |
| 1 | `default` | built into offbox | working without any file |
| 2 | `user` | the `[project]` tables in `config.toml` | your preferences for every repository |
| 3 | `project` | `offbox.toml` | the team's shared setup |
| 4 | `local` | `offbox.local.toml` | your changes for one repository, not committed |
| 5 | `env` | `OFFBOX_<SECTION>_<KEY>` variables | one shell, one command or a script |

Values merge per key, not per section: `memory` from `offbox.local.toml` and `cpus` from `offbox.toml` both apply. A list from a later source replaces the earlier list rather than extending it, so `sync.ignore` in `offbox.local.toml` must repeat any project patterns it still wants.

`offbox config` prints every effective setting of the current worktree, in the order of the [settings table](#project-settings), with its value, source and variable name. Unset values print as `unset`; for `env.cpus` and `env.memory` that means the host-derived default. `offbox config --json` emits the same report for scripts, with `null` for unset values. For example, with `OFFBOX_ENV_CPUS=6` exported and gcloud credentials in the user defaults:

```text
Sources, later wins: default < user < project < local < env
  user     /home/me/.config/offbox/config.toml [project]
  project  /home/me/src/app/offbox.toml
  local    /home/me/src/app/offbox.local.toml (not present)

env.image        "ghcr.io/o-avdieiev/offbox-universal:1"                        default  OFFBOX_ENV_IMAGE
env.dockerfile   unset                                                         default  OFFBOX_ENV_DOCKERFILE
env.cpus         6.0                                                           env      OFFBOX_ENV_CPUS
env.memory       unset                                                         default  OFFBOX_ENV_MEMORY
...
files.copy       ["~/.config/gcloud/application_default_credentials.json"]    user     OFFBOX_FILES_COPY
git.mirror       true                                                          default  OFFBOX_GIT_MIRROR
git.refs         []                                                            default  OFFBOX_GIT_REFS
```

`env.image` and `env.dockerfile` count as one setting for precedence: a source that sets either replaces both from earlier sources, so `OFFBOX_ENV_IMAGE` can override a project `dockerfile`.

### User defaults

`config.toml` accepts the project sections under `project.`, with the same keys as `offbox.toml`:

```toml
[project.files]
copy = ["~/.config/gcloud/application_default_credentials.json"]

[project.sync]
ignore = [".agent"]
```

### Local overrides

`offbox.local.toml` has the same format as `offbox.toml`. Add it to `.gitignore` or `.git/info/exclude` so it stays out of commits. It synchronizes to the environment like other worktree files, so treat it with the same trust as `offbox.toml`; see [security](security.md#environment-variables).

### Environment variables

Every project setting has a variable named `OFFBOX_`, the section, an underscore and the key in upper case: `git.mirror` is `OFFBOX_GIT_MIRROR`, `env.shm_size` is `OFFBOX_ENV_SHM_SIZE` and `files.copy` is `OFFBOX_FILES_COPY`. `offbox config` lists all of them.

Values follow the setting's type:

- Text settings (`env.image`, `env.memory`, `install.command`, `stack.up` and similar) take the value as written, without quotes: `OFFBOX_ENV_MEMORY=8g`, `OFFBOX_INSTALL_COMMAND='uv sync --frozen'`.
- Booleans and numbers use TOML: `true`/`false`, `4`, `1.5`.
- Lists take a TOML array, `OFFBOX_SYNC_IGNORE='[".env", "dist"]'`. A single value without brackets is a one-item list (`OFFBOX_FILES_COPY=~/.config/gcloud/application_default_credentials.json`, `OFFBOX_PORTS_FORWARD=3000`), and an empty value is an empty list, which clears the list from earlier sources. A value without brackets is never split on commas, because paths and patterns can contain them.

An `OFFBOX_` variable that starts with a section name but matches no setting, such as `OFFBOX_GIT_MIRORR`, fails with `Unknown setting`. Other `OFFBOX_` names are left alone, including `OFFBOX_ENV`, which offbox sets inside environments.

```sh
OFFBOX_ENV_CPUS=6 offbox up
OFFBOX_GIT_MIRROR=false offbox exec -- pytest
export OFFBOX_FILES_COPY='["~/.config/gcloud/application_default_credentials.json"]'
```

## Project settings

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

[files]
copy = ["~/.config/gcloud/application_default_credentials.json"]

[git]
mirror = true
refs = ["origin/release"]
```

| Key | Variable | Default | Behavior |
| --- | --- | --- | --- |
| `env.host` | `OFFBOX_ENV_HOST` | Unset | Host a **new** environment uses, by name. An existing environment keeps its recorded host. With an auto-managed preset of that name, `up` creates the host on demand; see [auto-managed hosts](#auto-managed-hosts). |
| `env.image` | `OFFBOX_ENV_IMAGE` | Package `DEFAULT_IMAGE` | Registry image for the environment. Omit to use the packaged universal image. |
| `env.dockerfile` | `OFFBOX_ENV_DOCKERFILE` | Unset | Alternative Dockerfile inside this worktree; mutually exclusive with `image`. Source is flushed before the host builds it. |
| `env.cpus` | `OFFBOX_ENV_CPUS` | `max(1, host CPUs - 1)` | Finite CPU limit, from 0.01 through the host CPU count. |
| `env.memory` | `OFFBOX_ENV_MEMORY` | 75% of host memory | Positive Docker byte size; at least 6 MiB. Example: `"8g"`. |
| `env.shm_size` | `OFFBOX_ENV_SHM_SIZE` | `"2g"` | Docker shared-memory size, useful for browsers. |
| `install.command` | `OFFBOX_INSTALL_COMMAND` | Unset | Shell command run as `dev` in `/work`; no command means no dependency-install action. |
| `install.when` | `OFFBOX_INSTALL_WHEN` | `[]` | Ordered relative files controlling automatic reinstall before `exec`. Missing files count as empty; the first install still runs. |
| `stack.up` | `OFFBOX_STACK_UP` | Root Compose fallback | Explicit shell command, or `docker compose up -d` if a recognized root Compose file exists. |
| `stack.down` | `OFFBOX_STACK_DOWN` | Root Compose fallback | Declares the corresponding down command; `stop`/`rm` stop or remove the whole environment directly and do not run an extra teardown action. |
| `ports.forward` | `OFFBOX_PORTS_FORWARD` | `[]` | Explicit remote TCP ports, each 1–65535. |
| `ports.auto` | `OFFBOX_PORTS_AUTO` | `true` | Add published TCP ports from a root Compose stack; deduplicate IPv4/IPv6 entries. |
| `sync.ignore` | `OFFBOX_SYNC_IGNORE` | `[]` additional patterns | Add Mutagen ignore patterns; each value remains one pattern, including commas/quotes. Mandatory Git exclusion is applied last. |
| `exec.env` | `OFFBOX_EXEC_ENV` | `[]` | Names copied from the local environment when set; values are not stored in state. |
| `files.copy` | `OFFBOX_FILES_COPY` | `[]` | Files under your home directory, written `~/path`, copied to the same path under the environment's home (`/home/dev`). See [copied files](#copied-files). |
| `git.mirror` | `OFFBOX_GIT_MIRROR` | `true` | Mirrors `HEAD`, the remote's default branch and `git.refs` into `/work/.git`. See [git mirror](#git-mirror). |
| `git.refs` | `OFFBOX_GIT_REFS` | `[]` | Extra refs to mirror, such as `origin/release`, pushed under their full names (`refs/remotes/origin/release`). |

The default image has one definition, `DEFAULT_IMAGE` in [offbox/__init__.py](../src/offbox/__init__.py). Custom images must preserve the ready-file, `dev` user, Docker, `git` and `offbox-run` contracts; see [the universal image](universal-image.md).

Recognized root Compose filenames are `compose.yaml`, `compose.yml`, `docker-compose.yaml` and `docker-compose.yml`. Automatic setup runs only `mise install` when `.tool-versions` or `mise.toml` exists, the declared install command, and the declared/default stack command. `up --no-stack` skips stack startup. There is no additional project detection.

`up` explicitly runs the install stage. Later `exec` calls rerun it when the stored hash of the `when` files or the install command itself changes. The marker lives outside source at `/srv/offbox/envs/<env>/.offbox/install.sha256`; see [the hash and coordination contract](agents.md#dependency-installation). `exec --no-sync` uses the already-synced remote files without a hidden flush.

A custom Dockerfile image is cached by project-path and Dockerfile-content hashes. Other build-context changes alone do not invalidate this cache. An existing container retains its image until `up --fresh`; that command replaces its Docker data volume while keeping synchronized code, so it asks for confirmation on a terminal and needs `--yes` otherwise.

### Sync and environment variables

Default ignores cover dependency/cache/build directories: `node_modules`, `.venv`, `venv`, `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `dist`, `build`, `.next`, `.nuxt`, `target`, `.terraform`, `.DS_Store`, and mandatory `.git`. Project patterns retain their order and repetitions so later patterns can override earlier ones. Mandatory `.git` is moved to the end once. Offbox also enables VCS-directory ignoring. Linked worktrees' regular `.git` files are excluded too, including when project patterns use negation. Offbox sessions ignore unrelated global Mutagen defaults.

Patterns use Mutagen's default ignore syntax. A pattern without a `/`, such as `build` or `target`, matches that name at any depth, so the defaults also exclude source directories like `src/build/` or `packages/api/dist/`. A pattern with a `/` is relative to the worktree root, and a leading `!` re-includes a path matched by an earlier pattern. Project patterns are applied after the defaults, so a nested source directory can be re-included:

```toml
[sync]
ignore = ["!src/build", "!tools/target"]
```

A negated pattern re-includes only the named path; files inside a directory that is still ignored stay ignored. Use `/build` to ignore a root-level directory only.

`.gitignore` is not a secret filter. `.env` files synchronize unless `sync.ignore` lists them. Files created remotely, such as lockfiles and formatter output, sync back under two-way-safe conflict handling. Changing `sync.ignore` for an existing environment needs `mutagen sync terminate`, as the error explains, because the running session keeps its original patterns.

`exec --env NAME=value` overrides configured names for one command. Automatic install receives these scoped values too; `up` stages receive configured `exec.env` values. TTY execution additionally forwards `TERM`, `COLORTERM` and `LANG` when set. Offbox does not forward the whole local environment. Known secret-named values are redacted from diagnostics; command stdout/stderr remain the command's own streams.

Commands started by `offbox exec`, the install and stack stages of `up`, and `offbox shell` see `OFFBOX_ENV` set to the environment name, so scripts can tell they are already inside offbox.

### Copied files

`files.copy` sends files that live outside the worktree, typically credentials a test suite reads from a fixed path. Each entry is written relative to your home directory and lands at the same place under `/home/dev`:

```toml
[files]
copy = ["~/.config/gcloud/application_default_credentials.json"]
```

That file becomes `/home/dev/.config/gcloud/application_default_credentials.json`, readable only by `dev` (mode 0600), where Google client libraries look for application default credentials. Rules:

- Only regular files under your home directory, up to 1 MiB each. Anything under `~/.ssh`, `~/.gnupg` or `~/.config/offbox` is refused, including through a symlink: offbox resolves each entry and refuses targets in those directories or outside your home directory.
- Copies never sync back and live in the container: `up` copies every entry, `exec` copies entries whose contents changed, `up --fresh` and `offbox rm` drop them. Removing an entry leaves its copy until then.
- Tools that take a credential path from a variable need the environment path, not the laptop one. Set it in the project, for example `GOOGLE_APPLICATION_CREDENTIALS=/home/dev/.config/gcloud/application_default_credentials.json` in a synchronized `.env` file.
- The user `[project]` table is a good place for personal credentials: set once, every repository gets them, and a repository's own `files.copy` replaces the list.

### Git mirror

Sync never copies `.git`: a linked worktree's `.git` is only a pointer to a path on the laptop. The git mirror instead gives the environment a real repository at `/work/.git` with the laptop's commit and branch, so `git status`, `git diff`, `git ls-files`, changed-file test selection, `pre-commit` and version tools such as `setuptools-scm` work there. It is on by default.

Before `up` and each `exec`, offbox:

1. force-pushes `HEAD`, the remote's default branch and any `git.refs` over the existing SSH connection with `--no-verify`, so local hooks do not run;
2. points the environment's branch at the pushed commit (a detached `HEAD` stays detached) and resets its index to that commit without changing synchronized files;
3. copies the repository's `info/exclude`, so ignored files stay ignored.

The default branch is the target of the local `refs/remotes/origin/HEAD`, usually `origin/main`. `git clone` creates it; for a repository set up with `git remote add`, run `git remote set-head origin --auto` once. Without it, or when it points to a branch that no longer exists, offbox mirrors `HEAD` and `git.refs` only. Only a remote named `origin` counts; list other remotes' branches in `git.refs`.

Uncommitted changes arrive through sync and show as unstaged, because the index matches `HEAD`; staged-only state is not mirrored. `up` always pushes; `exec` skips the push when `HEAD`, the branch, the refs and the excludes are unchanged and `/work/.git` still exists, and pushes again otherwise. The first push of a worktree sends the history reachable from those refs, which for a large repository can take minutes (the push times out after 10 minutes); later pushes send only new objects. `exec --no-sync` skips the mirror. A repository without any commit is skipped until its first commit. A shallow local clone works. The environment has no `origin` remote, so commands that fetch from a Git server still need their own credentials.

The environment image needs `git`; with an image that lacks it, offbox prints `The environment image has no git, so the git mirror was skipped.` and runs commands without a repository. To turn the mirror off, set `git.mirror = false` in any source, for example `OFFBOX_GIT_MIRROR=false` for one command or `[project.git] mirror = false` in `config.toml` for every repository.

`exec.env`, `files.copy` and the mirrored refs are approved by `offbox up`; see [security](security.md#environment-variables).

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
| `project` | `{}` | Project setting defaults for every repository, in `[project.SECTION]` tables; see [user defaults](#user-defaults). |
| `hosts.NAME.provider` | `"hetzner"` | Built-in/custom provider name. |
| `hosts.NAME.size` | Provider default | Cloud machine size; Hetzner currently defaults to `ccx33`. |
| `hosts.NAME.region` | Provider default | Cloud location; Hetzner currently defaults to `fsn1`. |
| `hosts.NAME.image` | Provider default | Cloud operating-system image, distinct from `env.image`; Hetzner defaults to `ubuntu-24.04`. |
| `hosts.NAME.address` | Unset | Accepted existing-host address field; `host init` requires its target explicitly and does not read this preset. |
| `hosts.NAME.port` | `22` | Accepted existing-host port field; pass `--port` explicitly to `host init` when different from 22. |
| `hosts.NAME.ssh_key` | `~/.ssh/id_ed25519`, or `~/.ssh/offbox_ed25519` when only that key exists | Local private-key path used by `host create`; `host init` uses the same default. It must be usable without an interactive passphrase prompt, or loaded into an agent. |
| `hosts.NAME.auto` | `false` | Let offbox create this cloud host on first use and destroy it when unused; see [auto-managed hosts](#auto-managed-hosts). Not allowed with `provider = "ssh"`. |
| `hosts.NAME.idle` | `"2h"` | For an auto-managed host, how long it may sit unused before `offbox host reap` destroys it: `30m`, `2h`, `1d`, or `off`. |

`host init` does not load host presets. Pass `USER@ADDRESS`, any nondefault `--port`, and `--ssh-key` explicitly, for example `offbox host init dev@192.0.2.10 --name home --port 2222 --ssh-key ~/.ssh/home --host-key-fingerprint SHA256:...`.

A registered environment always uses its recorded host. Changing the default does not move it. Remove that environment before selecting another host with `up --host NAME`. A new environment uses `--host`, then `env.host`, then the default host.

## Auto-managed hosts

An auto-managed host exists only while worktrees use it. Opt in per preset:

```toml
# ~/.config/offbox/config.toml
[hosts.agg-ai]
provider = "hetzner"
size = "ccx33"
auto = true
idle = "2h"

[project.env]          # or env.host in a repository's offbox.toml
host = "agg-ai"
```

**Creation.** `offbox up` in a worktree whose host is `agg-ai` creates the VM `offbox-agg-ai` if it does not exist, prints `Creating auto-managed host agg-ai: billable until destroyed; …`, and continues. Parallel `up` commands on the same laptop create it once: they serialize on the host's lock, wait up to 15 minutes, and join the new host. A repository can only name a host; creation needs a user preset with `auto = true`. If cloud resources named `offbox-agg-ai` already exist (for example from another laptop), creation stops instead of adopting or duplicating them. An auto-managed host never becomes the default host. `auto` and `idle` are recorded when the host is created; editing the preset later affects the next host, not a running one. Hosts made by `offbox host create`, `setup` or `host init` are never destroyed automatically.

**Destruction.** Only these commands destroy an auto-managed host:

| Command | Destroys when |
| --- | --- |
| `offbox rm` | it removed the last environment on the host whose worktree still exists (removed worktrees do not count) |
| `offbox host reap [NAME]` | the host has been unused for longer than `idle`, or its creation started over 30 minutes ago and never finished |
| `offbox host destroy NAME` | always, as for any host |

A host is in use while any `up`, `exec`, `shell`, `ports`, `logs --stack` or `host ssh` runs against it: those commands hold a shared lock that destroy paths cannot take, so a long test run never loses its VM. Activity is the last start or end of `up`, `exec`, `shell`, `ports` or `logs --stack` in any of its environments. Browsing forwarded ports, editing synced files, `offbox status` and a running Compose stack with nothing attached are not activity; raise `idle` if you work that way. Idle reaping applies whether or not the worktrees still exist.

When the last `rm` finds the host busy, it waits a minute, then names the commands holding it and leaves the host for `offbox host reap`. Destroying the VM loses its Docker volumes, images and installed dependencies; source stays on the laptop through sync. The next `offbox up` creates a fresh host and reinstalls. `exec` in a worktree whose host was reaped fails with `Environment is not registered` and a fix naming when and why the host was reaped.

`offbox host reap --dry-run` shows each decision without changing anything, and `--json` emits a typed report (`hosts` with `name`, `action` of `kept`, `destroy` (dry run), `destroyed`, `skipped` or `failed`, `reason`, `lost_environments` and `error`). It exits 1 when any host failed and continues with the others. `offbox host list` shows each auto-managed host's idle limit, last use and the time after which it is reaped.

**The idle reaper.** Nothing runs `host reap` on a schedule by default. `offbox host reap --install-timer` installs a per-user launchd agent (macOS) or systemd timer (Linux) that runs it every 15 minutes; `--remove-timer` and `offbox uninstall` remove it. Before installing, offbox checks that every auto-managed preset's credentials are readable without your shell's environment: store the Hetzner token in `credentials.toml` rather than only exporting `HCLOUD_TOKEN`, and keep Google application default credentials at their default path. The unit receives `HOME`, `PATH` (the directories of OpenTofu or Terraform, ssh, git and Mutagen), any `XDG_*` overrides and `GOOGLE_PROJECT`, never a secret. It needs an installed `offbox` (`uv tool install`), not a temporary `uvx` copy. `offbox doctor`, `host list` and `up` warn when the timer has not run for an hour or its last run failed. The reaper runs on the laptop, so a sleeping laptop delays it; the VM keeps billing until the next run.

**Example day.** Agent 1 runs `up` at 09:00 and the VM is created; agent 2 joins it. Agent 1 runs `rm` at 12:00 and the VM stays. Agent 2 runs `rm` at 13:00 and the VM is destroyed. Had agent 2 crashed instead, the timer would destroy the VM about two hours after its last use.

## Credentials

Prefer a local `HCLOUD_TOKEN` environment variable. Alternatively, create the credentials file at the path above:

```toml
[hetzner]
token = "replace-locally"
```

Set its permissions to `0600`. The environment takes precedence. Never commit the file or put cloud-token values in project configuration/provider defaults. Other providers declare required environment names in their manifests; see [writing a provider](writing-a-provider.md).
