import base64
import hashlib
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from offbox import console
from offbox.config import GitConfig, Model, ProjectConfig
from offbox.errors import CommandFailed, ConnectionError_, PreconditionError
from offbox.naming import Worktree
from offbox.runner import Runner
from offbox.ssh import ssh_argv
from offbox.state import EnvState, HostState, StateStore
from offbox.timings import stage

logger = logging.getLogger(__name__)
MAX_COPY_BYTES = 1024 * 1024
_BLOCKED_COPIES = (PurePosixPath(".ssh"), PurePosixPath(".config/offbox"), PurePosixPath(".gnupg"))
_CONTAINER_HOME = PurePosixPath("/home/dev")
_BLOCKED_FIX = "Remove SSH, GnuPG and offbox configuration files from files.copy; offbox config shows which source sets it."
_MIRROR_REF = "refs/offbox/head"
DEFAULT_BRANCH = "origin/HEAD"
DEFAULT_GIT_SHARING = ("HEAD", DEFAULT_BRANCH)

_WRITE_FILE = r"""set -eu
umask 077
target=$1
mkdir -p -- "$(dirname -- "$target")"
tmp=$(mktemp "$target.XXXXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
base64 -d > "$tmp"
mv -f -- "$tmp" "$target"
"""
_INIT_REPOSITORY = r"""set -eu
command -v git >/dev/null 2>&1 || { echo missing-git; exit 0; }
cd /work
[ -d .git ] || git init -q
git config receive.shallowUpdate true
git config receive.denyCurrentBranch ignore
"""
_CHECKOUT = r"""set -eu
cd /work
head=$1
branch=$2
if [ -n "$branch" ]; then
    git update-ref "refs/heads/$branch" "$head"
    git symbolic-ref HEAD "refs/heads/$branch"
else
    git update-ref --no-deref HEAD "$head"
fi
git update-ref -d refs/offbox/head
mkdir -p .git/info
cat > .git/info/exclude
git reset -q --mixed
"""


class MirrorPlan(Model):
    head: str
    branch: str | None
    refs: dict[str, str]
    exclude: str

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def git_sharing(config: GitConfig) -> list[str]:
    """What the git mirror sends to the environment, as the list offbox up approves; origin/HEAD is the remote's default branch."""
    return [*DEFAULT_GIT_SHARING, *sorted(set(config.refs) - set(DEFAULT_GIT_SHARING))] if config.mirror else []


def copy_path(entry: str) -> PurePosixPath:
    """The home-relative path of a files.copy entry; only regular paths under ~/ outside key and offbox directories."""
    relative = PurePosixPath(entry[2:]) if entry.startswith("~/") else None
    if (
        relative is None
        or not relative.parts
        or any(part in ("", ".", "..") for part in entry[2:].split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in entry)
    ):
        raise PreconditionError(f"Invalid files.copy entry: {entry}", "Use paths below your home directory written as ~/path/to/file.")
    if any(relative.is_relative_to(blocked) for blocked in _BLOCKED_COPIES):
        raise PreconditionError(f"files.copy must not copy {entry}", _BLOCKED_FIX)
    return relative


def unapproved(configured: Sequence[str], approved: Sequence[str] | None) -> list[str]:
    return sorted(set(configured) - set(approved or ()))


def check_sharing(project: ProjectConfig, recorded: EnvState) -> None:
    """Refuse files.copy entries and git refs that offbox up has not approved; the VM can edit offbox.toml through sync."""
    for entry in project.files.copy_:
        copy_path(entry)
    files = unapproved(project.files.copy_, recorded.approved_files)
    refs = unapproved(git_sharing(project.git), DEFAULT_GIT_SHARING if recorded.approved_git is None else recorded.approved_git)
    if files or refs:
        names = [*(f"files.copy {entry}" for entry in files), *(f"git {ref}" for ref in refs)]
        raise PreconditionError(
            "offbox up has not approved sharing: " + ", ".join(names),
            "Review files.copy and git with offbox config, then run offbox up to approve them.",
        )


