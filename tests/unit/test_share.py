import base64
import shlex
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.config import FilesConfig, GitConfig, ProjectConfig
from offbox.errors import PreconditionError
from offbox.naming import Worktree
from offbox.share import MAX_COPY_BYTES, MirrorPlan, Sharing, check_sharing, copy_path, git_sharing, sharing_additions
from offbox.state import EnvState, HostState, State, StateStore


def context(root: Path) -> tuple[Worktree, HostState]:
    return Worktree(root=root / "app", name="app-abcd"), HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        known_hosts_path=root / "known_hosts",
    )


def env(work: Worktree, **values: object) -> EnvState:
    return EnvState.model_validate(
        {"name": work.name, "host": "main", "worktree_path": work.root, "created_at": datetime(2026, 10, 23, tzinfo=UTC), **values}
    )


def project(*, copy: list[str] | None = None, mirror: bool = False, refs: list[str] | None = None) -> ProjectConfig:
    return ProjectConfig(files=FilesConfig.model_validate({"copy": copy or []}), git=GitConfig(mirror=mirror, refs=refs or []))


def test_files_copy_parses_from_toml_key() -> None:
    assert ProjectConfig.model_validate({"files": {"copy": ["~/a"]}}).files.copy_ == ["~/a"]


@pytest.mark.parametrize("entry", ["~/.config/gcloud/application_default_credentials.json", "~/token"])
def test_copy_path_accepts_files_below_home(entry: str) -> None:
    assert str(copy_path(entry)) == entry[2:]


@pytest.mark.parametrize("entry", ["/etc/passwd", "token", "~", "~/", "~/../x", "~/a/./b", "~/a//b", "~/a\nb"])
def test_copy_path_rejects_paths_outside_home(entry: str) -> None:
    with pytest.raises(PreconditionError, match="Invalid files.copy entry"):
        copy_path(entry)


@pytest.mark.parametrize(
    "entry",
    ["~/.ssh/id_ed25519", "~/.ssh", "~/.SSH/id_ed25519", "~/.config/offbox/credentials.toml", "~/.Config/Offbox/x", "~/.gnupg/secring.gpg"],
)
def test_copy_path_refuses_keys_and_offbox_credentials(entry: str) -> None:
    with pytest.raises(PreconditionError, match="files.copy must not copy"):
        copy_path(entry)


def test_git_sharing_lists_head_and_refs_only_when_mirroring() -> None:
    assert git_sharing(GitConfig(mirror=False, refs=["origin/main"])) == []
    assert git_sharing(GitConfig(mirror=True, refs=["origin/main", "origin/main", "a", "HEAD"])) == [
        "HEAD",
        "origin/HEAD",
        "a",
        "origin/main",
    ]


def test_exec_refuses_sharing_that_up_has_not_approved(tmp_path: Path) -> None:
    work, _ = context(tmp_path)
    recorded = env(work, approved_files=[], approved_git=["HEAD", "origin/HEAD"])
    with pytest.raises(PreconditionError) as caught:
        check_sharing(project(copy=["~/token"], mirror=True, refs=["origin/main"]), recorded)
    assert caught.value.message == "offbox up has not approved sharing: files.copy ~/token, git origin/main"
    assert caught.value.fix == "Review files.copy and git with offbox config, then run offbox up to approve them."


def test_approved_or_removed_sharing_runs(tmp_path: Path) -> None:
    work, _ = context(tmp_path)
    recorded = env(work, approved_files=["~/token", "~/old"], approved_git=["HEAD", "origin/HEAD", "origin/main"])
    check_sharing(project(copy=["~/token"], mirror=True), recorded)
    check_sharing(project(), recorded)


def test_environments_from_before_approval_accept_only_the_default_mirror(tmp_path: Path) -> None:
    work, _ = context(tmp_path)
    check_sharing(project(mirror=True), env(work))
    with pytest.raises(PreconditionError, match="git origin/dev$"):
        check_sharing(project(mirror=True, refs=["origin/dev"]), env(work))
    with pytest.raises(PreconditionError, match="files.copy ~/token$"):
        check_sharing(project(copy=["~/token"]), env(work))


def test_up_warns_about_new_sharing_but_not_on_first_approval(tmp_path: Path) -> None:
    work, _ = context(tmp_path)
    configured = project(copy=["~/token"], mirror=True, refs=["origin/main"])
    assert sharing_additions(configured, env(work)) == []
    assert sharing_additions(configured, env(work, approved_files=[], approved_git=["HEAD", "origin/HEAD"])) == [
        "Copying new local files to the environment: ~/token",
        "Mirroring new git refs to the environment: origin/main",
    ]


