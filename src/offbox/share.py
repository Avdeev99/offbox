import base64
import hashlib
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

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
_MIRROR_REF = "refs/offbox/head"

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
    """What the git mirror sends to the environment, as the list offbox up approves."""
    return ["HEAD", *sorted(set(config.refs))] if config.mirror else []


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
        raise PreconditionError(
            f"files.copy must not copy {entry}", "Remove SSH, GnuPG and offbox configuration files from files.copy in offbox.toml."
        )
    return relative


def unapproved(configured: Sequence[str], approved: Sequence[str] | None) -> list[str]:
    return sorted(set(configured) - set(approved or ()))


def check_sharing(project: ProjectConfig, recorded: EnvState) -> None:
    """Refuse files.copy entries and git refs that offbox up has not approved; the VM can edit offbox.toml through sync."""
    for entry in project.files.copy_:
        copy_path(entry)
    files = unapproved(project.files.copy_, recorded.approved_files)
    refs = unapproved(git_sharing(project.git), recorded.approved_git)
    if files or refs:
        names = [*(f"files.copy {entry}" for entry in files), *(f"git {ref}" for ref in refs)]
        raise PreconditionError(
            "offbox up has not approved sharing: " + ", ".join(names),
            "Review files.copy and git in offbox.toml, then run offbox up to approve them.",
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

    def _container(self, host: HostState, work: Worktree, script: str, args: Sequence[str], *, input: str, timeout: float) -> None:
        argv = ["docker", "exec", "-i", "-u", "dev", work.container, "sh", "-c", script, "offbox-share", *args]
        result = self.runner.run(ssh_argv(host.name, argv), check=False, input=input, timeout=timeout)
        if result.returncode == 255:
            raise ConnectionError_(f"Cannot reach host {host.name}", f"Run offbox host ssh {host.name} and check connectivity.")
        if result.returncode:
            raise CommandFailed(["docker", "exec", work.container, "offbox-share"], result.returncode, result.stderr)

    def copy_files(
        self, work: Worktree, host: HostState, entries: Sequence[str], copied: Mapping[str, str], *, force: bool
    ) -> dict[str, str]:
        """Copy changed files.copy entries into the container home and return the digests now present there."""
        result: dict[str, str] = {}
        for entry in entries:
            relative = copy_path(entry)
            source = self.home.joinpath(*relative.parts)
            fix = f"Create {source} or remove {entry} from files.copy in offbox.toml."
            try:
                if not source.is_file():
                    raise PreconditionError(f"files.copy source is not a regular file: {source}", fix)
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

    def plan(self, root: Path, refs: Sequence[str]) -> MirrorPlan:
        head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        symbolic = self.runner.run(["git", "-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"], check=False, timeout=30)
        resolved: dict[str, str] = {}
        for ref in sorted(set(refs)):
            full = self._git(root, "rev-parse", "--symbolic-full-name", ref)
            if not full.startswith("refs/"):
                raise PreconditionError(f"git.refs entry is not a branch or tag: {ref}", "Name refs such as origin/main in git.refs.")
            resolved[full] = self._git(root, "rev-parse", "--verify", f"{full}^{{commit}}")
        exclude = root / self._git(root, "rev-parse", "--git-path", "info/exclude")
        try:
            text = exclude.read_text() if exclude.is_file() else ""
        except OSError as exc:
            raise PreconditionError(f"Cannot read {exclude}", "Check the repository's info/exclude permissions.") from exc
        branch = symbolic.stdout.strip() if symbolic.returncode == 0 and symbolic.stdout.strip() else None
        return MirrorPlan(head=head, branch=branch, refs=resolved, exclude=text)

    def mirror(self, work: Worktree, host: HostState, plan: MirrorPlan) -> None:
        """Push HEAD and the configured refs into /work/.git and point its index at HEAD without touching synced files."""
        self._container(host, work, _INIT_REPOSITORY, [], input="", timeout=60)
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
                "Check offbox status, then retry; set git.mirror = false in offbox.toml to run without git.",
            )
        self._container(host, work, _CHECKOUT, [plan.head, plan.branch or ""], input=plan.exclude, timeout=120)
        logger.info("mirrored %s into %s", plan.head, work.name)

    def apply(self, store: StateStore, work: Worktree, host: HostState, project: ProjectConfig, *, force: bool, git: bool = True) -> None:
        """Bring copied files and the git mirror up to date, recording what the environment now holds."""
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
                if force or plan.fingerprint() != recorded.mirrored_git:
                    self.mirror(work, host, plan)
                mirrored = plan.fingerprint()
        if copied == recorded.copied_files and mirrored == recorded.mirrored_git:
            return
        with store.transaction() as state:
            current = state.envs.get(work.name)
            if current is not None and current.host == host.name:
                current.copied_files = copied
                current.mirrored_git = mirrored
