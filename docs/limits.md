# Limits and operating model

Offbox supports one source laptop/user, macOS or Linux clients, Ubuntu 24.04 amd64 hosts, and the Hetzner, Google Cloud and existing-SSH providers. Windows clients use WSL2. The universal image is amd64; an ARM laptop can be a client, but an arm64 host image is not provided.

## Git and project tooling

The laptop’s `.git` directory and linked-worktree pointer file are excluded from source sync. By default, a separate [Git mirror](configuration.md#git-mirror) supplies the remote commit, branch and reachable history, so Git-aware build/test tools can run there. Commits, branch management and pushes remain local; the remote repository has no `origin` remote or copied hooks. If you disable mirroring or use an image without Git, Git-dependent tools need an alternative. `exec --no-sync` skips both the sync flush and mirror update.

All project configuration is optional. Offbox automatically runs only root mise configuration, declared install commands and declared/default Compose startup. It does not infer package managers or start unrelated services. Custom Dockerfile rebuilds are keyed by Dockerfile content, not every build-context file. Change the Dockerfile when a cached custom build must be invalidated.

## Resources and isolation

Each environment has a separate Docker daemon and named data volume. CPU, memory and shared-memory limits apply to that whole environment, including its Compose services. The default memory limit is 75% of host memory per environment; limits are not reserved capacity. Choose explicit smaller limits when running several busy worktrees. Status warns about aggregate memory limits.

Containers are privileged and share the host kernel. They provide independent Docker names/data/ports for trusted projects, not a hostile-code security boundary. See [security](security.md). Manual hosts need explicit destruction to stop billing. Opt-in [auto-managed hosts](configuration.md#auto-managed-hosts) support last-environment cleanup and idle reaping; scheduled idle cleanup requires the laptop-local timer. Sleep, lost connectivity or failed credentials can delay destruction, so this is not a provider-side spending limit.

## Networking and downloads

Forwards bind only `127.0.0.1` on the client. An occupied preferred port moves to the next available port. Mappings persist across runs and remain reserved by stopped environments. Use `ports --swap PORT` when OAuth/CORS configuration needs a particular local port, including when the stopped environment holds it. Offbox will not claim an unrelated listener is its forward. Automatic Compose discovery forwards at most 32 ports per environment and warns about the rest; list required ports in `ports.forward`.

`exec` restores forwards while the command runs, so a forward can appear a fraction of a second after the command starts. While another lifecycle operation holds the host lock, such as a long `up` in another worktree on the same host, `exec` runs without restoring forwards and records the skip in its log. Other forwarding failures warn; retry `offbox ports` when the lifecycle operation finishes.

Project servers and Compose published ports must be reachable on the environment's network interface. A service listening only on the environment's loopback address cannot be reached by a host-to-container-IP forward; bind it to `0.0.0.0` inside that environment. UDP forwards are not supported.

Every inner Docker daemon has its own image store. Downloads can count against Docker Hub's per-source-IP rate limits; several environments share the host's outbound address. Image caches are not shared across environments. Configure registry authentication within the environment if your project requires it.

## Lifecycles and recovery

`stop` retains synchronized source and Docker data. `up --fresh` replaces the container and its Docker data, keeping source. `rm` removes the environment's remote source directory and Docker data. Keep valuable generated files synced back or backed up before removal. Host destruction removes all environments on that host; a cloud host can be charged until provider deletion completes.

Changing `env.image`, `env.cpus`, `env.memory`, `env.shm_size` or the Dockerfile does not recreate an existing container. `up` warns and `up --fresh` applies the change. Containers created before this check carry no settings label; their settings are treated as unknown, so that missing label alone does not trigger a warning.

Local state, copied provider workspaces and pinned host keys are required for reliable cleanup. Preserve them until hosts are destroyed. `uninstall --yes` refuses while cloud hosts are registered unless you also pass `--abandon-cloud-hosts`; abandoned hosts keep running and billing without local records, and are identified by the label `offbox-host=NAME`. Registered SSH hosts are retained remotely. Destroy hosts with `offbox host destroy NAME --yes` before uninstalling when you want them gone. Each state write keeps the previous `state.json` as `state.json.bak` for manual recovery.

Install coordination is per environment on one client. Parallel `exec` commands in one worktree share a single install; later commands wait for it. Shared remote worktrees managed from multiple laptops are outside the supported model. Port swap failures preserve ownership metadata; an incomplete rollback can require closing the affected SSH master and restoring forwards, as the printed `Fix:` explains.

## Validation boundaries

Unit tests cover exact orchestration, error handling, state and typed external-data contracts. Provider validation and mocked Terraform contracts check schemas and planned Hetzner and GCP resources without cloud credentials. The focused native Linux Docker smoke job covers real SSH/sync/exec/signals/forwarding and two isolated Compose stacks using disposable containers. It does not verify cloud API credentials/IAM, VM provisioning or bootstrap, real-host kernel differences, browser behavior, OOM behavior, or performance comparisons. CI creates no real-cloud resources. Separately approved manual [Hetzner](benchmarks/2026-10-23-hetzner.md) and [Google Cloud](benchmarks/2026-10-23-gcp.md) runs verified provisioning and both sample apps, including browser tests; their small performance samples are not general capacity or latency guarantees.
