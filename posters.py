"""On-demand GP poster lookup from organizers and public artwork galleries."""
from __future__ import annotations

import http.client
import io
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from permissions import apply_permissions

from PIL import Image
from pypdf import PdfReader

LOG = logging.getLogger("folder_sync.posters")
LIMIT = 20 * 1024 * 1024
ALIASES = {"monza": "italy", "spain barcelona": "barcelona-catalunya"}
EVENT_NAMES = {
    "azerbaijan": ("azerbaijan", "baku", "azgp"),
    "bahrain": ("bahrain", "sakhir"),
    "singapore": ("singapore",),
    "italy": ("italy", "italian", "monza", "italia"),
    "great britain": ("great britain", "british", "silverstone"),
    "netherlands": ("netherlands", "dutch", "zandvoort"),
    "china": ("china", "chinese"), "canada": ("canada", "canadian"),
    "austria": ("austria", "austrian"), "belgium": ("belgium", "belgian"),
    "hungary": ("hungary", "hungarian"), "japan": ("japan", "japanese"),
    "united states": ("united states", "united states grand prix", "austin"),
    "australia": ("australia", "australian", "melbourne"),
    "mexico": ("mexico", "mexican"),
    "spain": ("spain", "spanish", "madrid"),
    "são paulo": ("são paulo", "sao paulo", "brazil", "brazilian"),
}
UNRELATED = re.compile(r"\b(shop|store|product|template|contest|super friday|fan festival)\b", re.I)


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTP(S) URL without credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Local and private network URLs are not allowed")
    return parsed, addresses[0][4][0], port


def fetch(url):
    """Pin the validated address; revalidate every redirect, without proxies."""
    for redirect in range(6):
        parsed, address, port = public_url(url)
        for attempt in range(2):
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=10)
            try:
                connection.sock = socket.create_connection((address, port), timeout=10)
                if parsed.scheme == "https":
                    connection.sock = ssl.create_default_context().wrap_socket(
                        connection.sock, server_hostname=parsed.hostname)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request("GET", path, headers={"User-Agent": "FolderSync/1.0", "Accept-Encoding": "identity"})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("Redirect has no location")
                    url = urljoin(url, location)
                    break
                if response.status == 429 or response.status >= 500:
                    raise OSError(f"HTTP {response.status}")
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                if int(response.getheader("Content-Length", "0")) > LIMIT:
                    raise ValueError("Response exceeds 20 MB")
                body = response.read(LIMIT + 1)
                if len(body) > LIMIT:
                    raise ValueError("Response exceeds 20 MB")
                return body, url
            except (OSError, http.client.HTTPException):
                if attempt:
                    raise
                time.sleep(0.5)
            finally:
                connection.close()
    raise ValueError("Too many redirects")


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.images = []
        self.links = []
        self.words = []
        self.headings = []
        self.heading = None
        self.hidden = 0
        self.anchor = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("title", "h1", "h2"):
            self.heading = ""
        if tag == "img":
            self.images.append((attrs.get("data-src") or attrs.get("src", ""), attrs.get("alt", "")))
        elif tag == "a":
            self.anchor = [attrs.get("href", ""), attrs.get("title", "")]

    def handle_data(self, data):
        if self.hidden:
            return
        self.words.append(data)
        if self.heading is not None:
            self.heading += data
        if self.anchor is not None:
            self.anchor[1] += " " + data

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("title", "h1", "h2") and self.heading is not None:
            self.headings.append(self.heading)
            self.heading = None
        if tag == "a" and self.anchor is not None:
            self.links.append(tuple(self.anchor))
            self.anchor = None


def canonical(name):
    name = name.casefold()
    return ALIASES.get(name, name)


def drive_download(url, year=None):
    """Resolve a publicly linked Drive file through its normal download endpoint."""
    parsed = urlsplit(url)
    match = re.fullmatch(r"/file/d/([A-Za-z0-9_-]+)(?:/view)?/?", parsed.path)
    if parsed.hostname != "drive.google.com" or not match:
        return url
    if year is not None:
        body, _ = fetch(url)
        title = " ".join(Page(body.decode("utf-8", errors="replace")).headings)
        if str(year) not in title or "media kit" not in title.casefold():
            raise ValueError("Linked media kit does not identify the requested year")
    return f"https://drive.google.com/uc?export=download&id={match.group(1)}"