def test_copy_writes_changed_files_as_base64_into_the_container_home(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    home = tmp_path / "home"
    (home / ".config/gcloud").mkdir(parents=True)
    (home / ".config/gcloud/adc.json").write_bytes(b'{"type": "authorized_user"}\n')
    (home / "same").write_text("unchanged\n")
    runner = FakeRunner([Script(prefix=["ssh", "offbox-main"])])
    from hashlib import sha256

    copied = Sharing(runner, home).copy_files(
        work,
        host,
        ["~/.config/gcloud/adc.json", "~/same"],
        {"~/same": sha256(b"unchanged\n").hexdigest(), "~/.config/gcloud/adc.json": "0" * 64},
        force=False,
    )
    assert len(runner.calls) == 1
    remote = shlex.split(runner.calls[0].argv[-1])
    assert remote[:7] == ["docker", "exec", "-i", "-u", "dev", work.container, "sh"]
    assert remote[-1] == "/home/dev/.config/gcloud/adc.json"
    assert base64.b64decode(runner.calls[0].input or "") == b'{"type": "authorized_user"}\n'
    assert copied == {
        "~/.config/gcloud/adc.json": sha256(b'{"type": "authorized_user"}\n').hexdigest(),
        "~/same": sha256(b"unchanged\n").hexdigest(),
    }


def test_forced_copy_rewrites_unchanged_files(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    (tmp_path / "token").write_text("x")
    runner = FakeRunner([Script(prefix=["ssh"])])
    from hashlib import sha256

    Sharing(runner, tmp_path).copy_files(work, host, ["~/token"], {"~/token": sha256(b"x").hexdigest()}, force=True)
    assert len(runner.calls) == 1


def test_missing_and_oversized_copy_sources_fail_before_any_write(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    runner = FakeRunner()
    with pytest.raises(PreconditionError, match="not a regular file") as caught:
        Sharing(runner, tmp_path).copy_files(work, host, ["~/missing"], {}, force=False)
    assert (
        caught.value.fix == f"Create {tmp_path / 'missing'} or remove ~/missing from files.copy; offbox config shows which source sets it."
    )
    (tmp_path / "big").write_bytes(b"x" * (MAX_COPY_BYTES + 1))
    with pytest.raises(PreconditionError, match="larger than 1 MiB"):
        Sharing(runner, tmp_path).copy_files(work, host, ["~/big"], {}, force=False)
    assert runner.calls == []


def test_copy_refuses_symlinks_into_blocked_or_outside_directories(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh/id_ed25519").write_text("private")
    (tmp_path / "outside").write_text("other")
    (home / "key").symlink_to(home / ".ssh/id_ed25519")
    (home / "escape").symlink_to(tmp_path / "outside")
    runner = FakeRunner()
    for entry, target in (("~/key", home / ".ssh/id_ed25519"), ("~/escape", tmp_path / "outside")):
        with pytest.raises(PreconditionError) as caught:
            Sharing(runner, home).copy_files(work, host, [entry], {}, force=False)
        assert caught.value.message == f"files.copy must not copy {entry}, which resolves to {target.resolve()}"
    assert runner.calls == []


def test_unknown_git_refs_name_the_entry(tmp_path: Path) -> None:
    root = tmp_path / "app"
    runner = FakeRunner(
        [
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD^{commit}"], stdout="a" * 40 + "\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref"], returncode=1),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--symbolic-full-name"], returncode=128, stderr="fatal: ambiguous\n"),
        ]
    )
    with pytest.raises(PreconditionError, match="not a local branch, tag or remote-tracking ref: origin/missing$") as caught:
        Sharing(runner).plan(root, ["origin/missing"])
    assert caught.value.fix == "Fetch it or remove it from git.refs; offbox config shows which source sets it."


def test_plan_resolves_head_branch_refs_and_exclude(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (tmp_path / "common/info").mkdir(parents=True)
    (tmp_path / "common/info/exclude").write_text("offbox.toml\n")
    head, main = "a" * 40, "b" * 40
    runner = FakeRunner(
        [
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD^{commit}"], stdout=head + "\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"], stdout="feature/x\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], stdout="refs/remotes/origin/main\n"),
            Script(
                prefix=["git", "-C", str(root), "rev-parse", "--symbolic-full-name", "origin/main"], stdout="refs/remotes/origin/main\n"
            ),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"], stdout=main + "\n"),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--git-path"], stdout="../common/info/exclude\n"),
        ]
    )
    assert Sharing(runner).plan(root, ["origin/main"]) == MirrorPlan(
        head=head, branch="feature/x", refs={"refs/remotes/origin/main": main}, exclude="offbox.toml\n"
    )


