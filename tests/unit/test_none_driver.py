import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.drivers.none import NoneDriver
from offbox.errors import CommandFailed, ProviderError
from offbox.state import HostState


def host() -> HostState:
    return HostState(
        name="fixture",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=Path("known_hosts"),
    )


@pytest.mark.parametrize("labels", [None, {}, {"offbox.managed": "other"}])
def test_destroy_preserves_unlabelled_or_foreign_network(labels: dict[str, str] | None) -> None:
    runner = FakeRunner(
        [
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout=json.dumps([{"Name": "offbox", "Labels": labels}])),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
        ]
    )
    NoneDriver(runner, host()).destroy()
    commands = [call.argv[-1] for call in runner.calls]
    assert "docker volume ls -q --filter label=offbox.env" in commands
    assert "docker network rm offbox" not in commands
    assert commands[-2:] == [
        "sudo -n rm -rf /srv/offbox /var/lib/offbox",
        "sudo -n rm -f /etc/sudoers.d/offbox /home/offbox/.ssh/authorized_keys",
    ]


@pytest.mark.parametrize(
    "labels", [None, {}, {"offbox.env": "other", "offbox.worktree": "/work"}, {"offbox.env": "feature", "offbox.worktree": "relative"}]
)
def test_destroy_validates_volume_ownership_before_any_deletion(labels: dict[str, str] | None) -> None:
    runner = FakeRunner(
        [
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout="offbox-feature-docker\n"),
            Script(prefix=["ssh"], stdout=json.dumps([{"Name": "offbox-feature-docker", "Labels": labels}])),
        ]
    )
    with pytest.raises(ProviderError, match="ownership"):
        NoneDriver(runner, host()).destroy()
    assert len(runner.calls) == 3
    assert all(" rm " not in call.argv[-1] for call in runner.calls)


@pytest.mark.parametrize("status,stderr", [(255, "connection lost"), (1, "permission denied")])
def test_network_inspection_failure_never_starts_deletion(status: int, stderr: str) -> None:
    runner = FakeRunner([Script(prefix=["ssh"]), Script(prefix=["ssh"]), Script(prefix=["ssh"], returncode=status, stderr=stderr)])
    with pytest.raises(ProviderError, match="inspect"):
        NoneDriver(runner, host()).destroy()
    assert len(runner.calls) == 3


def test_missing_network_does_not_prevent_directory_cleanup() -> None:
    runner = FakeRunner(
        [
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], returncode=1, stderr="network offbox not found"),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
        ]
    )
    NoneDriver(runner, host()).destroy()
    assert [call.argv[-1] for call in runner.calls[-2:]] == [
        "sudo -n rm -rf /srv/offbox /var/lib/offbox",
        "sudo -n rm -f /etc/sudoers.d/offbox /home/offbox/.ssh/authorized_keys",
    ]


def test_access_is_revoked_only_after_owned_directories_are_removed() -> None:
    runner = FakeRunner(
        [
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], returncode=1, stderr="network offbox not found"),
            Script(prefix=["ssh"], returncode=1, stderr="rm: cannot remove '/srv/offbox': Device or resource busy"),
        ]
    )
    with pytest.raises(CommandFailed, match="Device or resource busy"):
        NoneDriver(runner, host()).destroy()
    assert all("sudoers" not in call.argv[-1] for call in runner.calls)


def test_foreign_network_endpoint_aborts_before_any_deletion() -> None:
    labels = {"offbox.env": "feature", "offbox.worktree": "/work"}
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], stdout="a" * 64),
            Script(prefix=["ssh"], stdout=json.dumps([{"Id": "a" * 64, "Name": "/offbox-feature", "Config": {"Labels": labels}}])),
            Script(prefix=["ssh"]),
            Script(
                prefix=["ssh"],
                stdout=json.dumps([{"Name": "offbox", "Labels": {"offbox.managed": "1"}, "Containers": {"b" * 64: {"Name": "foreign"}}}]),
            ),
            Script(prefix=["ssh"], stdout="b" * 64),
        ]
    )
    with pytest.raises(ProviderError, match="unrelated attached"):
        NoneDriver(runner, host()).destroy()
    assert len(runner.calls) == 5
    assert all(" rm " not in call.argv[-1] for call in runner.calls)


@pytest.mark.parametrize(
    "identity,name,labels",
    [
        ("b" * 64, "/offbox-feature", {"offbox.env": "feature", "offbox.worktree": "/work"}),
        ("a" * 64, "/foreign", {"offbox.env": "feature", "offbox.worktree": "/work"}),
        ("a" * 64, "/offbox-feature", None),
    ],
)
def test_container_inventory_mismatch_aborts_before_any_deletion(identity: str, name: str, labels: dict[str, str] | None) -> None:
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], stdout="a" * 64),
            Script(prefix=["ssh"], stdout=json.dumps([{"Id": identity, "Name": name, "Config": {"Labels": labels}}])),
        ]
    )
    with pytest.raises(ProviderError):
        NoneDriver(runner, host()).destroy()
    assert len(runner.calls) == 2
    assert all(" rm " not in call.argv[-1] for call in runner.calls)


def test_stopped_foreign_network_member_aborts_before_deletion() -> None:
    runner = FakeRunner(
        [
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout=json.dumps([{"Name": "offbox", "Labels": {"offbox.managed": "1"}, "Containers": {}}])),
            Script(prefix=["ssh"], stdout="b" * 64),
        ]
    )
    with pytest.raises(ProviderError, match="unrelated attached"):
        NoneDriver(runner, host()).destroy()
    assert len(runner.calls) == 4
    assert runner.calls[-1].argv[-1] == "docker ps -aq --no-trunc --filter network=offbox"
