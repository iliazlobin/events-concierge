"""Small fail-closed loader for deployment-mounted secret files.

Kubernetes Secret Manager CSI mounts and similar mechanisms expose one secret per read-only file.
Application and migration processes use the same ambiguity and size checks so a rollout cannot
silently prefer an old environment value over a newly rotated mounted value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_MAX_SECRET_FILE_BYTES = 65_536


def resolve_env_or_file(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return ``NAME`` or the contents of ``NAME_FILE``, rejecting ambiguous input."""
    source = os.environ if environ is None else environ
    inline = source.get(name)
    file_path = source.get(f"{name}_FILE")
    if inline is not None and file_path is not None:
        raise ValueError(f"configure exactly one of {name} and {name}_FILE")
    if file_path is None:
        return inline
    return read_secret_file(file_path, setting_name=name)


def read_secret_file(path_value: str, *, setting_name: str) -> str:
    """Read one bounded regular file without leaking its path or contents in failures."""
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{setting_name}_FILE must be an absolute path")
    try:
        stat = path.stat()
    except OSError as error:
        raise ValueError(f"{setting_name}_FILE is not readable") from error
    if not path.is_file():
        raise ValueError(f"{setting_name}_FILE must identify a regular file")
    if stat.st_size <= 0 or stat.st_size > _MAX_SECRET_FILE_BYTES:
        raise ValueError(f"{setting_name}_FILE must contain 1-{_MAX_SECRET_FILE_BYTES} bytes")
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"{setting_name}_FILE must contain readable UTF-8") from error
    # Secret managers commonly terminate text values with one newline. Preserve every other byte,
    # including intentional leading/trailing spaces inside credentials.
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value or "\x00" in value:
        raise ValueError(f"{setting_name}_FILE resolved to invalid secret material")
    return value
