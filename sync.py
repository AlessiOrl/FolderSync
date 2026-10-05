"""Add hard links for stable media files according to ordered path rules."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nfo import ensure_nfo, scan as scan_nfo
from poster import scan as scan_posters


LOG = logging.getLogger("folder_sync")
DEFAULT_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov")
CONFIG_FILE = Path(__file__).with_name("config.json")
SOURCE_ROOT = Path("/app/media/src")
DESTINATION_ROOT = Path("/app/media/dst")
DEFAULT_GRAND_PRIX = (
    "Australia", "China", "Japan", "Bahrain", "Saudi Arabia", "Miami", "Emilia-Romagna",
    "Monaco", "Barcelona-Catalunya", "Spain Barcelona", "Spain", "Canada", "Austria", "Great Britain", "Belgium",
    "Hungary", "Netherlands", "Italy", "Monza", "Azerbaijan", "Singapore",
    "United States", "Mexico", "São Paulo", "Las Vegas", "Qatar", "Abu Dhabi",
)
DEFAULT_EVENTS = (
    ("Post-Qualifying Show", "Post-Qualifying-Show"),
    ("Post Qualifying Show", "Post-Qualifying-Show"),
    ("Post-Race Show", "Post-Race-Show"),
    ("Post Race Show", "Post-Race-Show"),
    ("Pre-Race Show", "Pre-Race-Show"),
    ("Pre Race Show", "Pre-Race-Show"),
    ("Pre-Qualifying Show", "Pre-Qualifying-Show"),
    ("Post-Sprint Show", "Post-Sprint-Show"),
    ("Pre-Sprint Show", "Pre-Sprint-Show"),
    ("Paddock Uncut", "Paddock-Uncut"),
    ("Drivers Press Conference", "Drivers-Press-Conference"),
    ("Weekend Warm-Up", "Weekend-Warm-Up"),
    ("Sprint Qualifying", "Sprint-Qualifying"),
    ("Sprint Shootout", "Sprint-Shootout"),
    ("Practice 1", "Practice-1"),
    ("Practice 2", "Practice-2"),
    ("Practice 3", "Practice-3"),
    ("FP1", "Practice-1"),
    ("FP2", "Practice-2"),
    ("FP3", "Practice-3"),
    ("Qualification", "Qualify"),
    ("Qualifying", "Qualify"),
    ("Practice", "Practice"),
    ("Sprint", "Sprint"),
    ("Quali", "Qualify"),
    ("Qualify", "Qualify"),
    ("Race", "Race"),
)
F1_PREFIX = re.compile(r"^(?:Formula1[.\s_-]+)+(20\d{2})[.\s_-]+Round[.\s_-]*(\d+)[.\s_-]+(.+)$", re.I)
SEPARATOR = r"[.\s_-]+"


@dataclass(frozen=True)
class Rule:
    contains: str
    destination: Path


@dataclass(frozen=True)
class Config:
    extensions: frozenset[str]
    rules: tuple[Rule, ...]
    grand_prix: tuple[str, ...] = DEFAULT_GRAND_PRIX
    events: tuple[tuple[str, str], ...] = DEFAULT_EVENTS


@dataclass
class ScanResult:
    linked: int = 0
    pending: int = 0
    already_linked: int = 0
    skipped: int = 0
    failed: int = 0
    nfo_created: int = 0
    nfo_existing: int = 0
    nfo_unparsed: int = 0
    nfo_failed: int = 0
    posters_created: int = 0
    posters_existing: int = 0
    posters_missing: int = 0
    posters_unparsed: int = 0
    posters_deferred: int = 0
    posters_failed: int = 0


@dataclass(frozen=True)
class F1Name:
    code: str
    grand_prix: str
    event: str
    suffix: str

    def episode_name(self, number: int) -> str:
        return f"S{self.code}E{number:02d} - {self.grand_prix} {self.event}{self.suffix}"

    def old_name(self) -> str:
        gp = self.grand_prix.replace(" ", "-")
        event = ("Qualifying" if self.event == "Qualify" else self.event.replace(" ", "-"))
        return f"Round{int(self.code[2:]):02d} {gp} {event}{self.suffix}"


def relative_path(value: str, label: str) -> Path:
    """Accept a nonempty relative path without traversal or ambiguous components."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty relative path")
    while value.startswith("./"):
        value = value[2:]
    if value in ("", "."):
        return Path(".")
    if any(part != part.strip() for part in value.split("/")):
        raise ValueError(f"{label} has leading or trailing whitespace: {value!r}")
    if "\\" in value or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"{label} contains an invalid path component: {value!r}")
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"{label} must be relative: {value!r}")
    return path