def sharing_additions(project: ProjectConfig, recorded: EnvState) -> list[str]:
    """Newly configured sharing that this up approves; the first up of an environment approves silently."""
    messages: list[str] = []
    for entry in project.files.copy_:
        copy_path(entry)
    if recorded.approved_files is not None and (files := unapproved(project.files.copy_, recorded.approved_files)):
        messages.append("Copying new local files to the environment: " + ", ".join(files))
    if recorded.approved_git is not None and (refs := unapproved(git_sharing(project.git), recorded.approved_git)):
        messages.append("Mirroring new git refs to the environment: " + ", ".join(refs))
    return messages


class Sharing:
    def __init__(self, runner: Runner, home: Path | None = None) -> None:
        self.runner = runner
        self.home = home or Path.home()

    def _container(self, host: HostState, work: Worktree, script: str, args: Sequence[str], *, input: str, timeout: float) -> str:
        argv = ["docker", "exec", "-i", "-u", "dev", work.container, "sh", "-c", script, "offbox-share", *args]
        result = self.runner.run(ssh_argv(host.name, argv), check=False, input=input, timeout=timeout)
        if result.returncode == 255:
            raise ConnectionError_(f"Cannot reach host {host.name}", f"Run offbox host ssh {host.name} and check connectivity.")
        if result.returncode:
            raise CommandFailed(["docker", "exec", work.container, "offbox-share"], result.returncode, result.stderr)
        return result.stdout

    def copy_files(
        self, work: Worktree, host: HostState, entries: Sequence[str], copied: Mapping[str, str], *, force: bool
    ) -> dict[str, str]:
        """Copy changed files.copy entries into the container home and return the digests now present there."""
        result: dict[str, str] = {}
        for entry in entries:
            relative = copy_path(entry)
            source = self.home.joinpath(*relative.parts)
            fix = f"Create {source} or remove {entry} from files.copy; offbox config shows which source sets it."
            try:
                if not source.is_file():
                    raise PreconditionError(f"files.copy source is not a regular file: {source}", fix)
                home = self.home.resolve()
                resolved = source.resolve()
                if not resolved.is_relative_to(home) or any(resolved.is_relative_to(home / blocked) for blocked in _BLOCKED_COPIES):
                    raise PreconditionError(f"files.copy must not copy {entry}, which resolves to {resolved}", _BLOCKED_FIX)
                if source.stat().st_size > MAX_COPY_BYTES:
                    raise PreconditionError(f"files.copy source is larger than 1 MiB: {source}", "Copy large files another way.")
                content = source.read_bytes()
            except OSError as exc:
                raise PreconditionError(f"Cannot read files.copy source {source}", fix) from exc
            digest = hashlib.sha256(content).hexdigest()
            if force or copied.get(entry) != digest:
                target = _CONTAINER_HOME.joinpath(*relative.parts)
                self._container(host, work, _WRITE_FILE, [str(target)], input=base64.b64encode(content).decode(), timeout=60)
                logger.info("copied %s into %s", entry, work.name)
            result[entry] = digest
        return result

    def _git(self, root: Path, *args: str) -> str:
        return self.runner.run(["git", "-C", str(root), *args], timeout=30).stdout.strip()

    def plan(self, root: Path, refs: Sequence[str]) -> MirrorPlan | None:
        """What to mirror, or None while the repository has no commit yet."""
        current = self.runner.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD^{commit}"], check=False, timeout=30)
        if current.returncode:
            return None
        head = current.stdout.strip()
        symbolic = self.runner.run(["git", "-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"], check=False, timeout=30)
        resolved: dict[str, str] = {}
        for ref in sorted(set(refs)):
            named = self.runner.run(["git", "-C", str(root), "rev-parse", "--symbolic-full-name", ref], check=False, timeout=30)
            full = named.stdout.strip()
            if named.returncode or not full.startswith("refs/"):
                raise PreconditionError(
                    f"git.refs entry is not a local branch, tag or remote-tracking ref: {ref}",
                    "Fetch it or remove it from git.refs; offbox config shows which source sets it.",
                )
            resolved[full] = self._git(root, "rev-parse", "--verify", f"{full}^{{commit}}")
        exclude = root / self._git(root, "rev-parse", "--git-path", "info/exclude")
        try:
            text = exclude.read_text() if exclude.is_file() else ""
        except OSError as exc:
            raise PreconditionError(f"Cannot read {exclude}", "Check the repository's info/exclude permissions.") from exc
        default = self.runner.run(["git", "-C", str(root), "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], check=False, timeout=30)
        target = default.stdout.strip()
        if default.returncode == 0 and target.startswith("refs/remotes/origin/") and target not in resolved:
            commit = self.runner.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{target}^{{commit}}"], check=False, timeout=30
            )
            if commit.returncode == 0:
                resolved[target] = commit.stdout.strip()
            else:
                logger.info("origin/HEAD points to missing %s; mirroring without the default branch", target)
        branch = symbolic.stdout.strip() if symbolic.returncode == 0 and symbolic.stdout.strip() else None
        return MirrorPlan(head=head, branch=branch, refs=resolved, exclude=text)

    def mirror(self, work: Worktree, host: HostState, plan: MirrorPlan) -> bool:
        """Push HEAD and the configured refs into /work/.git and point its index at HEAD without touching synced files.

        Returns False without pushing when the environment image has no git."""
        if self._container(host, work, _INIT_REPOSITORY, [], input="", timeout=60).strip() == "missing-git":
            return False
        receive = f"docker exec -i -u dev {work.container} git-receive-pack"
        specs = [f"{plan.head}:{_MIRROR_REF}", *(f"{sha}:{ref}" for ref, sha in plan.refs.items())]
        result = self.runner.run(
            [
                "git",
                "-C",
                str(work.root),
                "push",
                "--force",
                "--quiet",
                "--no-verify",
                "--receive-pack",
                receive,
                f"offbox-{host.name}:/work",
                *specs,
            ],
            check=False,
            env={"GIT_SSH_COMMAND": "ssh"},
            timeout=600,
        )
        if result.returncode:
            raise PreconditionError(
                "Cannot mirror git history to the environment: " + (result.stderr.strip().splitlines() or ["git push failed"])[-1],
                "Check offbox status, then retry; set git.mirror = false (or OFFBOX_GIT_MIRROR=false) to run without git.",
            )
        try:
            self._container(host, work, _CHECKOUT, [plan.head, plan.branch or ""], input=plan.exclude, timeout=120)
        except CommandFailed as exc:
            raise PreconditionError(
                "Cannot check out the mirrored commit in the environment: " + (exc.stderr_tail.splitlines() or ["git failed"])[-1],
                "Check git in the environment image, then retry; set git.mirror = false (or OFFBOX_GIT_MIRROR=false) to run without git.",
            ) from exc
        logger.info("mirrored %s into %s", plan.head, work.name)
        return True

    def apply(
        self,
        store: StateStore,
        work: Worktree,
        host: HostState,
        project: ProjectConfig,
        *,
        force: bool,
        git: bool = True,
        repository: bool = True,
    ) -> None:
        """Bring copied files and the git mirror up to date; repository=False means /work/.git is gone and must be pushed again."""
        recorded = store.read().envs.get(work.name)
        if recorded is None:
            raise PreconditionError("Environment registration disappeared", "Run offbox up.")
        copied = dict(recorded.copied_files)
        if project.files.copy_:
            with stage("File copies"):
                copied = self.copy_files(work, host, project.files.copy_, recorded.copied_files, force=force)
        mirrored = recorded.mirrored_git
        if project.git.mirror and git:
            with stage("Git mirror"):
                plan = self.plan(work.root, project.git.refs)
                if plan is not None:
                    stale = force or not repository or plan.fingerprint() != recorded.mirrored_git
                    if stale and not self.mirror(work, host, plan):
                        console.warn(
                            "The environment image has no git, so the git mirror was skipped. "
                            "Fix: add git to the image or set git.mirror = false."
                        )
                    mirrored = plan.fingerprint()
        if copied == recorded.copied_files and mirrored == recorded.mirrored_git:
            return
        with store.transaction() as state:
            current = state.envs.get(work.name)
            if current is not None and current.host == host.name:
                current.copied_files = copied
                current.mirrored_git = mirrored
