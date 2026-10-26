# Security

Report suspected vulnerabilities privately through [GitHub's vulnerability reporting form](https://github.com/o-avdieiev/offbox/security/advisories/new). Include the affected revision, impact and a minimal reproduction without real credentials. Use public issues for ordinary bugs and feature requests.

Offbox runs trusted personal projects in privileged containers on a single-user host. It is not a sandbox for untrusted workloads. Read the [security model](docs/security.md) before using it. Protect local provisioning state and destroy cloud hosts when finished; stopping an environment does not stop VM billing.

The current development line receives fixes on `master`. Security fixes follow the same tests and review process as other changes; this project does not promise a response deadline.
