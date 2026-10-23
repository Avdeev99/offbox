from collections.abc import Sequence


class OffboxError(Exception):
    def __init__(self, message: str, fix: str | None = None, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.exit_code = exit_code


class UsageError(OffboxError):
    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message, fix, 2)


class PreconditionError(OffboxError):
    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message, fix, 3)


class ConnectionError_(OffboxError):
    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message, fix, 4)


class CommandFailed(OffboxError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr_tail: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr_tail = "\n".join(stderr_tail.splitlines()[-20:])
        message = f"Command failed (exit {returncode}): {' '.join(argv)}"
        if self.stderr_tail:
            message += f"\n{self.stderr_tail}"
        super().__init__(message, "Check the command output and try again.")


class ProviderError(OffboxError):
    pass