def test_plan_detached_head_and_non_ref_names(tmp_path: Path) -> None:
    root = tmp_path / "app"
    runner = FakeRunner(
        [
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify"], stdout="a" * 40 + "\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref"], returncode=1),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--git-path"], stdout=".git/info/exclude\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref"], returncode=1),
        ]
    )
    assert Sharing(runner).plan(root, []) == MirrorPlan(head="a" * 40, branch=None, refs={}, exclude="")
    runner = FakeRunner(
        [
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify"], stdout="a" * 40 + "\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref"], returncode=1),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--symbolic-full-name"], stdout="\n"),
        ]
    )
    with pytest.raises(PreconditionError, match="git.refs entry is not a local branch, tag or remote-tracking ref: abc123"):
        Sharing(runner).plan(root, ["abc123"])


def test_plan_adds_the_remote_default_branch(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    runner = FakeRunner(
        [
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD^{commit}"], stdout="a" * 40 + "\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"], stdout="feature\n"),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--git-path"], stdout=".git/info/exclude\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], stdout="refs/remotes/origin/trunk\n"),
            Script(
                prefix=["git", "-C", str(root), "rev-parse", "--verify", "-q", "refs/remotes/origin/trunk^{commit}"], stdout="d" * 40 + "\n"
            ),
        ]
    )
    planned = Sharing(runner).plan(root, [])
    assert planned is not None and planned.refs == {"refs/remotes/origin/trunk": "d" * 40}


