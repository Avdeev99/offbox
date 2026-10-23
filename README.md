# offbox

Keep your editor and coding agents local. Give every git worktree its own full Docker stack on one cloud VM. Your compose files don't change.

**Under construction.** Version 0.0.1 establishes the Python CLI and repository tooling. VM provisioning, environments, sync and execution are planned for subsequent milestones.

Requires Python 3.12 or newer, on macOS or Linux (Windows through WSL2).

```sh
uv sync --frozen
uv run offbox --version
uv run offbox version
uv run python -m offbox --version
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and release setup. Licensed under [MIT](LICENSE).
