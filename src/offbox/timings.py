from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import perf_counter

from offbox import console


@dataclass
class _Timing:
    seconds: float = 0
    calls: int = 0


@dataclass
class _Session:
    started: float
    stages: dict[str, _Timing] = field(default_factory=dict[str, _Timing])


_active: ContextVar[_Session | None] = ContextVar("offbox_timings", default=None)


@contextmanager
def collect(enabled: bool) -> Generator[None]:
    if not enabled:
        yield
        return
    session = _Session(started=perf_counter())
    token = _active.set(session)
    try:
        yield
    finally:
        elapsed = perf_counter() - session.started
        _active.reset(token)
        console.info("Timings (CLI startup excluded):")
        for label, value in session.stages.items():
            calls = f" ({value.calls} calls)" if value.calls > 1 else ""
            console.info(f"  {label}: {value.seconds:.3f}s{calls}")
        console.info(f"  Total: {elapsed:.3f}s")


@contextmanager
def stage(label: str) -> Generator[None]:
    session = _active.get()
    if session is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        value = session.stages.setdefault(label, _Timing())
        value.seconds += perf_counter() - started
        value.calls += 1


def measured[**P, R](label: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def call(*args: P.args, **kwargs: P.kwargs) -> R:
            with stage(label):
                return function(*args, **kwargs)

        return call

    return decorate
