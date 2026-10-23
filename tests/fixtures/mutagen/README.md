# Mutagen v0.18.1 unit data

`v0.18.1-problems.json` is a constructed example derived from the public v0.18.1 Go export models; it was not captured from a running daemon. Paths, errors, identifiers and change digests are synthetic. Other unit variants are built from typed sessions. No tests start Mutagen or SSH.

Sources: [session](https://github.com/mutagen-io/mutagen/blob/v0.18.1/pkg/api/models/synchronization/session.go), [endpoint](https://github.com/mutagen-io/mutagen/blob/v0.18.1/pkg/api/models/synchronization/endpoint.go), [conflict](https://github.com/mutagen-io/mutagen/blob/v0.18.1/pkg/api/models/synchronization/conflict.go), [configuration](https://github.com/mutagen-io/mutagen/blob/v0.18.1/pkg/api/models/synchronization/configuration.go), and [status enum](https://github.com/mutagen-io/mutagen/blob/v0.18.1/pkg/synchronization/state.go).

The list template emits an array with flattened session configuration/state and endpoint problem arrays. Excluded counts represent additional omitted problems/conflicts. Ignore-VCS is a boolean and file modes are octal strings.

Creation disables global configuration so exact offbox settings are reproducible. `--ignore` uses pflag CSV StringSlice parsing, so each configured pattern must be encoded as a single CSV field. [Ignore-VCS checks directories only](https://github.com/mutagen-io/mutagen/blob/v0.18.1/pkg/synchronization/core/ignore/ignore_vcs.go); the explicit `.git` ignore also excludes the regular `.git` pointer file in linked worktrees.