def discover_media_kit(year, gp, sources):
    starts = next((urls for name, urls in sources.items() if canonical(name) == canonical(gp)), [])
    candidates = {}
    errors = []
    names = EVENT_NAMES.get(canonical(gp), (gp.casefold(), canonical(gp)))
    for start in starts:
        try:
            body, actual = fetch(start)
            if urlsplit(actual).hostname != urlsplit(start).hostname:
                continue
            page = Page(body.decode("utf-8", errors="replace"))
            headings = " ".join(page.headings).casefold()
            if str(year) not in headings or not any(name in headings for name in names) or "media kit" not in headings:
                continue
            for href, label in page.links:
                if not re.search(r"download|media kit", label, re.I):
                    continue
                target = urljoin(actual, href)
                parsed = urlsplit(target)
                if parsed.hostname == "drive.google.com" and parsed.path.startswith("/file/d/"):
                    target = drive_download(target, year)
                elif not re.search(r"\.pdf(?:\?|$)", target, re.I) or str(year) not in target:
                    continue
                candidates[target] = actual
        except Exception as error:
            errors.append(str(error))
    if len(candidates) > 1:
        return None, None, "ambiguous", "Multiple media kits found; supply a direct URL"
    if candidates:
        asset, source = next(iter(candidates.items()))
        return asset, source, "found", ""
    return None, None, "not_found", "; ".join(errors) or "No matching public media kit"


def cover_image(content):
    """Keep the dominant embedded cover image's bytes, with bounded PDF parsing."""
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted or not 0 < len(reader.pages) <= 200:
        raise ValueError("Media kit must be an unencrypted PDF with at most 200 pages")
    page = reader.pages[0]
    candidates = []
    # Inspect dimensions before decoding so oversized embedded assets are rejected.
    for name, reference in page.get("/Resources", {}).get("/XObject", {}).items():
        obj = reference.get_object()
        if obj.get("/Subtype") != "/Image":
            continue
        width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
        if width * height > 20_000_000:
            raise ValueError("Media-kit cover image is too large")
        if width >= 600 and height > width:
            candidates.append((width * height, str(name)))
    if not candidates:
        raise ValueError("PDF has no suitable embedded portrait cover image")
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[1][0] > candidates[0][0] * .8:
        raise ValueError("PDF cover contains competing portrait images")
    return page.images[candidates[0][1]].data


def validate_image(content):
    with Image.open(io.BytesIO(content)) as image:
        width, height = image.size
        extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(image.format)
        if not extension or width < 600 or height <= width or width * height > 20_000_000:
            raise ValueError("Poster must be JPEG, PNG or WebP, portrait, at least 600px wide")
        image.verify()
    with Image.open(io.BytesIO(content)) as image:
        image.load()
    return extension


def discover_gallery(year, gp, galleries):
    """Find a matching collection, then use its first valid poster in page order."""
    names = EVENT_NAMES.get(canonical(gp), (canonical(gp),))
    starts = list(galleries.get("*", []))
    starts += next((urls for name, urls in galleries.items() if canonical(name) == canonical(gp)), [])
    errors = []

    def identity(label):
        label = re.sub(r"[-_+]", " ", label.casefold())
        years = re.findall(r"\b20\d{2}\b", label)
        return (str(year) in years and all(value == str(year) for value in years)
                and any(re.search(r"\b" + re.escape(name) + r"\b", label) for name in names)
                and "poster" in label and not UNRELATED.search(label))

    for start in dict.fromkeys(starts):
        pages, seen, checked_assets = [start], set(), set()
        for url in pages:
            if url in seen or len(seen) >= 4:
                continue
            seen.add(url)
            try:
                body, actual = fetch(url)
                if urlsplit(actual).hostname != urlsplit(start).hostname:
                    continue
                page = Page(body.decode("utf-8", errors="replace"))
                # A collection's own title must identify the race/year. Index headings
                # describe other articles and cannot establish image identity.
                matching_page = bool(page.headings and identity(page.headings[0]))
                for href, label in page.links:
                    target = urljoin(actual, href)
                    if (identity(label) or identity(urlsplit(target).path)) and urlsplit(target).hostname == urlsplit(start).hostname and len(pages) < 4:
                        pages.append(target)
                for src, alt in page.images:
                    label = alt + " " + urlsplit(src).path
                    if not src or not matching_page or "poster" not in label.casefold() or UNRELATED.search(label):
                        continue
                    years = re.findall(r"\b20\d{2}\b", label)
                    if any(value != str(year) for value in years):
                        continue
                    target = urljoin(actual, src)
                    if target in checked_assets or len(checked_assets) >= 8:
                        continue
                    checked_assets.add(target)
                    try:
                        content, final = fetch(target)
                        validate_image(content)
                        return final, actual, "found", ""
                    except Exception as error:
                        errors.append(f"Poster image: {error}")
            except Exception as error:
                errors.append(f"Gallery: {error}")
    return None, None, "not_found", "; ".join(errors) or "No matching GP/year poster in public galleries"


