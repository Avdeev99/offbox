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

## Runtime tools

- OpenSSH 8.0 or newer and Git 2.30 or newer are required.
- Sync targets the Mutagen **0.18.x** minor series. Mutagen is installed separately and is never downloaded by offbox. On macOS, use `brew install mutagen-io/mutagen/mutagen`; other systems use the [official releases](https://github.com/mutagen-io/mutagen/releases).
- Provisioning prefers OpenTofu 1.8 or newer on PATH, then Terraform 1.6 or newer. If neither is available, offbox downloads pinned OpenTofu **1.10.6** for macOS/Linux on amd64/arm64 and verifies the archive against the SHA256 embedded in the package. A checksum mismatch stops installation.

## Dependency installation

`offbox up` runs the configured install command after synchronization. Before `offbox exec`, a changed installation hash triggers installation again; setup output goes to stderr so stdout contains only the requested command's output. Configured `[exec].env` variables and explicit `exec --env` overrides also reach the install command.

The host computes the hash in `[install].when` order. Each line is the lowercase SHA256 of the file contents, two spaces, the configured relative filename, and a newline; the combined hash is the SHA256 of those lines. Missing files count as empty contents. Empty and missing files therefore have the same content hash, while filenames and order remain significant. Unsafe paths, symlinks, directories and I/O failures stop installation. After a successful install, the host recomputes the hash and atomically writes `/srv/offbox/envs/<env>/.offbox/install.sha256`, outside the synced work directory.

One nonblocking local lock per environment coordinates hash checking, installation and marker updates from this laptop. Another installation for that environment fails with a retry instruction; other worktrees remain independent. The lock is released before the requested command starts. Coordination across multiple source laptops is outside the supported single-client workflow.

`offbox --help` lists the currently implemented commands.
