# Repository safety and contributions

The public repository accepts issues and pull requests from forks. Contributors do not need cloud accounts, API tokens, access to repository secrets or write permission. Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for local checks; report vulnerabilities through [SECURITY.md](../SECURITY.md).

The default branch is `master`. GitHub requires a PR, an up-to-date branch, resolved review conversations and six successful GitHub Actions checks: `check`, `smoke`, and the four Linux/macOS Python 3.12/3.13 unit jobs. These protections also apply to administrators; force pushes and branch deletion are blocked. Required checks are bound to the GitHub Actions app. The path-filtered image build is deliberately not a required check for unrelated PRs.

A human approval count of zero lets the maintainer's authorized agent merge reviewed changes after CI. External contributors still cannot merge without repository write access. The agent performs the requested Opus review before merging; GitHub does not interpret an external CLI review as a formal PR approval. There is no administrator bypass for the agent or maintainer.

All external contributors' fork workflows require maintainer approval before execution. Review the proposed code and workflow changes before granting that approval. CI uses ephemeral GitHub-hosted runners, read-only tokens for PRs, no persisted checkout credentials, and no cloud secrets or billable cloud resources. The Docker smoke test uses privileged disposable containers on that runner; it must never target a personal or production Docker host. No `pull_request_target` workflow executes contributor code.

Actions must use full commit SHA pins. The image workflow separates read-only PR builds from registry-authorized publication on protected-branch pushes. The release workflow builds/tests with read-only permissions and gives publishing permissions only to a separate artifact-consuming job. Docker build contexts allow only the files needed by the image and fixture, excluding local state, Git credentials and ignored task data.

Secret scanning and push protection are enabled. Dependabot alerts and security-update proposals are enabled; dependency changes still need review and required CI. Branches are deleted automatically after merge. Private vulnerability reporting is enabled. Repository settings are managed through GitHub and are not enforced by merely editing this document.

PyPI publishing remains deferred. An active tag ruleset blocks creation, update and deletion of `v*` tags without bypass actors. The `pypi` environment accepts only `v*` tags, and its trusted publisher is not yet configured. Resuming releases requires an explicit maintainer decision to update the tag ruleset, review environment controls and configure PyPI; merging normal PRs does not enable publication.