def discover(year, gp, sources, media_kits=None, galleries=None):
    starts = next((urls for name, urls in sources.items() if canonical(name) == canonical(gp)), [])
    candidates = {}
    errors = []
    for start in starts:
        pages = [start]
        seen = set()
        for url in pages:
            if url in seen or len(seen) >= 6:
                continue
            seen.add(url)
            try:
                body, actual = fetch(url)
                if urlsplit(actual).hostname != urlsplit(start).hostname:
                    continue
                page = Page(body.decode("utf-8", errors="replace"))
                names = EVENT_NAMES.get(canonical(gp), (gp.casefold(), canonical(gp)))
                def identity(label):
                    return str(year) in label and any(name in label for name in names) and "poster" in label and not UNRELATED.search(label)
                # Identity must come from the official page, not just its URL.
                matches = any(identity(heading.casefold()) for heading in page.headings)
                for href, label in page.links:
                    target = urljoin(actual, href)
                    if "poster" in (label + " " + href).casefold() and str(year) in (label + " " + href):
                        if urlsplit(target).hostname == urlsplit(start).hostname and len(pages) < 6:
                            pages.append(target)
                    if (matches or identity(label.casefold())) and "poster" in label.casefold() and not UNRELATED.search(label + " " + target) and re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$)", target, re.I):
                        if re.search(r"20\d{2}", label) and str(year) not in label:
                            continue
                        candidates[target] = actual
                for src, alt in page.images:
                    if src and (matches or identity(alt.casefold())) and "poster" in alt.casefold() and any(name in alt.casefold() for name in names) and not UNRELATED.search(alt + " " + src):
                        if re.search(r"20\d{2}", alt) and str(year) not in alt:
                            continue
                        candidates[urljoin(actual, src)] = actual
            except Exception as error:
                errors.append(str(error))
            time.sleep(0.1)
    if candidates:
        asset, source = next(iter(candidates.items()))
        return asset, source, "found", ""
    if media_kits:
        fallback = discover_media_kit(year, gp, media_kits)
        if fallback[2] != "not_found":
            return fallback
        errors.append(fallback[3])
    if galleries:
        fallback = discover_gallery(year, gp, galleries)
        if fallback[2] == "found":
            return fallback
        errors.append(fallback[3])
    if not starts and not media_kits and not galleries:
        errors.append(f"No poster sources configured for {gp}")
    return None, None, "not_found", "; ".join(errors) or "No matching GP/year poster on configured pages"


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        apply_permissions(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def season_prefix(directory):
    match = re.fullmatch(r"Season (\d{4})", directory.name, re.I)
    if not match:
        raise ValueError("Poster destination must be a Season YYRR folder")
    return f"Season{match.group(1)}"


def plex_existing(directory):
    prefix = season_prefix(directory).casefold()
    wanted = {f"{prefix}.{ext}" for ext in ("jpg", "jpeg", "png", "tbn")}
    return next((path for path in sorted(directory.iterdir())
                 if path.name.casefold() in wanted and path.is_file() and not path.is_symlink()), None)


def publish_image(directory, name, content):
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".poster-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        apply_permissions(temporary)
        try:
            os.link(temporary, directory / name)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)


