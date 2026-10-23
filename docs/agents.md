# Using offbox from coding agents

The CLI prints human diagnostics to stderr and machine-readable results to stdout. Use `--json` when available. Every offbox failure includes a `Fix:` line. Commands do not prompt unless stdin is a terminal.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | offbox or a wrapped tool failed |
| 2 | Invalid command usage |
| 3 | A precondition is unmet; follow the `Fix:` line |
| 4 | The host cannot be reached |
| N | `offbox exec` preserves the remote command's own exit code, including 130 and 143 for signals |

A remote command can itself exit with codes 1–4. When execution and status commands are available, agents that need to distinguish an offbox failure from a command failure should run `offbox status --json` after a nonzero exit.

## Diagnostics

`offbox doctor --json` emits a report with a `checks` array and exits 0 when every check passes, otherwise 3. Each check includes its name, success status, message, and an actionable fix for failure. Human diagnostics use `offbox doctor`.

`offbox --version` and `offbox version` print only the version to stdout. `-v` echoes subprocess commands to stderr with secrets redacted. Invocation details, durations, statuses and error tracebacks are written to the daily log under `$XDG_STATE_HOME/offbox/logs`, defaulting to `~/.local/state/offbox/logs`. Logs are retained for 14 days.

The global `--timings` flag prints stage durations and total command-handling time to stderr, including measured stages that raise an error. For example, use `offbox --timings up`, `offbox --timings exec -- pytest`, or `offbox --timings status --json`. Repeated stages are accumulated with a call count. The total starts after argument parsing and excludes Python/import/CLI startup; uninstrumented work is included only in the total. Stdout, JSON and exit statuses retain their usual contracts. Eager help/version options can exit before timing collection begins.

## Runtime tools

- OpenSSH 8.0 or newer and Git 2.30 or newer are required.
- Sync targets the Mutagen **0.18.x** minor series. Mutagen is installed separately and is never downloaded by offbox. On macOS, use `brew install mutagen-io/mutagen/mutagen`; other systems use the [official releases](https://github.com/mutagen-io/mutagen/releases).
- Provisioning prefers OpenTofu 1.8 or newer on PATH, then Terraform 1.6 or newer. If neither is available, offbox downloads pinned OpenTofu **1.10.6** for macOS/Linux on amd64/arm64 and verifies the archive against the SHA256 embedded in the package. A checksum mismatch stops installation.

## Dependency installation

`offbox up` runs the configured install command after synchronization. Before `offbox exec`, a changed installation hash triggers installation again; setup output goes to stderr so stdout contains only the requested command's output. Configured `[exec].env` variables and explicit `exec --env` overrides also reach the install command.

The host computes the hash in `[install].when` order. Each line is the lowercase SHA256 of the file contents, two spaces, the configured relative filename, and a newline; the combined hash is the SHA256 of those lines. Missing files count as empty contents. Empty and missing files therefore have the same content hash, while filenames and order remain significant. Unsafe paths, symlinks, directories and I/O failures stop installation. After a successful install, the host recomputes the hash and atomically writes `/srv/offbox/envs/<env>/.offbox/install.sha256`, outside the synced work directory.

One nonblocking local lock per environment coordinates hash checking, installation and marker updates from this laptop. Another installation for that environment fails with a retry instruction; other worktrees remain independent. The lock is released before the requested command starts. Coordination across multiple source laptops is outside the supported single-client workflow.

`offbox --help` lists the currently implemented commands.

## Environment status

`offbox status --json` emits `StatusReport` and exits 3 if any check has `level: "error"`, otherwise 0. Warnings do not fail the command. Host connection failures are reported inside the JSON and also exit 3. Dependent probes are omitted when the host or environment cannot be checked. Unexpected software failures retain the ordinary CLI error contract.

```json
{
  "env": "project-abcd",
  "host": "main",
  "checks": [
    {"name": "host", "level": "ok", "message": "Host main reachable, offbox 0.0.1", "fix": null},
    {"name": "disk", "level": "warning", "message": "Host disk 80% used", "fix": "Run offbox gc or offbox rm in unused worktrees."}
  ],
  "ports": {"forwards": []},
  "stack": [],
  "sync": null
}
```

The serialized top-level fields are `env` and `host` (strings or null), `checks`, `ports`, `stack` and `sync`. Each check has `name`, `level` (`ok`, `warning`, `error`), `message` and `fix` (null only for success). Port entries have `env`, `host`, `remote`, `local`, `ip` and `active`. Stack entries have `name`, `state` and nullable `health`. A populated sync record has `state`, `paused`, `conflicts`, `problems`, `last_error`, `ok` and `fix`. The versioned contract is enforced by the JSON schema snapshot in `tests/fixtures/status-schema.json`.

Status checks host reachability/version, disk, live running memory limits, container ownership/readiness, sync health, image updates, restored port forwards and Compose health. Disk usage warns at 80% and fails at 95%. A stopped inner Docker daemon fails readiness even if its old ready file remains. Running environment memory limits exceeding host RAM, or an unlimited environment, produce a warning.

The image advisory reads registry metadata through `docker manifest inspect --verbose` and compares the Linux amd64 image **config digest** with the running container's Docker image ID. It never pulls image layers. Registry/authentication/unsupported-manifest failures are explicit warnings, not a claim that the image is current. Custom Dockerfiles use the configured synchronized Dockerfile hash and local image cache; unsynchronized changes or a missing build produce a warning with a recovery instruction. Status restores port forwards but does not flush source, install dependencies or start a stack.

## Logs and first-run cleanup

`offbox logs` writes today's attributed records for the current git worktree to stdout. Records are JSON lines with `timestamp`, `invocation`, `env`, `level`, `logger` and `message`. The invocation ID associates startup records with the environment resolved later in that invocation. Multiline messages remain one escaped JSON record; secrets are redacted before serialization. Older records without environment metadata cannot be attributed safely and are omitted. `--follow` follows additions and the daily date change. `offbox logs --stack [SERVICE] [--follow]` streams Compose logs directly, without sync or install hooks.

`offbox setup` prompts only with terminal stdin. Every answer has an equivalent flag: `--provider`, `--action`, `--target`, `--name`, `--ssh-key`, `--size`, `--region`, `--port`, and `--accept-host-key`. Noninteractive checks can use `offbox setup --provider ssh --action check`. Provisioning requires explicit `--action create` or an affirmative interactive choice; it can create a billable VM. `--action init` bootstraps an existing server.

`offbox uninstall` terminates verified owned Mutagen sessions, closes owned SSH masters and removes local state/cache/logs/control resources plus the exact generated SSH Include and configuration. It preserves user config, credentials, custom providers, SSH keys and remote hosts. If hosts remain, it lists them and requires explicit `--yes`; use `offbox host destroy NAME` first when you intend to remove those hosts. Uninstalling with `--yes` retains remote hosts while deleting their local provisioning state.
