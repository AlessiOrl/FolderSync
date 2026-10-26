"""Opt-in live check of all sample GPs without copying media bytes.

Run inside the built image with /app/tests mounted writable:
python tests/live_poster_check.py
"""
import json
import os
import sys
import tempfile
import shutil
import re
from contextlib import contextmanager
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import RecentLogs, SyncService
from sync import CONFIG_FILE, LinkSync, load_config


@contextmanager
def scratch_folder(parent):
    temporary = tempfile.mkdtemp(prefix=".foldersync-test-", dir=parent)
    try:
        yield temporary
    finally:
        # Windows media/antivirus readers can briefly hold newly created links.
        for attempt in range(20):
            try:
                shutil.rmtree(temporary)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(1)


def main():
    fixtures = Path(__file__).parent / "test_folder"
    videos = sorted(fixtures.glob("Season */*.mkv"))
    if not videos:
        raise RuntimeError("Expected GP sample videos")
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in fixtures.rglob("*") if path.is_file()}
    with scratch_folder(fixtures.parent) as temporary:
        root = Path(temporary)
        source, destination = root / "source", root / "destination"
        source.mkdir(); destination.mkdir()
        races = {}
        for video in videos:
            match = re.fullmatch(r"S(\d{2})(\d{2})E\d+ - (.+)\.mkv", video.name)
            if not match:
                raise RuntimeError(f"Unexpected sample name: {video.name}")
            yy, rr, title = match.groups()
            name = f"Formula1.20{yy}.Round{rr}.{title.replace(' ', '.')}.mkv"
            os.link(video, source / name)
            races[video.parent.name] = None
        config = load_config(CONFIG_FILE)
        service = SyncService(LinkSync(destination, source, config, stable_seconds=0), RecentLogs())
        try:
            result = service.scan_now()
            assert result["linked"] == len(videos) and result["nfo_created"] == len(videos), result
            deadline = time.monotonic() + 180
            while service.posters.active and time.monotonic() < deadline:
                time.sleep(.1)
            statuses = service.posters.status()
            assert len(statuses) == len(races), statuses
            for status in statuses:
                assert status["status"] == "downloaded", status
                folder = destination / status["folder"]
                poster = folder / status["file_path"]
                assert poster.is_file(), status
                races[status["folder"]] = (poster, poster.stat().st_mtime_ns)
            for original in videos:
                assert any(target.samefile(original) for target in (destination / original.parent.name).glob("*.mkv"))
            result = service.scan_now()
            assert result["already_linked"] == len(videos), result
            assert all(poster.stat().st_mtime_ns == stamp for poster, stamp in races.values())
            print(json.dumps({"scan": result, "posters": statuses}, indent=2))
        finally:
            service.posters.close()
    after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in before}
    assert before == after, "Sample files changed"
    print("Live automatic post-link download passed; sample files unchanged")


if __name__ == "__main__":
    main()