def load_config(path: Path) -> Config:
    with path.open(encoding="utf-8") as file:
        raw = json.load(file)
    return parse_config(raw)



def parse_config(raw: object) -> Config:
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a JSON object")

    extensions = raw.get("extensions", DEFAULT_EXTENSIONS)
    if not isinstance(extensions, list | tuple) or not extensions:
        raise ValueError("extensions must be a nonempty list")
    for extension in extensions:
        if (
            not isinstance(extension, str)
            or not extension.startswith(".")
            or len(extension) < 2
            or not extension[1:].isalnum()
        ):
            raise ValueError(f"invalid extension: {extension!r}")

    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("rules must be a nonempty list")
    rules = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"rule {index} must be a JSON object")
        contains = raw_rule.get("contains")
        if not isinstance(contains, str) or not contains.strip():
            raise ValueError(f"rule {index} needs nonempty contains text")
        destination = relative_path(raw_rule.get("destination"), f"rule {index} destination")
        rules.append(Rule(contains.casefold(), destination))

    naming = raw.get("formula1", {})
    if not isinstance(naming, dict):
        raise ValueError("formula1 must be a JSON object")
    grand_prix = naming.get("grand_prix", DEFAULT_GRAND_PRIX)
    events = naming.get("events", dict(DEFAULT_EVENTS))
    if (not isinstance(grand_prix, (list, tuple)) or not grand_prix
            or any(not isinstance(name, str) or not name.strip() for name in grand_prix)):
        raise ValueError("formula1 grand_prix must be a nonempty list of names")
    if (not isinstance(events, dict) or not events
            or any(not isinstance(alias, str) or not alias.strip()
                   or not isinstance(name, str) or not name.strip()
                   for alias, name in events.items())):
        raise ValueError("formula1 events must map aliases to names")
    if any(re.search(r"[/\\.]", name) for name in (*grand_prix, *events.values())):
        raise ValueError("formula1 output names cannot contain path separators or dots")
    return Config(
        frozenset(extension.casefold() for extension in extensions), tuple(rules),
        tuple(grand_prix), tuple(events.items()),
    )



def _words_pattern(value: str) -> str:
    """Match a configured name with dot, space, underscore, or hyphen separators."""
    return SEPARATOR.join(re.escape(part) for part in re.split(r"[.\s_-]+", value.strip()) if part)


def normalized_f1_name(filename: str, config: Config) -> tuple[F1Name | None, str | None]:
    """Return destination title components, or a reason for an unparsed F1 name."""
    path = Path(filename)
    stem = path.stem
    if not re.match(r"^Formula1(?:[.\s_-]|$)", stem, re.I):
        return None, None
    match = F1_PREFIX.fullmatch(stem)
    if match is None:
        return None, "missing or invalid Formula 1 year/round"
    year, round_number, tail = match.groups()
    if not 1 <= int(round_number) <= 26:
        return None, "Formula 1 round must be between 01 and 26"
    for gp in sorted(config.grand_prix, key=len, reverse=True):
        gp_match = re.match(rf"^{_words_pattern(gp)}{SEPARATOR}(.+)$", tail, re.I)
        if gp_match is None:
            continue
        event_tail = gp_match.group(1)
        for alias, canonical in sorted(config.events, key=lambda item: len(item[0]), reverse=True):
            if re.match(rf"^{_words_pattern(alias)}(?:{SEPARATOR}|$)", event_tail, re.I):
                clean_gp = re.sub(r"[.\s_-]+", " ", gp.strip())
                clean_event = re.sub(r"[.\s_-]+", " ", canonical.strip())
                return F1Name(f"{year[-2:]}{int(round_number):02d}", clean_gp,
                              clean_event, path.suffix), None
        return None, f"unknown Formula 1 event after {gp}"
    return None, "unknown Formula 1 Grand Prix"


def load_directories(src_root: Path | None = None, dst_root: Path | None = None) -> tuple[Path, Path]:
    src_root = src_root if src_root is not None else Path(os.environ.get("SOURCE_DIR", str(SOURCE_ROOT)))
    dst_root = dst_root if dst_root is not None else Path(os.environ.get("DESTINATION_DIR", str(DESTINATION_ROOT)))
    source = src_root.resolve(strict=True)
    destination = dst_root.resolve(strict=True)
    for label, directory in (("source", source), ("destination", destination)):
        if not directory.is_dir():
            raise ValueError(f"{label} mount is not a directory: {directory}")
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("source and destination directories must not overlap")
    return destination, source


