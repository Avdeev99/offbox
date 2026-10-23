import os
import sys

from pydantic import BaseModel
from rich.console import Console


def _human(message: str, style: str = "") -> None:
    Console(stderr=True, no_color="NO_COLOR" in os.environ or not sys.stderr.isatty()).print(
        message, style=style, markup=False, highlight=False
    )


def output(value: str) -> None:
    print(value)


def version(value: str) -> None:
    output(value)


def info(message: str) -> None:
    _human(message)


def success(message: str) -> None:
    _human(f"✓ {message}", "green")


def warn(message: str) -> None:
    _human(f"! {message}", "yellow")


def error(message: str, fix: str | None = None) -> None:
    _human(f"✗ {message}", "red")
    if fix:
        _human(f"  Fix: {fix}")


def step(message: str) -> None:
    _human(f"→ {message}")


def command(message: str) -> None:
    _human(f"$ {message}", "dim")


def json_out(model: BaseModel) -> None:
    output(model.model_dump_json())
