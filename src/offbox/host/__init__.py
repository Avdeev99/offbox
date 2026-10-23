import fcntl
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from offbox.config import UserConfig, default_host, load_config
from offbox.errors import PreconditionError
from offbox.paths import Paths
from offbox.providers import validate_name
from offbox.runner import Runner
from offbox.state import HostState, StateStore


def select_host(paths: Paths, name: str | None = None) -> HostState:
    state = StateStore(paths.state).read()
    selected = name or default_host(load_config(paths.user_config, UserConfig), state.default_host)
    if selected is None:
        raise PreconditionError("No default host", "Run offbox host create or offbox host use NAME.")
    validate_name(selected)
    if selected not in state.hosts:
        raise PreconditionError(f"Unknown host: {selected}", "Run offbox host list or create this host.")
    return state.hosts[selected]


def public_key(runner: Runner, key: Path) -> str:
    derived = runner.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], check=False, timeout=5)
    if derived.returncode == 0:
        content = derived.stdout.strip()
        sidecar = key.with_name(key.name + ".pub")
        try:
            if sidecar.exists() and sidecar.read_text(encoding="utf-8").split()[:2] != content.split()[:2]:
                raise PreconditionError(
                    "SSH public key sidecar does not match the private key", f"Regenerate {sidecar} with ssh-keygen -y -f {key}."
                )
        except (OSError, UnicodeError) as exc:
            raise PreconditionError(f"Cannot read {sidecar}", "Correct the SSH public key file permissions and encoding.") from exc
    else:
        path = key.with_name(key.name + ".pub")
        if not path.is_file():
            raise PreconditionError(f"SSH key is unavailable: {key}", f"Generate the key or load it with ssh-add {key}.")
        actual = runner.run(["ssh-keygen", "-lf", str(path)], check=False, timeout=5)
        expected = runner.run(["ssh-keygen", "-lf", str(key)], check=False, timeout=5) if key.is_file() else actual
        agent = runner.run(["ssh-add", "-l"], check=False, timeout=5)
        expected_fields, actual_fields = expected.stdout.split(), actual.stdout.split()
        if (
            expected.returncode
            or actual.returncode
            or agent.returncode
            or len(expected_fields) < 2
            or len(actual_fields) < 2
            or expected_fields[1] != actual_fields[1]
            or expected_fields[1] not in agent.stdout
        ):
            raise PreconditionError(
                "SSH public key does not match an available identity", f"Regenerate {path} and load the matching key with ssh-add {key}."
            )
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise PreconditionError(f"Cannot read public key for {key}", "Generate its .pub file with ssh-keygen -y.") from exc
    fields = content.split()
    if len(content.splitlines()) != 1 or len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise PreconditionError("Invalid SSH public key", "Generate a valid SSH key with ssh-keygen.")
    return content


@contextmanager
def host_operation(paths: Paths, name: str) -> Generator[None]:
    validate_name(name)
    directory = paths.state / "host-locks"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (directory / f"{name}.lock").open("a", encoding="utf-8")
    except OSError as exc:
        raise PreconditionError("Cannot open host operation lock", "Check state directory permissions.") from exc
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreconditionError(
                f"A lifecycle operation is already running for {name}", "Wait for that operation to finish, then retry."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
