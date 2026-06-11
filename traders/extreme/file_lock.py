"""file_lock.py — Shared file locking utilities for aitrader.

Provides thread-safe file operations for concurrent access patterns,
particularly for pending_review.json shared across trading bots.
"""
import os
import fcntl
import json


def with_file_lock(lock_file_path, func):
    """Execute func with exclusive flock on the lock file.

    Args:
        lock_file_path: Path to the lock file (will be created if needed)
        func: Callable to execute under the lock

    Returns:
        The return value of func

    Raises:
        Exception: Any exception raised by func is propagated
    """
    os.makedirs(os.path.dirname(lock_file_path), exist_ok=True)
    with open(lock_file_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return func()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def atomic_write_json(file_path, data, indent=2):
    """Write JSON data atomically using temp file + os.replace.

    Uses the temp file pattern to avoid readers seeing a torn/partial write.
    os.replace is atomic on POSIX, so readers only ever see the old or the new
    file — never a half-written one.

    Args:
        file_path: Target file path
        data: Python dict/list to write as JSON
        indent: JSON indentation (default: 2)
    """
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp = f"{file_path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, file_path)


def atomic_write_text(file_path, content):
    """Write text data atomically using temp file + os.replace.

    Args:
        file_path: Target file path
        content: String content to write
    """
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp = f"{file_path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, file_path)


def load_json_safe(file_path, default=None):
    """Load JSON file with safe error handling.

    Args:
        file_path: Path to JSON file
        default: Default value to return if file doesn't exist or is invalid

    Returns:
        Loaded JSON data or default
    """
    if default is None:
        default = {}
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default


def load_json_with_defaults(file_path, defaults):
    """Load JSON file, merging with defaults for missing keys.

    Args:
        file_path: Path to JSON file
        defaults: Dict of default values

    Returns:
        Dict with loaded data merged with defaults
    """
    if not os.path.exists(file_path):
        return defaults
    try:
        with open(file_path) as f:
            data = json.load(f)
        return {**defaults, **data}
    except (json.JSONDecodeError, IOError):
        return defaults


class FileLockContext:
    """Context manager for file locking.

    Usage:
        with FileLockContext(lock_path):
            # critical section
            pass
    """

    def __init__(self, lock_file_path):
        self.lock_file_path = lock_file_path
        self.lock_file = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_file_path), exist_ok=True)
        self.lock_file = open(self.lock_file_path, "w")
        if self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None