import hashlib
import os
import re
from pathlib import Path

from pydantic import field_validator

from offbox.config import Model
from offbox.errors import PreconditionError
from offbox.runner import Runner


def validate_env_name(name: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,36}", name) is None:
        raise PreconditionError("Invalid environment name", "Repair the environment state before continuing.")
    return name


def worktree_name(root: Path) -> str:
    absolute = Path(os.path.abspath(root))
    slug = re.sub(r"[^a-z0-9]+", "-", absolute.name.lower()).strip("-") or "env"
    return slug[:32] + "-" + hashlib.sha256(str(absolute).encode()).hexdigest()[:4]


class Worktree(Model):
    root: Path
    name: str

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return validate_env_name(value)

    @field_validator("root")
    @classmethod
    def check_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Worktree root must be absolute")
        return value

    @property
    def container(self) -> str:
        return f"offbox-{self.name}"

    @property
    def volume(self) -> str:
        return f"offbox-{self.name}-docker"

    @property
    def directory(self) -> str:
        return f"/srv/offbox/envs/{self.name}"

    @property
    def work(self) -> str:
        return f"{self.directory}/work"


def find_worktree(runner: Runner, cwd: Path | None = None) -> Worktree:
    result = runner.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or Path.cwd(), check=False, timeout=5)
    root = result.stdout.rstrip("\n")
    if result.returncode or not root or not Path(root).is_absolute():
        raise PreconditionError("Not inside a git worktree", "cd into a git worktree")
    absolute = Path(os.path.abspath(root))
    return Worktree(root=absolute, name=worktree_name(absolute))
