"""Thread-safe file locking and atomic JSON/text writes."""

import fcntl
import json
import os


def with_file_lock(lock_file, func, timeout_sec=30):
    """Execute func with exclusive flock on lock_file."""
    os.makedirs(os.path.dirname(lock_file) or ".", exist_ok=True)
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return func()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def atomic_write_json(filepath, data, indent=2, ensure_dir=True):
    """Write JSON atomically via temp file + os.replace."""
    if ensure_dir:
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    tmp = f"{filepath}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filepath)


def atomic_write_text(filepath, content, ensure_dir=True):
    """Write text atomically via temp file + os.replace."""
    if ensure_dir:
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    tmp = f"{filepath}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filepath)


def load_json_safe(file_path, default=None):
    """Load JSON file with safe error handling."""
    if default is None:
        default = {}
    return load_json_with_defaults(file_path, default)


def load_json_with_defaults(filepath, defaults):
    """Load JSON, merging with defaults on missing/invalid data."""
    if not os.path.exists(filepath):
        return defaults.copy() if isinstance(defaults, dict) else defaults
    try:
        with open(filepath) as f:
            loaded = json.load(f)
        if isinstance(defaults, dict):
            return {**defaults, **loaded}
        return loaded
    except (json.JSONDecodeError, IOError):
        return defaults.copy() if isinstance(defaults, dict) else defaults


class FileLockContext:
    """Context manager for file locking."""

    def __init__(self, lock_file_path):
        self.lock_file_path = lock_file_path
        self.lock_file = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_file_path) or ".", exist_ok=True)
        self.lock_file = open(self.lock_file_path, "w")
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None