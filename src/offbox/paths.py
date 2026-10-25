import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from offbox.errors import PreconditionError


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path
    cache: Path
    control: Path

    @classmethod
    def discover(cls, env: Mapping[str, str] | None = None, home: Path | None = None) -> "Paths":
        values = os.environ if env is None else env
        base = home if home is not None else Path.home()
        return cls(
            Path(values.get("XDG_CONFIG_HOME") or base / ".config") / "offbox",
            Path(values.get("XDG_STATE_HOME") or base / ".local/state") / "offbox",
            Path(values.get("XDG_CACHE_HOME") or base / ".cache") / "offbox",
            Path(f"/tmp/offbox-{os.getuid()}"),
        )

    @property
    def user_config(self) -> Path:
        return self.config / "config.toml"

    @property
    def credentials(self) -> Path:
        return self.config / "credentials.toml"

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    def log_file(self, day: date | None = None) -> Path:
        return self.logs / f"{day or date.today()}.log"

    def secure_roots(self) -> None:
        for root in (self.state, self.cache):
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if stat.S_IMODE(root.stat().st_mode) != 0o700:
                root.chmod(0o700)

    def ensure_control(self) -> Path:
        self.control.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.control.is_symlink() or self.control.stat().st_uid != os.getuid():
            raise PreconditionError(f"Unsafe SSH control directory: {self.control}", "Remove the directory and retry.")
        self.control.chmod(0o700)
        return self.control
