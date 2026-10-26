"""Persistent directory selection within the shared data mount."""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path

from permissions import apply_permissions
from sync import load_directories, relative_path


class DirectorySettings:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"Data mount is not a directory: {self.root}")
        self.path = self.root / ".foldersync.json"

    def validate(self, raw: object) -> tuple[dict[str, str], Path, Path]:
        if not isinstance(raw, dict):
            raise ValueError("Directory settings must be a JSON object")
        selected = {}
        paths = {}
        for key in ("source", "destination"):
            relative = relative_path(raw.get(key), key)
            path = (self.root / relative).resolve(strict=True)
            if path == self.root or not path.is_relative_to(self.root):
                raise ValueError(f"{key} must be a subfolder inside {self.root}")
            selected[key] = relative.as_posix()
            paths[key] = path
        destination, source = load_directories(paths["source"], paths["destination"])
        return selected, destination, source

    def read(self) -> tuple[dict[str, str], Path, Path] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return self.validate(json.loads(raw))

    def save(self, selected: dict[str, str]) -> None:
        # Replace atomically so an interrupted save leaves the old settings intact.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                         prefix=".foldersync-", delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(selected, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            apply_permissions(temporary)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def check_link(source: Path, destination: Path) -> None:
        # A real link detects separate mounts even when st_dev is identical.
        # The temporary directory owns all probe paths, including cleanup on failure.
        with tempfile.TemporaryDirectory(prefix=".foldersync-check-", dir=source) as origin:
            with tempfile.TemporaryDirectory(prefix=".foldersync-check-", dir=destination) as target:
                probe = Path(origin) / "probe"
                probe.write_bytes(b"FolderSync hard-link check\n")
                link = Path(target) / "probe"
                try:
                    os.link(probe, link)
                    if not probe.samefile(link):
                        raise ValueError("Storage did not create a hard link")
                except OSError as error:
                    if error.errno == errno.EXDEV:
                        raise ValueError(
                            "These folders cannot share hard links. Use one mount at /app/data "
                            "and choose folders on the same underlying filesystem."
                        ) from error
                    raise ValueError(f"Hard-link check failed: {error}") from error