def plex_content(content):
    with Image.open(io.BytesIO(content)) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError("Existing poster is too large")
        if image.format in ("JPEG", "PNG"):
            extension = "jpg" if image.format == "JPEG" else "png"
            image.verify()
            return content, extension
        if image.format != "WEBP":
            raise ValueError("Existing poster must be JPEG, PNG or WebP")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue(), "png"


def prepare_existing(directory):
    """Add Plex season artwork without changing a legacy original or custom art."""
    target = plex_existing(directory)
    if target is not None:
        return target
    legacy = next((directory / f"poster.{ext}" for ext in ("jpg", "jpeg", "png", "webp")
                   if os.path.lexists(directory / f"poster.{ext}")), None)
    if legacy is None:
        return None
    if legacy.is_symlink() or not legacy.is_file() or legacy.stat().st_size > LIMIT:
        raise ValueError("Existing poster is not a safe regular image file")
    content, extension = plex_content(legacy.read_bytes())
    name = f"{season_prefix(directory)}.{extension}"
    publish_image(directory, name, content)
    sidecar = directory / "poster.json"
    if sidecar.is_file() and not sidecar.is_symlink():
        record = json.loads(sidecar.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record.update(file_path=name, original_file_path=legacy.name)
            atomic_json(sidecar, record)
    return directory / name


class PosterWorker:
    def __init__(self, config):
        self.enabled = config.get("enabled", True)
        self.config_require_poster = config.get("require_before_link", False)
        self.sources = config.get("sources", {})
        self.media_kits = config.get("media_kits", {})
        self.galleries = config.get("galleries", {})
        self.lock = threading.RLock()
        self.jobs = queue.Queue()
        self.root = None
        self.generation = 0
        self.races = {}
        self.active = set()
        self.stopped = False
        self.thread = None

    def configure(self, root):
        with self.lock:
            self.root = root.resolve()
            self.generation += 1
            self.races.clear()
            self.active.clear()

    def observe(self, directory, year, gp):
        with self.lock:
            if self.root is None:
                return
            key = directory.relative_to(self.root).as_posix()
            self.races.setdefault(key, {"folder": key, "year": year, "grand_prix": gp, "status": "pending"})
            if self.enabled or self.existing(directory):
                self.submit(key)

    def backfill(self):
        """Migrate saved artwork even when the original source videos are gone."""
        if self.root is None:
            return
        for base, directories, files in os.walk(self.root):
            directories[:] = [name for name in directories if not (Path(base) / name).is_symlink()]
            directory = Path(base)
            if not re.fullmatch(r"Season \d{4}", directory.name, re.I) or "poster.json" not in files:
                continue
            try:
                if (directory / "poster.json").is_symlink():
                    continue
                if not directory.resolve().is_relative_to(self.root):
                    continue
                saved = json.loads((directory / "poster.json").read_text(encoding="utf-8"))
                year, gp = saved.get("year"), saved.get("grand_prix")
                if isinstance(year, int) and 2000 <= year <= 2099 and isinstance(gp, str) and gp:
                    if int(directory.name[-4:-2]) == year % 100 and self.existing(directory):
                        self.observe(directory, year, gp)
            except (OSError, ValueError, AttributeError) as error:
                LOG.warning("Cannot prepare Plex artwork in %s: %s", directory, error)

    def _directory(self, key):
        directory = (self.root / key).resolve(strict=True)
        if directory == self.root or not directory.is_relative_to(self.root) or not directory.is_dir():
            raise ValueError("GP folder is outside the selected destination")
        return directory

    @staticmethod
    def existing(directory):
        return plex_existing(directory) is not None or any(
            path.is_file() and not path.is_symlink()
            for path in (directory / f"poster.{ext}" for ext in ("jpg", "jpeg", "png", "webp")))

    def submit(self, key, asset=None, retry=False):
        with self.lock:
            if self.stopped or key not in self.races:
                raise ValueError("Choose a GP folder detected during syncing")
            directory = self._directory(key)
            if self.existing(directory):
                self.races[key]["status"] = "existing"
                try:
                    target = prepare_existing(directory)
                    self.races[key].update(file_path=target.name, reason="")
                    sidecar = directory / "poster.json"
                    if sidecar.is_file() and not sidecar.is_symlink():
                        saved = json.loads(sidecar.read_text(encoding="utf-8"))
                        if isinstance(saved, dict):
                            self.races[key].update({field: saved[field] for field in
                                ("source_url", "asset_url", "asset_kind", "original_file_path") if field in saved})
                except (OSError, ValueError) as error:
                    self.races[key]["reason"] = f"Existing artwork kept; Plex preparation failed: {error}"
                return
            if key in self.active:
                return
            if self.races[key]["status"] in ("existing", "downloaded"):
                self.races[key].update(status="pending", reason="")
                self.races[key].pop("file_path", None)
            if not retry and self.races[key]["status"] != "pending":
                return
            sidecar = directory / "poster.json"
            if sidecar.exists() and not retry:
                try:
                    if sidecar.is_symlink():
                        raise ValueError("Poster metadata must not be a symlink")
                    saved = json.loads(sidecar.read_text(encoding="utf-8"))
                    self.races[key].update({field: saved[field] for field in ("status", "reason", "source_url", "asset_url") if field in saved})
                    if saved.get("status") in ("downloaded", "existing"):
                        self.races[key].update(status="failed", reason="Saved poster is missing; retry manually")
                except (OSError, ValueError, TypeError):
                    self.races[key].update(status="failed", reason="Invalid poster.json; retry manually")
                return
            self.races[key]["status"] = "queued"
            self.active.add(key)
            self.jobs.put((self.generation, key, asset, dict(self.races[key])))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="posters")
                self.thread.start()

    def status(self):
        with self.lock:
            for key, record in self.races.items():
                if record["status"] in ("existing", "downloaded"):
                    try:
                        present = self.existing(self._directory(key))
                    except (OSError, ValueError):
                        present = False
                    if not present:
                        record.update(status="failed", reason="Saved poster is missing; retry manually")
                        record.pop("file_path", None)
            return [dict(value) for value in self.races.values()]

    def ready(self, directory):
        """LinkSync gives missing artwork up to one minute to arrive."""
        with self.lock:
            return (plex_existing(directory) is not None
                    or (not self.enabled and not self.config_require_poster))

    def close(self):
        with self.lock:
            self.stopped = True
            self.generation += 1
        self.jobs.put(None)
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            generation, key, asset, record = job
            try:
                record["last_checked"] = datetime.now(timezone.utc).isoformat()
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.races[key]["status"] = "downloading"
                source = asset
                if not asset:
                    asset, source, status, reason = discover(record["year"], record["grand_prix"], self.sources, self.media_kits, self.galleries)
                    record.update(status=status, reason=reason)
                record.update(source_url=source, asset_url=asset, last_checked=datetime.now(timezone.utc).isoformat())
                content = None
                if asset:
                    asset = drive_download(asset)
                    content, final = fetch(asset)
                    if content.startswith(b"%PDF-"):
                        content = cover_image(content)
                        record["asset_kind"] = "media_kit_cover"
                    else:
                        record["asset_kind"] = "image"
                    extension = validate_image(content)
                    original_extension = extension
                    original_content = content
                    content, extension = plex_content(content)
                    record.update(status="downloaded", reason="", asset_url=final)
                with self.lock:
                    if generation != self.generation:
                        continue
                    directory = self._directory(key)
                    if self.existing(directory):
                        record["status"] = "existing"
                        record["file_path"] = prepare_existing(directory).name
                    elif content is not None:
                        record["file_path"] = f"{season_prefix(directory)}.{extension}"
                        if not publish_image(directory, record["file_path"], content):
                            record["status"] = "existing"
                        if original_extension == "webp" and record["status"] == "downloaded":
                            publish_image(directory, "poster.webp", original_content)
                            record["original_file_path"] = "poster.webp"
                    atomic_json(directory / "poster.json", record)
                    self.races[key].update(record)
            except Exception as error:
                LOG.warning("Poster for %s: %s", key, error)
                with self.lock:
                    if generation == self.generation:
                        record.update(status="failed", reason=str(error))
                        self.races[key].update(record)
                        try:
                            atomic_json(self._directory(key) / "poster.json", record)
                        except Exception:
                            LOG.exception("Cannot save poster outcome for %s", key)
            finally:
                with self.lock:
                    if generation == self.generation:
                        self.active.discard(key)
