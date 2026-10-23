# Repository instructions

@RTK.md

## Commands

Use uv for Python tooling. Run `uv sync --frozen` to install locked dependencies. Before pushing, run `make check` and `make test`. Use `make fmt` for formatting and lint fixes and `uv build` for distributions. Validation uses deterministic unit tests, static checks, builds and one focused Linux Docker/SSH smoke job. Do not create billable resources or add real-cloud tests.

## Code rules

- No comments by default. A comment explains why, never what, and only if deleting it would lose a fact. Prefer better names. Short docstrings only on public functions whose contract is not obvious.
- No `print` outside `src/offbox/console.py`. That module owns stdout/stderr; use module loggers elsewhere: `logger = logging.getLogger(__name__)`. Human messages go to stderr, parseable results to stdout.
- Annotate every signature, including `-> None`. No `Any` in annotations. `# type: ignore[code]` is allowed only with a reason.
- Use typed Pydantic models at boundaries: config files, state files, provider outputs and `--json` output. Do not pass raw dicts between modules.
- Raise `OffboxError` subclasses, never bare `Exception`. No broad `except` that hides a failure. When catching to add context, use `raise ... from e`.
- All subprocesses go through `Runner`. Use argv lists, never `shell=True`.
- No speculative abstractions, feature flags for unused features, or compatibility shims. Fix root causes.
- Tests are deterministic. No `time.sleep` in unit tests; inject `Clock`/`Runner`. No `pytest.skip()` without a reason. No assertions that cannot fail. Do not end a test on `assert x is not None`; assert the actual value.
- Define `DEFAULT_IMAGE` only in `src/offbox/__init__.py`. Other defaults and generated documentation refer to that constant.
- Package root `host/`, `images/` and `providers/` resources under `offbox/_data/`; access them using `importlib.resources`.

## Git and PRs

- Use conventional commit messages: `feat:`, `fix:`, `docs:`, `test:`, `chore:`.
- One PR per milestone, targeting the existing default branch `master`. The PR description lists its milestone's acceptance criteria as a checked list, with command-output evidence for each.
- `make check` and `make test` must pass locally before pushing.
- Never commit secrets; tests never need real credentials. Do not create billable resources for tests.
- Keep local agent task scratch under the ignored `.agent/` directory.
