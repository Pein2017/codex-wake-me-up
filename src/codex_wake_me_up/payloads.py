"""Private, bounded JSON payload files for producer capability operations."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .models import ValidationError


MAX_PRIVATE_PAYLOAD_BYTES = 64 * 1024


def load_private_json_payload(path: str | Path) -> dict[str, Any]:
    selected = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(selected, flags)
    except OSError as exc:
        raise ValidationError("payload path must be a readable regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("payload path must be a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValidationError("payload file must be private to the current user")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_PRIVATE_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PRIVATE_PAYLOAD_BYTES:
            raise ValidationError("payload file exceeds the 64 KiB limit")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("payload file must contain one JSON object") from exc
    if not isinstance(value, dict):
        raise ValidationError("payload file must contain one JSON object")
    return value