def test_plan_skips_a_default_branch_that_no_longer_exists(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    runner = FakeRunner(
        [
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD^{commit}"], stdout="a" * 40 + "\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"], stdout="feature\n"),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--git-path"], stdout=".git/info/exclude\n"),
            Script(prefix=["git", "-C", str(root), "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], stdout="refs/remotes/origin/gone\n"),
            Script(prefix=["git", "-C", str(root), "rev-parse", "--verify", "-q", "refs/remotes/origin/gone^{commit}"], returncode=1),
        ]
    )
    planned = Sharing(runner).plan(root, [])
    assert planned is not None and planned.refs == {}


def test_plan_is_empty_until_the_first_commit(tmp_path: Path) -> None:
    root = tmp_path / "app"
    runner = FakeRunner([Script(prefix=["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD^{commit}"], returncode=1)])
    assert Sharing(runner).plan(root, ["origin/main"]) is None
    assert len(runner.calls) == 1


def test_mirror_initializes_pushes_without_hooks_then_checks_out_the_index(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    head, main = "a" * 40, "b" * 40
    runner = FakeRunner([Script(prefix=["ssh", "offbox-main"]), Script(prefix=["git"]), Script(prefix=["ssh", "offbox-main"])])
    Sharing(runner).mirror(work, host, MirrorPlan(head=head, branch="feature/x", refs={"refs/remotes/origin/main": main}, exclude="x\n"))
    init, push, checkout = runner.calls
    assert "git init -q" in shlex.split(init.argv[-1])[8]
    assert push.argv == [
        "git",
        "-C",
        str(work.root),
        "push",
        "--force",
        "--quiet",
        "--no-verify",
        "--receive-pack",
        f"docker exec -i -u dev {work.container} git-receive-pack",
        "offbox-main:/work",
        f"{head}:refs/offbox/head",
        f"{main}:refs/remotes/origin/main",
    ]
    assert push.env == {"GIT_SSH_COMMAND": "ssh"}
    remote = shlex.split(checkout.argv[-1])
    assert remote[-3:] == ["offbox-share", head, "feature/x"]
    assert "git reset -q --mixed" in remote[8]
    assert checkout.input == "x\n"


def test_images_without_git_skip_the_mirror_with_one_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work, host = context(tmp_path)
    plan = MirrorPlan(head="a" * 40, branch=None, refs={}, exclude="")
    runner = FakeRunner([Script(prefix=["ssh", "offbox-main"], stdout="missing-git\n")])
    assert Sharing(runner).mirror(work, host, plan) is False
    assert len(runner.calls) == 1

    class GitlessSharing(PlannedSharing):
        def mirror(self, work: Worktree, host: HostState, plan: MirrorPlan) -> bool:
            self.pushed.append(plan)
            return False

    store = store_with(tmp_path, env(work), host)
    sharing = GitlessSharing([plan])
    for _ in range(3):
        sharing.apply(store, work, host, project(mirror=True), force=False, repository=False)
    assert sharing.pushed == [plan]
    assert store.read().envs[work.name].mirrored_git == f"no-git:{plan.fingerprint()}"
    assert capsys.readouterr().err.count("The environment image has no git, so the git mirror was skipped.") == 1
    moved = MirrorPlan(head="b" * 40, branch=None, refs={}, exclude="")
    sharing.plans = [moved]
    sharing.apply(store, work, host, project(mirror=True), force=False, repository=False)
    assert sharing.pushed == [plan, moved]


def test_failed_checkout_names_the_git_error_and_the_opt_out(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    runner = FakeRunner(
        [Script(prefix=["ssh"]), Script(prefix=["git"]), Script(prefix=["ssh"], returncode=128, stderr="fatal: bad object aaaa\n")]
    )
    with pytest.raises(PreconditionError) as caught:
        Sharing(runner).mirror(work, host, MirrorPlan(head="a" * 40, branch=None, refs={}, exclude=""))
    assert caught.value.message == "Cannot check out the mirrored commit in the environment: fatal: bad object aaaa"
    assert "OFFBOX_GIT_MIRROR=false" in (caught.value.fix or "")


def test_failed_push_names_the_git_error_and_the_opt_out(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    runner = FakeRunner(
        [Script(prefix=["ssh"]), Script(prefix=["git"], returncode=128, stderr="hint\nfatal: shallow update not allowed\n")]
    )
    with pytest.raises(PreconditionError) as caught:
        Sharing(runner).mirror(work, host, MirrorPlan(head="a" * 40, branch=None, refs={}, exclude=""))
    assert caught.value.message == "Cannot mirror git history to the environment: fatal: shallow update not allowed"
    assert "git.mirror = false" in (caught.value.fix or "")


def store_with(tmp_path: Path, recorded: EnvState, host: HostState) -> StateStore:
    store = StateStore(tmp_path / "state")
    store.write(State(hosts={host.name: host}, envs={recorded.name: recorded}))
    return store


class PlannedSharing(Sharing):
    def __init__(self, plans: list[MirrorPlan]) -> None:
        super().__init__(FakeRunner())
        self.plans = plans
        self.pushed: list[MirrorPlan] = []

    def plan(self, root: Path, refs: Sequence[str]) -> MirrorPlan:
        if not self.plans:
            pytest.fail("no git mirror was expected")
        return self.plans[0]

    def mirror(self, work: Worktree, host: HostState, plan: MirrorPlan) -> bool:
        self.pushed.append(plan)
        return True


def test_apply_skips_an_unchanged_mirror_and_records_new_ones(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    plan = MirrorPlan(head="a" * 40, branch=None, refs={}, exclude="")
    store = store_with(tmp_path, env(work, mirrored_git=plan.fingerprint()), host)
    sharing = PlannedSharing([plan])
    sharing.apply(store, work, host, project(mirror=True), force=False)
    assert sharing.pushed == []
    sharing.apply(store, work, host, project(mirror=True), force=True)
    assert sharing.pushed == [plan]
    moved = MirrorPlan(head="c" * 40, branch=None, refs={}, exclude="")
    sharing.plans = [moved]
    sharing.apply(store, work, host, project(mirror=True), force=False)
    assert sharing.pushed == [plan, moved]
    assert store.read().envs[work.name].mirrored_git == moved.fingerprint()


def test_apply_pushes_again_when_the_environment_lost_its_repository(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    plan = MirrorPlan(head="a" * 40, branch=None, refs={}, exclude="")
    store = store_with(tmp_path, env(work, mirrored_git=plan.fingerprint()), host)
    sharing = PlannedSharing([plan])
    sharing.apply(store, work, host, project(mirror=True), force=False, repository=False)
    assert sharing.pushed == [plan]


def test_apply_without_git_keeps_the_recorded_mirror(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    store = store_with(tmp_path, env(work, mirrored_git="f" * 64), host)
    PlannedSharing([]).apply(store, work, host, project(mirror=True), force=False, git=False)
    assert store.read().envs[work.name].mirrored_git == "f" * 64


def test_apply_records_copied_file_digests(tmp_path: Path) -> None:
    work, host = context(tmp_path)
    (tmp_path / "token").write_text("x")
    store = store_with(tmp_path, env(work), host)
    Sharing(FakeRunner([Script(prefix=["ssh"])]), tmp_path).apply(store, work, host, project(copy=["~/token"]), force=False)
    from hashlib import sha256

    assert store.read().envs[work.name].copied_files == {"~/token": sha256(b"x").hexdigest()}
