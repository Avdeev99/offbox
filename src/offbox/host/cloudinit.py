from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from offbox import DEFAULT_IMAGE, __version__
from offbox.errors import PreconditionError
from offbox.logging import register_secrets
from offbox.runner import Runner


def bootstrap_script() -> str:
    return files("offbox").joinpath("_data", "host", "host-init.sh").read_text(encoding="utf-8")


def render_cloud_init(public_key: str, private_host_key: str, public_host_key: str, *, script: str | None = None) -> str:
    template = files("offbox").joinpath("_data", "host", "cloud-init.yaml.j2").read_text(encoding="utf-8")
    register_secrets({"OFFBOX_HOST_PRIVATE_KEY": private_host_key})
    return (
        Environment(undefined=StrictUndefined, keep_trailing_newline=True)
        .from_string(template)
        .render(
            private_host_key=private_host_key,
            public_host_key=public_host_key,
            script=bootstrap_script() if script is None else script,
            bootstrap_argv=[
                "env",
                "OFFBOX_HARDEN=1",
                f"OFFBOX_VERSION={__version__}",
                "/usr/local/sbin/offbox-host-init",
                DEFAULT_IMAGE,
                public_key,
            ],
        )
    )


def generate_host_key(runner: Runner, path: Path) -> tuple[str, str]:
    runner.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "offbox-host", "-f", str(path)])
    try:
        path.chmod(0o600)
        return path.read_text(encoding="utf-8"), path.with_name(path.name + ".pub").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise PreconditionError("Cannot read generated SSH host key", "Check state directory permissions and ssh-keygen.") from exc
