"""Permissions for published files and selected destination folders."""

import os
import stat
from pathlib import Path


def policy():
    """PGID enables management; Docker supplies the default host users GID."""
    raw = os.environ.get("PGID")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal() or not 0 <= int(raw) < 2**32 - 1:
        raise ValueError("PGID must be a numeric host group ID")
    modes = []
    for name, default in (("FILE_MODE", "0774"), ("DIRECTORY_MODE", "0774")):
        value = os.environ.get(name, default)
        if not value or any(char not in "01234567" for char in value) or int(value, 8) > 0o7777:
            raise ValueError(f"{name} must be an octal permission mode")
        modes.append(int(value, 8))
    return int(raw), *modes


def apply_permissions(path: Path):
    """Set root ownership and modes without following a final symlink."""
    selected = policy()
    if selected is None or path.is_symlink():
        return
    group, file_mode, directory_mode = selected
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            return
        mode = directory_mode if stat.S_ISDIR(info.st_mode) else file_mode
        if (info.st_uid, info.st_gid) != (0, group):
            os.fchown(descriptor, 0, group)
            # chown can clear special mode bits.
            os.fchmod(descriptor, mode)
        elif stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def repair_tree(root: Path):
    """Repair existing destination contents when selected or restored at startup."""
    if policy() is None:
        return
    apply_permissions(root)

    def walk_error(error):
        raise error

    for base, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
        directories[:] = [name for name in directories if not (Path(base) / name).is_symlink()]
        for name in directories + files:
            apply_permissions(Path(base) / name)
