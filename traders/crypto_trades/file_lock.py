"""Backwards-compatible re-export — use traders.common.locks instead."""

import os
import sys

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from traders.common.locks import (  # noqa: F401
    FileLockContext,
    atomic_write_json,
    atomic_write_text,
    load_json_with_defaults,
    with_file_lock,
)