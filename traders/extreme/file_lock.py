"""Backwards-compatible re-export — use traders.common.locks instead."""

from traders.common import bootstrap  # noqa: F401

from traders.common.locks import (  # noqa: F401
    FileLockContext,
    atomic_write_json,
    atomic_write_text,
    load_json_safe,
    load_json_with_defaults,
    with_file_lock,
)