class LinkSync:
    def __init__(
        self,
        data: Path,
        torrent: Path,
        config: Config,
        stable_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
        registry_path: Path | None = None,
    ):
        self.data = data
        self.torrent = torrent
        self.config = config
        self.stable_seconds = stable_seconds
        self.clock = clock
        self.last_error: str | None = None
        self.pending_paths: set[Path] = set()
        self.pending_due_at: float | None = None
        self.previous: dict[Path, tuple[tuple[int, int, int, int], float]] = {}
        self.poster_attempted: dict[Path, float] = {}
        self.poster_retry_after = 0.0
        self.registry_path = registry_path or self.data.parent / ".foldersync-links.json"
        self.registry = self._load_registry()
        self.pair_key = json.dumps([str(self.torrent), str(self.data)], ensure_ascii=False)
        self.links: dict[str, str] = self.registry["pairs"].get(self.pair_key, {})

    def _load_registry(self) -> dict:
        try:
            with self.registry_path.open(encoding="utf-8") as file:
                value = json.load(file)
        except FileNotFoundError:
            return {"version": 1, "pairs": {}}
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("pairs"), dict)):
            raise ValueError(f"Invalid link registry: {self.registry_path}")
        for links in value["pairs"].values():
            if not isinstance(links, dict) or any(
                not isinstance(source, str) or not isinstance(destination, str)
                for source, destination in links.items()
            ):
                raise ValueError(f"Invalid link registry: {self.registry_path}")
        return value

    def _remember(self, source: Path, destination: Path) -> None:
        links = dict(self.links)
        links[source.as_posix()] = destination.relative_to(self.data).as_posix()
        registry = {"version": 1, "pairs": dict(self.registry["pairs"])}
        registry["pairs"][self.pair_key] = links
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.registry_path.parent,
                prefix=".foldersync-links-", delete=False,
            ) as file:
                temporary = Path(file.name)
                json.dump(registry, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.registry_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.registry = registry
        self.links = links

    @staticmethod
    def _same_inode(path: Path, signature: tuple[int, int, int, int]) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISREG(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == signature[:2]

    def _existing_link(self, directories: tuple[Path, ...], source_name: str,
                       signature: tuple[int, int, int, int],
                       title: F1Name | None) -> Path | None:
        names = [source_name]
        if title is not None:
            old_name = title.old_name()
            names.extend((old_name, f"Formula1 20{title.code[:2]} {old_name}"))
        for directory in directories:
            for name in names:
                candidate = directory / name
                if self._same_inode(candidate, signature):
                    return candidate
            if title is not None and directory.exists():
                legacy_episode = re.compile(rf"^{re.escape(title.code)}E\d+ - ")
                suffix = f" - {title.grand_prix} {title.event}{title.suffix}"
                for candidate in directory.iterdir():
                    if (legacy_episode.match(candidate.name) and candidate.name.endswith(suffix)
                            and self._same_inode(candidate, signature)):
                        return candidate
        return None

    @staticmethod
    def _episode_target(directory: Path, title: F1Name,
                        signature: tuple[int, int, int, int]) -> Path:
        prefix = re.compile(rf"^(S?){re.escape(title.code)}E(\d+) - ")
        suffix = f" - {title.grand_prix} {title.event}{title.suffix}"
        highest = 0
        legacy_number = None
        for path in directory.iterdir():
            if path.suffix.casefold() != title.suffix.casefold():
                continue
            match = prefix.match(path.name)
            if match is None:
                continue
            number = int(match.group(2))
            highest = max(highest, number)
            if path.name.endswith(suffix) and LinkSync._same_inode(path, signature):
                if match.group(1):
                    return path
                legacy_number = number
        if legacy_number is not None:
            candidate = directory / title.episode_name(legacy_number)
            if not os.path.lexists(candidate):
                return candidate
        return directory / title.episode_name(highest + 1)

    def _available_name(self, directory: Path, basename: str, source: Path,
                        signature: tuple[int, int, int, int], normalized: bool) -> Path | None:
        candidate = directory / basename
        if not os.path.lexists(candidate) or self._same_inode(candidate, signature):
            return candidate
        if not normalized:
            return None
        digest = hashlib.sha256(source.as_posix().encode("utf-8")).hexdigest()[:8]
        name = Path(basename)
        for number in range(1, 10000):
            extra = digest if number == 1 else f"{digest}-{number}"
            candidate = directory / f"{name.stem} - {extra}{name.suffix}"
            if not os.path.lexists(candidate) or self._same_inode(candidate, signature):
                return candidate
        return None

    def _sync_source(self, source: Path, relative: Path, directory: Path,
                     signature: tuple[int, int, int, int],
                     title: F1Name | None = None,
                     legacy_directory: Path | None = None) -> str:
        try:
            current = source.lstat()
            if (not stat.S_ISREG(current.st_mode) or
                    (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != signature):
                LOG.info("Source changed during scan; waiting: %s", source)
                return "pending"

            if title is None:
                title, reason = normalized_f1_name(source.name, self.config)
                if reason:
                    LOG.warning("Cannot normalize %s: %s; using original name", relative, reason)
            normalized = title is not None
            saved = self.links.get(relative.as_posix())
            if saved is not None:
                target = self.data / relative_path(saved, "saved destination")
                target_dir = self._destination_dir(Rule("", target.parent.relative_to(self.data)))
                if target_dir is None:
                    return "skipped"
                target = target_dir / target.name
                if os.path.lexists(target):
                    if not self._same_inode(target, signature):
                        LOG.warning("Saved destination conflicts with source; leaving untouched: %s", target)
                        return "skipped"
                    if not normalized:
                        return "already_linked"
                    new_target = self._episode_target(directory, title, signature)
                    if target == new_target:
                        return "already_linked"
                    if not self._same_inode(new_target, signature):
                        outcome = self._link(source, new_target, signature)
                        if outcome not in ("linked", "already_linked"):
                            return outcome
                    target.unlink()
                    self._remember(relative, new_target)
                    LOG.info("Renamed destination link %s -> %s", target, new_target)
                    return "already_linked"
                if not normalized:
                    outcome = self._link(source, target, signature)
                    if outcome == "linked":
                        LOG.info("Restored saved destination %s", target)
                    return outcome

            if normalized:
                target = self._episode_target(directory, title, signature)
                search_dirs = (directory, legacy_directory) if legacy_directory and legacy_directory != directory else (directory,)
                old = self._existing_link(search_dirs, source.name, signature, title)
            else:
                target = self._available_name(directory, source.name, relative, signature, False)
                old = self._existing_link((directory,), source.name, signature, None)
            if target is None:
                LOG.warning("Filename conflict; leaving both files untouched: %s", directory / source.name)
                return "skipped"
            if old is not None or self._same_inode(target, signature):
                old = old or target
                if old != target:
                    if not self._same_inode(target, signature):
                        outcome = self._link(source, target, signature)
                        if outcome not in ("linked", "already_linked"):
                            return outcome
                    old.unlink()
                    LOG.info("Renamed destination link %s -> %s", old, target)
                self._remember(relative, target)
                return "already_linked"

            outcome = self._link(source, target, signature)
            if outcome in ("linked", "already_linked"):
                self._remember(relative, target)
            return outcome
        except (OSError, ValueError) as error:
            self.last_error = f"Cannot sync {source}: {error}"
            LOG.error("%s", self.last_error)
            return "failed"

    def _destination_dir(self, rule: Rule) -> Path | None:
        candidate = self.data / rule.destination
        try:
            directory = candidate.resolve(strict=False)
            if (
                not directory.is_relative_to(self.data)
                or (directory == self.data and rule.destination != Path("."))
            ):
                LOG.warning("Destination folder is invalid or outside data: %s", candidate)
                return None
            directory.mkdir(parents=True, exist_ok=True)
            directory = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            self.last_error = (
                f"Cannot resolve destination folder {str(candidate)!r}: "
                f"{type(error).__name__}: {error}"
            )
            LOG.warning("%s", self.last_error)
            return None
        if (
            not directory.is_relative_to(self.data)
            or (directory == self.data and rule.destination != Path("."))
            or not directory.is_dir()
        ):
            LOG.warning("Destination folder is invalid or outside data: %s", candidate)
            return None
        return directory

    def _link(self, source: Path, destination: Path, signature: tuple[int, int, int, int]) -> str:
        try:
            if os.path.lexists(destination):
                existing = destination.lstat()
                if stat.S_ISREG(existing.st_mode) and (existing.st_dev, existing.st_ino) == signature[:2]:
                    return "already_linked"
                LOG.warning("Filename conflict; leaving both files untouched: %s", destination)
                return "skipped"

            # A source changed since this scan began must wait for another stable scan.
            current = source.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != signature
            ):
                LOG.info("Source changed during scan; waiting: %s", source)
                return "pending"

            os.link(source, destination, follow_symlinks=False)
            LOG.info("Linked %s -> %s", source, destination)
            return "linked"
        except FileExistsError:
            LOG.warning("Filename conflict; destination appeared during scan: %s", destination)
        except OSError as error:
            message = f"Cannot link {source} -> {destination}: {error}"
            if error.errno == errno.EXDEV:
                message += (
                    ". Hard links require the same mounted filesystem. "
                    "Mount the shared host parent once at /app/data and select "
                    "its source and destination subfolders in the web setup. "
                    "Both folders must be on the same underlying filesystem."
                )
            self.last_error = message
            LOG.error("%s", message)
            return "failed"
        return "skipped"

    def pending_ready(self) -> bool:
        return self.pending_due_at is not None and self.clock() >= self.pending_due_at

    def _sources(self):
        def walk_error(error: OSError) -> None:
            LOG.error("Cannot scan torrent directory: %s", error)

        for base, directories, filenames in os.walk(self.torrent, onerror=walk_error):
            directories[:] = sorted(
                name for name in directories if not (Path(base) / name).is_symlink()
            )
            for filename in sorted(filenames):
                yield Path(base) / filename

    def _nfo_for_link(self, relative: Path,
                      signature: tuple[int, int, int, int],
                      result: ScanResult) -> None:
        saved = self.links.get(relative.as_posix())
        if saved is None:
            return
        try:
            target = self.data / relative_path(saved, "saved destination")
            if not self._same_inode(target, signature):
                return
            status = ensure_nfo(target)
        except (OSError, ValueError) as error:
            result.nfo_failed += 1
            self.last_error = f"Cannot create episode NFO for {relative}: {error}"
            LOG.error("%s", self.last_error)
            return
        setattr(result, f"nfo_{status}", getattr(result, f"nfo_{status}") + 1)
        if status == "created":
            LOG.info("Created episode NFO %s", target.with_suffix(".nfo"))

    def _scan_posters(self, root: Path, result: ScanResult, *, retry: bool = False) -> None:
        def report_poster(message: str) -> None:
            if message.startswith("ERROR "):
                self.last_error = message
                LOG.error("%s", message)
            elif message.startswith(("CREATED ", "NO POSTER ")):
                LOG.info("%s", message)
            else:
                LOG.debug("%s", message)

        def should_attempt(folder: Path) -> bool:
            now = self.clock()
            if not retry and now < self.poster_retry_after:
                return False
            previous = self.poster_attempted.get(folder)
            if not retry and previous is not None and now - previous < 6 * 60 * 60:
                return False
            self.poster_attempted[folder] = now
            return True

        posters = scan_posters(root, self.config.grand_prix, write=True,
                               report=report_poster, should_attempt=should_attempt)
        result.posters_created += posters.created
        result.posters_existing += posters.existing
        result.posters_missing += posters.missing
        result.posters_unparsed += posters.unparsed
        result.posters_deferred += posters.deferred
        result.posters_failed += posters.errors
        if posters.errors:
            self.poster_retry_after = self.clock() + 6 * 60 * 60
        elif retry and (posters.created or posters.missing):
            self.poster_retry_after = 0.0

    def scan_once(self, pending_only: bool = False, *, retry_posters: bool = False) -> ScanResult:
        result = ScanResult()
        self.last_error = None
        current: dict[Path, tuple[tuple[int, int, int, int], float]] = {}
        if pending_only:
            current.update(self.previous)
        sources = [self.torrent / path for path in sorted(self.pending_paths)] if pending_only else self._sources()
        self.pending_paths = set()
        observed_at = self.clock()

        for source in sources:
            if pending_only:
                current.pop(source.relative_to(self.torrent), None)
                # A pending file's parent may have been replaced with a symlink.
                if any(
                    parent.is_symlink() for parent in source.parents
                    if parent != self.torrent and parent.is_relative_to(self.torrent)
                ):
                    result.skipped += 1
                    continue
            if source.suffix.casefold() not in self.config.extensions:
                continue
            try:
                metadata = source.lstat()
            except OSError as error:
                LOG.error("Cannot inspect %s: %s", source, error)
                result.skipped += 1
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue

            relative = source.relative_to(self.torrent)
            signature = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            previous = self.previous.get(relative)
            first_seen = previous[1] if previous and previous[0] == signature else observed_at
            current[relative] = (signature, first_seen)
            rule = next(
                (rule for rule in self.config.rules if rule.contains.casefold() in relative.as_posix().casefold()),
                None,
            )
            if rule is None:
                LOG.warning("No matching rule for %s", relative)
                result.skipped += 1
                continue
            title, _reason = normalized_f1_name(source.name, self.config)
            legacy_directory = self.data / rule.destination
            try:
                if not legacy_directory.resolve(strict=False).is_relative_to(self.data):
                    legacy_directory = None
            except (OSError, RuntimeError):
                legacy_directory = None
            destination_rule = rule
            if title is not None:
                base = (rule.destination.parent
                        if re.fullmatch(r"Season (?:20)?\d{2}", rule.destination.name, re.I)
                        else rule.destination)
                destination_rule = Rule(rule.contains, base / f"Season {title.code}")
            destination_dir = self._destination_dir(destination_rule)
            if destination_dir is None:
                result.skipped += 1
                continue
            if observed_at - first_seen < self.stable_seconds:
                result.pending += 1
                self.pending_paths.add(relative)
                continue
            outcome = self._sync_source(source, relative, destination_dir, signature,
                                        title, legacy_directory)
            setattr(result, outcome, getattr(result, outcome) + 1)
            if outcome == "pending":
                current[relative] = (signature, self.clock())
                self.pending_paths.add(relative)
            elif pending_only and outcome in ("linked", "already_linked"):
                self._nfo_for_link(relative, signature, result)
                if title is not None:
                    self._scan_posters(destination_dir, result)

        if not pending_only:
            def report_nfo(message: str) -> None:
                if message.startswith("ERROR "):
                    self.last_error = message
                    LOG.error("%s", message)
                elif message.startswith(("CREATED ", "UNPARSED ")):
                    LOG.info("%s", message)
                else:
                    LOG.debug("%s", message)

            nfo_result = scan_nfo(self.data, write=True, report=report_nfo)
            result.nfo_created = nfo_result.created
            result.nfo_existing = nfo_result.existing
            result.nfo_unparsed = nfo_result.unparsed
            result.nfo_failed = nfo_result.errors
            self._scan_posters(self.data, result, retry=retry_posters)

        self.pending_due_at = min(
            (current[path][1] + self.stable_seconds for path in self.pending_paths),
            default=None,
        )
        self.previous = current
        LOG.info(
            "Scan complete: linked=%d pending=%d already_linked=%d skipped=%d failed=%d "
            "nfo_created=%d nfo_existing=%d nfo_unparsed=%d nfo_failed=%d "
            "posters_created=%d posters_existing=%d posters_missing=%d "
            "posters_unparsed=%d posters_deferred=%d posters_failed=%d",
            result.linked,
            result.pending,
            result.already_linked,
            result.skipped,
            result.failed,
            result.nfo_created,
            result.nfo_existing,
            result.nfo_unparsed,
            result.nfo_failed,
            result.posters_created,
            result.posters_existing,
            result.posters_missing,
            result.posters_unparsed,
            result.posters_deferred,
            result.posters_failed,
        )
        return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(CONFIG_FILE)
        data, torrent = load_directories()
        interval = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))
        stable_seconds = int(os.environ.get("STABLE_SECONDS", "120"))
        if stable_seconds < 1:
            raise ValueError("STABLE_SECONDS must be at least 1")
        if interval < 1:
            raise ValueError("SCAN_INTERVAL_SECONDS must be at least 1")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        LOG.error("Startup configuration error: %s", error)
        raise SystemExit(1) from error

    LOG.info("Scanning %s into %s every %d seconds", torrent, data, interval)
    sync = LinkSync(data, torrent, config, stable_seconds=stable_seconds)
    next_scan = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= next_scan:
            sync.scan_once()
            next_scan = time.monotonic() + interval
        elif sync.pending_due_at is not None and now >= sync.pending_due_at:
            sync.scan_once(pending_only=True)
        wake_at = min(next_scan, sync.pending_due_at) if sync.pending_due_at is not None else next_scan
        time.sleep(max(0, wake_at - time.monotonic()))


if __name__ == "__main__":
    main()
