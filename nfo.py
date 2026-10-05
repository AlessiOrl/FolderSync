"""Generate Plex episode NFO files beside conventionally named videos."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable
from pathlib import Path
from xml.sax.saxutils import escape


VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".mov"})
EPISODE_NAME = re.compile(r"^S(?P<season>[0-9]+)E(?P<episode>[0-9]+) - (?P<title>.+)$", re.I)


@dataclass
class Summary:
    scanned: int = 0
    parsed: int = 0
    created: int = 0
    existing: int = 0
    unparsed: int = 0
    planned: int = 0
    errors: int = 0


def parse_episode(path: Path) -> tuple[int, int, str] | None:
    """Extract season, episode and title from a video's basename."""
    match = EPISODE_NAME.fullmatch(path.stem)
    if match is None:
        return None
    title = match["title"]
    if not title.strip() or any(
        (ord(char) < 0x20 and char not in "\t\n\r")
        or 0xD800 <= ord(char) <= 0xDFFF
        or ord(char) in (0xFFFE, 0xFFFF)
        for char in title
    ):
        return None
    return int(match["season"]), int(match["episode"]), title


def episode_xml(season: int, episode: int, title: str) -> str:
    safe_title = escape(title, {'"': "&quot;", "'": "&apos;"})
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
        "<episodedetails>\n"
        f"    <title>{safe_title}</title>\n"
        f"    <season>{season}</season>\n"
        f"    <episode>{episode}</episode>\n"
        "</episodedetails>\n"
    )


def ensure_nfo(video: Path) -> str:
    """Create one missing episode NFO, returning created, existing, or unparsed."""
    parsed = parse_episode(video)
    if parsed is None:
        return "unparsed"
    nfo = video.with_suffix(".nfo")
    if os.path.lexists(nfo):
        return "existing"
    try:
        # Exclusive creation protects metadata written by another process.
        with nfo.open("x", encoding="utf-8", newline="\n") as output:
            output.write(episode_xml(*parsed))
    except FileExistsError:
        return "existing"
    return "created"


def scan(root: Path, *, write: bool = False,
         report: Callable[[str], None] | None = None) -> Summary:
    """Visit videos recursively and create missing NFO files when requested."""
    summary = Summary()

    def emit(message: str, *, error: bool = False) -> None:
        if report is None:
            print(message, file=sys.stderr if error else sys.stdout)
        else:
            report(message)

    def walk_error(error: OSError) -> None:
        summary.errors += 1
        emit(f"ERROR {error}", error=True)

    for directory, subdirs, filenames in os.walk(root, onerror=walk_error):
        subdirs.sort()
        for filename in sorted(filenames):
            video = Path(directory) / filename
            if video.suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            summary.scanned += 1
            parsed = parse_episode(video)
            if parsed is None:
                summary.unparsed += 1
                emit(f"UNPARSED {video}")
                continue
            summary.parsed += 1
            nfo = video.with_suffix(".nfo")
            if os.path.lexists(nfo):
                summary.existing += 1
                emit(f"EXISTS {nfo}")
                continue
            if not write:
                summary.planned += 1
                emit(f"WOULD CREATE {nfo}")
                continue
            try:
                status = ensure_nfo(video)
            except OSError as error:
                summary.errors += 1
                emit(f"ERROR {nfo}: {error}", error=True)
            else:
                if status == "created":
                    summary.created += 1
                    emit(f"CREATED {nfo}")
                elif status == "existing":
                    summary.existing += 1
                    emit(f"EXISTS {nfo}")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media_dir", type=Path, nargs="?", default=Path("/data/f1"),
                        help="media directory to scan (default: /data/f1)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show proposed NFOs (default)")
    mode.add_argument("--write", action="store_true", help="create missing NFOs")
    args = parser.parse_args(argv)

    if not args.media_dir.is_dir():
        parser.error(f"not a directory: {args.media_dir}")
    summary = scan(args.media_dir, write=args.write)
    print(
        f"Summary: scanned={summary.scanned} parsed={summary.parsed} "
        f"created={summary.created} existing={summary.existing} "
        f"unparsed={summary.unparsed} planned={summary.planned} errors={summary.errors}"
    )
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
