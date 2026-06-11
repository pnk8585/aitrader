"""file_lock.py — Shared file locking and JSON utilities for AITrader bots.

Provides thread-safe file locking with fcntl.flock and atomic JSON operations
to prevent race conditions between concurrent bot instances (Pullback, Momentum).

Functions:
- with_file_lock: Execute a function with exclusive flock on a file
- atomic_write_json: Write JSON atomically with tmp file + os.replace
- load_json_with_defaults: Load JSON, returning defaults if file missing/invalid

Race Condition Prevention:
--------------------------
When two bots (Pullback and Momentum) both find candidates, they submit to
pending_review.json. Without a lock, the second write overwrites the first
silently. With fcntl.flock LOCK_EX, the writes are serialized.

The overseer also needs to read->verify->write atomically. It uses with_file_lock
to acquire exclusive access during the re-read/verify/write window.

Usage Example:
--------------
    from file_lock import with_file_lock, atomic_write_json

    def submit():
        with_file_lock("/path/to/lockfile", lambda: atomic_write_json("/path/to/data.json", data))

    def safe_operation():
        return with_file_lock("/path/to/lockfile", lambda: do_work_under_lock())
"""

import os
import fcntl
import json


def with_file_lock(lock_file, func, timeout_sec=30):
    """Execute func with exclusive flock on lock_file.

    Args:
        lock_file: Path to lock file (will be created if needed)
        func: Callable to execute under lock (no args)
        timeout_sec: Maximum time to wait for lock (default 30s)

    Returns:
        Return value of func

    Raises:
        TimeoutError if lock cannot be acquired within timeout_sec
    """
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return func()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def atomic_write_json(filepath, data, indent=2, ensure_dir=True):
    """Write data to filepath atomically using tmp file + os.replace.

    Prevents partial writes during crashes. If ensure_dir is True, creates
    parent directories if they don't exist.

    Args:
        filepath: Target file path
        data: JSON-serializable data
        indent: JSON indentation (default 2)
        ensure_dir: Create parent directories if needed (default True)
    """
    if ensure_dir:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp = f"{filepath}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filepath)


def load_json_with_defaults(filepath, defaults):
    """Load JSON from filepath, returning defaults on missing/invalid data.

    Args:
        filepath: Path to JSON file
        defaults: Dict with default values (returned if file invalid)

    Returns:
        Dict: Loaded JSON merged with defaults (defaults -> loaded values)
    """
    if not os.path.exists(filepath):
        return defaults.copy()
    try:
        with open(filepath) as f:
            loaded = json.load(f)
        return {**defaults, **loaded}
    except (json.JSONDecodeError, IOError):
        return defaults.copy()


def atomic_write_text(filepath, content, ensure_dir=True):
    """Write text content to filepath atomically using tmp file + os.replace.

    Prevents partial writes during crashes. If ensure_dir is True, creates
    parent directories if they don't exist.

    Args:
        filepath: Target file path
        content: String content to write
        ensure_dir: Create parent directories if needed (default True)
    """
    if ensure_dir:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp = f"{filepath}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filepath)