"""Configuration and filesystem validation for explicitly selected sources."""
import os
from pathlib import Path


def required_path(variable, directory=True):
    raw = os.environ.get(variable, "").strip()
    if not raw:
        raise ValueError(f"Set {variable} before selecting this connector")
    path = Path(raw).expanduser().resolve(strict=True)
    if directory and not path.is_dir():
        raise ValueError(f"{variable} must name a readable directory")
    if not directory and not path.is_file():
        raise ValueError(f"{variable} must name a readable file")
    return str(path)


def walk_error(error):
    raise error
