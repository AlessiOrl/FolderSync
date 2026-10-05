"""Find Formula 1 round posters on Reddit and save Plex season art."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from nfo import VIDEO_EXTENSIONS, parse_episode


SEASON_DIR = re.compile(r"Season (?P<code>\d{4})", re.I)
MEDIA_KIT = re.compile(
    r"^(?:F1 )?(?P<gp>.+?) Grand Prix(?: (?P<venue>In Malaysia))? "
    r"(?P<year>20\d{2}) \[(?:F1 )?Media Kit\]$", re.I,
)
IMAGE_HOSTS = frozenset({"i.redd.it", "preview.redd.it"})
USER_AGENT = "FolderSync/1.0 (Formula 1 season artwork)"
_token: tuple[str, str, float] | None = None


class PosterAuthError(ValueError):
    """Poster lookup cannot authenticate with Reddit."""


MAX_IMAGE_BYTES = 20 * 1024 * 1024
TITLE_ALIASES = {
    "australia": "australian",
    "china": "chinese",
    "japan": "japanese",
    "saudi arabia": "saudi arabian",
    "canada": "canadian",
    "spain": "spanish",
    "spain barcelona": "spanish",
    "great britain": "british",
    "belgium": "belgian",
    "hungary": "hungarian",
    "austria": "austrian",
    "netherlands": "dutch",
    "italy": "italian",
    "monza": "italian",
    "mexico": "mexico city",
    "emilia romagna": "emilia romagna",
}
TEAM_NAMES = frozenset({
    "mclaren", "red bull", "red bull racing", "oracle red bull racing",
    "racing bulls", "visa cash app racing bulls", "ferrari", "scuderia ferrari",
    "mercedes", "mercedes amg", "mercedes amg petronas", "williams",
    "williams racing", "alpine", "bwt alpine", "cadillac", "aston martin",
    "aston martin aramco", "audi", "haas", "moneygram haas", "sauber",
    "kick sauber",
})


@dataclass
class PosterResult:
    created: int = 0
    existing: int = 0
    missing: int = 0
    unparsed: int = 0
    deferred: int = 0
    planned: int = 0
    errors: int = 0


def _normal(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain.replace("-", " ").strip())


def _poster_gp(gp: str) -> str:
    normalized = _normal(gp)
    return TITLE_ALIASES.get(normalized, normalized)


def season_gp(folder: Path, code: str, grand_prix: tuple[str, ...]) -> str | None:
    """Infer a single GP from episode titles; mixed or unknown names are ambiguous."""
    names = set()
    for video in folder.iterdir():
        if video.suffix.casefold() not in VIDEO_EXTENSIONS or not video.is_file():
            continue
        parsed = parse_episode(video)
        if parsed is None or parsed[0] != int(code):
            continue
        title = _normal(parsed[2])
        matches = [gp for gp in grand_prix if title.startswith(_normal(gp) + " ")]
        if matches:
            gp = max(matches, key=lambda name: len(_normal(name)))
            # The 2026 Barcelona round has its own title, separate from Madrid's Spanish GP.
            if code == "2607" and _normal(gp) == "spain barcelona":
                names.add("barcelona catalunya")
            else:
                names.add(_poster_gp(gp))
    return next(iter(names)) if len(names) == 1 else None


def _image_url(post: dict) -> str | None:
    urls = []
    for field in ("url_overridden_by_dest", "url"):
        if isinstance(post.get(field), str):
            urls.append(post[field])
    gallery = post.get("gallery_data") or {}
    metadata = post.get("media_metadata") or {}
    for item in gallery.get("items", []):
        media = metadata.get(item.get("media_id"), {})
        source = media.get("s", {})
        if isinstance(source.get("u"), str):
            urls.append(source["u"])
    for value in urls:
        value = html.unescape(value)
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname in IMAGE_HOSTS:
            return value
    return None


def _matches(post: dict, gp: str, year: int) -> bool:
    if str(post.get("subreddit", "")).casefold() != "formula1posters":
        return False
    match = MEDIA_KIT.fullmatch(str(post.get("title", "")).strip())
    if not match or int(match["year"]) != year:
        return False
    candidate = _normal(match["gp"])
    expected = _poster_gp(gp)
    return candidate == expected and (match["venue"] is None or expected == "bahrain")


def _fallback_rank(post: dict, gp: str, year: int) -> int | None:
    """Rank official, FIA, then team artwork after Media Kit posts."""
    if str(post.get("subreddit", "")).casefold() != "formula1posters":
        return None
    title = str(post.get("title", "")).strip()
    expected = _poster_gp(gp)
    official = re.fullmatch(
        r"Official (?:(20\d{2}) )?(?:Formula 1 |F1 )?(.+?) Grand Prix"
        r"(?: (20\d{2}))? Poster(?: \[(\d+)x(\d+)\])?", title, re.I,
    )
    if official and _normal(official[2]) == expected:
        before, after = official[1], official[3]
        if (before or after) == str(year) and (not before or not after or before == after):
            return 0
    fia = re.fullmatch(r"FIA (.+?) Grand Prix (20\d{2})", title, re.I)
    if fia and _normal(fia[1]) == expected and int(fia[2]) == year:
        return 1
    fia = re.fullmatch(r"(.+?) Grand Prix (20\d{2}) \[FIA( Preview)?\]", title, re.I)
    if fia and _normal(fia[1]) == expected and int(fia[2]) == year:
        return 2 if fia[3] else 1
    team = re.fullmatch(
        r"(.+?) Grand Prix(?: (In Malaysia))? (20\d{2})(?: \[[^\]]+\])?", title, re.I,
    )
    if team and int(team[3]) == year and (team[2] is None or expected == "bahrain"):
        heading = _normal(team[1])
        if any(heading == f"{name} {expected}" for name in TEAM_NAMES):
            return 3
    return None


def _reddit_auth() -> tuple[str, str]:
    """Get an app-only OAuth token, caching it until shortly before expiry."""
    global _token
    client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    username = os.environ.get("REDDIT_USERNAME", "").strip().removeprefix("u/")
    if not client_id or not client_secret or not re.fullmatch(r"[A-Za-z0-9_-]+", username):
        raise PosterAuthError("Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and REDDIT_USERNAME for poster lookup")
    user_agent = f"python:foldersync.seasonposters:v1.0 (by /u/{username})"
    now = time.monotonic()
    if _token is not None and _token[0] == client_id and now < _token[2]:
        return _token[1], user_agent
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = Request(
        "https://www.reddit.com/api/v1/access_token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {credentials}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": user_agent, "Accept": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        payload = response.read(65_537)
    if len(payload) > 65_536:
        raise PosterAuthError("Reddit token response is too large")
    try:
        data = json.loads(payload)
    except ValueError as error:
        raise PosterAuthError("Reddit token response is invalid JSON") from error
    if not isinstance(data, dict):
        raise PosterAuthError("Reddit token response is invalid")
    token = data.get("access_token")
    expires_in = data.get("expires_in")
    if not isinstance(token, str) or not token or not isinstance(expires_in, int) or expires_in <= 0:
        raise PosterAuthError("Reddit did not return a usable OAuth token")
    _token = client_id, token, now + max(0, expires_in - 60)
    return token, user_agent


def _search_posts(query_text: str, sort: str, token: str, user_agent: str) -> list[dict]:
    query = urlencode({
        "q": query_text, "restrict_sr": "1", "sort": sort, "t": "all", "limit": "100",
    })
    request = Request(f"https://oauth.reddit.com/r/Formula1posters/search?{query}",
                      headers={"Authorization": f"bearer {token}",
                               "User-Agent": user_agent, "Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("Reddit search response is too large")
    return [child.get("data", {}) for child in json.loads(payload)["data"]["children"]]


def find_image(gp: str, year: int) -> str | None:
    """Prefer Media Kit, then official/FIA, then a team poster for the GP."""
    token, user_agent = _reddit_auth()
    words = f'{_poster_gp(gp)} "Grand Prix" "{year}"'
    media_posts = _search_posts(f'{words} "Media Kit"', "new", token, user_agent)
    for post in media_posts:
        if _matches(post, gp, year):
            image = _image_url(post)
            if image:
                return image
    fallback_posts = _search_posts(words, "relevance", token, user_agent)
    ranked = []
    for post in fallback_posts:
        rank = _fallback_rank(post, gp, year)
        image = _image_url(post) if rank is not None else None
        if image:
            score = post.get("score", 0)
            ranked.append((rank, -score if isinstance(score, int) else 0, image))
    return min(ranked, default=(None, None, None))[2]


def has_poster(folder: Path, code: str) -> bool:
    names = {f"season{code}.{ext}" for ext in ("jpg", "jpeg", "png", "tbn")}
    names.update(f"season{code}-poster.{ext}" for ext in ("jpg", "jpeg", "png", "tbn"))
    names.update(f"poster.{ext}" for ext in ("jpg", "jpeg", "png", "tbn"))
    return any(path.name.casefold() in names for path in folder.iterdir())


def _image_type(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def save_image(url: str, folder: Path, code: str) -> Path | None:
    """Download a JPEG or PNG with an exclusive final create; never replace artwork."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
        raise ValueError("Image URL must use Reddit image hosting")
    request = Request(url, headers={"User-Agent": USER_AGENT})
    temporary = None
    try:
        with urlopen(request, timeout=15) as response:
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname not in IMAGE_HOSTS:
                raise ValueError("Image redirected outside Reddit image hosting")
            if int(response.headers.get("Content-Length", "0")) > MAX_IMAGE_BYTES:
                raise ValueError("Poster image exceeds 20 MB")
            with tempfile.NamedTemporaryFile(dir=folder, prefix=".foldersync-poster-",
                                             delete=False) as output:
                temporary = Path(output.name)
                total = 0
                header = b""
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ValueError("Poster image exceeds 20 MB")
                    if len(header) < 8:
                        header += chunk[:8 - len(header)]
                    output.write(chunk)
        extension = _image_type(header)
        if extension is None:
            raise ValueError("Poster is not a JPEG or PNG image")
        target = folder / f"Season{code}.{extension}"
        if has_poster(folder, code):
            return None
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            return None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def scan(root: Path, grand_prix: tuple[str, ...], *, write: bool = False,
         report: Callable[[str], None] | None = None,
         should_attempt: Callable[[Path], bool] | None = None) -> PosterResult:
    """Fill missing season artwork under a destination tree."""
    result = PosterResult()
    emit = report or print

    def walk_error(error: OSError) -> None:
        result.errors += 1
        emit(f"ERROR poster scan: {error}")

    for base, directories, _files in os.walk(root, onerror=walk_error):
        directories[:] = sorted(name for name in directories if not (Path(base) / name).is_symlink())
        folder = Path(base)
        match = SEASON_DIR.fullmatch(folder.name)
        if not match:
            continue
        code = match["code"]
        year, round_number = 2000 + int(code[:2]), int(code[2:])
        if not 1 <= round_number <= 26:
            result.unparsed += 1
            emit(f"UNPARSED poster season: {folder} (round must be 01–26)")
            continue
        try:
            if has_poster(folder, code):
                result.existing += 1
                continue
            gp = season_gp(folder, code, grand_prix)
        except OSError as error:
            result.errors += 1
            emit(f"ERROR poster for {folder}: {error}")
            continue
        if gp is None:
            result.unparsed += 1
            emit(f"UNPARSED poster season: {folder} (no single recognized Grand Prix in episode names)")
            continue
        if should_attempt is not None and not should_attempt(folder):
            result.deferred += 1
            emit(f"DEFERRED poster lookup: {folder}")
            continue
        if not write:
            result.planned += 1
            emit(f"WOULD SEARCH {year} {gp}: {folder}")
            continue
        try:
            url = find_image(gp, year)
            if url is None:
                result.missing += 1
                emit(f"NO POSTER {year} {gp}: {folder}")
                continue
            path = save_image(url, folder, code)
        except (URLError, PosterAuthError) as error:
            result.errors += 1
            emit(f"ERROR poster for {folder}: {error}")
            break
        except (OSError, ValueError, KeyError, TypeError) as error:
            result.errors += 1
            emit(f"ERROR poster for {folder}: {error}")
        else:
            if path is None:
                result.existing += 1
            else:
                result.created += 1
                emit(f"CREATED {path}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media_dir", type=Path, nargs="?", default=Path("/data/f1"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show folders needing a poster (default)")
    mode.add_argument("--write", action="store_true", help="download missing posters")
    args = parser.parse_args(argv)
    if not args.media_dir.is_dir():
        parser.error(f"not a directory: {args.media_dir}")
    from sync import CONFIG_FILE, load_config
    result = scan(args.media_dir, load_config(CONFIG_FILE).grand_prix, write=args.write)
    print("Summary: " + " ".join(f"{key}={value}" for key, value in vars(result).items()))
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
