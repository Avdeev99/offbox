# Limits and operating model

Offbox supports one source laptop/user, macOS or Linux clients, Ubuntu 24.04 amd64 hosts, and the Hetzner, Google Cloud and existing-SSH providers. Windows clients use WSL2. The universal image is amd64; an ARM laptop can be a client, but an arm64 host image is not provided.

## Git and project tooling

Git history and pointer files stay local. Git-dependent build/test tools such as setuptools-scm, pre-commit and Husky need a project-specific workaround: provide a version explicitly, disable Git hooks for the remote install, or run Git-dependent steps locally. `exec --no-sync` only skips flushing; it does not make Git metadata available.

All project configuration is optional. Offbox automatically runs only root mise configuration, declared install commands and declared/default Compose startup. It does not infer package managers or start unrelated services. Custom Dockerfile rebuilds are keyed by Dockerfile content, not every build-context file. Change the Dockerfile when a cached custom build must be invalidated.

## Resources and isolation

Each environment has a separate Docker daemon and named data volume. CPU, memory and shared-memory limits apply to that whole environment, including its Compose services. The default memory limit is 75% of host memory per environment; limits are not reserved capacity. Choose explicit smaller limits when running several busy worktrees. Status warns about aggregate memory limits.

Containers are privileged and share the host kernel. They provide independent Docker names/data/ports for trusted projects, not a hostile-code security boundary. See [security](security.md). There is no automatic idle destroy or cloud billing stop.

## Networking and downloads

Forwards bind only `127.0.0.1` on the client. An occupied preferred port moves to the next available port. Mappings persist across runs and remain reserved by stopped environments. Use `ports --swap PORT` when OAuth/CORS configuration needs a particular local port. Offbox will not claim an unrelated listener is its forward.

Project servers and Compose published ports must be reachable on the environment's network interface. A service listening only on the environment's loopback address cannot be reached by a host-to-container-IP forward; bind it to `0.0.0.0` inside that environment. UDP forwards are not supported.

Every inner Docker daemon has its own image store. Downloads can count against Docker Hub's per-source-IP rate limits; several environments share the host's outbound address. Image caches are not shared across environments. Configure registry authentication within the environment if your project requires it.

## Lifecycles and recovery

`stop` retains synchronized source and Docker data. `up --fresh` replaces the container and its Docker data, keeping source. `rm` removes the environment's remote source directory and Docker data. Keep valuable generated files synced back or backed up before removal. Host destruction removes all environments on that host; a cloud host can be charged until provider deletion completes.

Local state, copied provider workspaces and pinned host keys are required for reliable cleanup. Preserve them until hosts are destroyed. `uninstall --yes` refuses while cloud hosts are registered unless you also pass `--abandon-cloud-hosts`; abandoned hosts keep running and billing without local records, and are identified by the label `offbox-host=NAME`. Registered SSH hosts are retained remotely. Destroy hosts with `offbox host destroy NAME --yes` before uninstalling when you want them gone. Each state write keeps the previous `state.json` as `state.json.bak` for manual recovery.

Install coordination is per environment on one client. Shared remote worktrees managed from multiple laptops are outside the supported model. Port swap failures preserve ownership metadata; an incomplete rollback can require closing the affected SSH master and restoring forwards, as the printed `Fix:` explains.

## Validation boundaries

Unit tests cover exact orchestration, error handling, state and typed external-data contracts. Provider validation and mocked Terraform contracts check schemas and planned GCP resources without cloud credentials. The focused native Linux Docker smoke job covers real SSH/sync/exec/signals/forwarding and two isolated Compose stacks using disposable containers. It does not verify cloud API credentials/IAM, VM provisioning or bootstrap, real-host kernel differences, browser behavior, OOM behavior, or performance comparisons. CI creates no real-cloud resources. Separately approved manual [Hetzner](benchmarks/2026-10-23-hetzner.md) and [Google Cloud](benchmarks/2026-10-23-gcp.md) runs verified provisioning and both sample apps, including browser tests; their small performance samples are not general capacity or latency guarantees.
