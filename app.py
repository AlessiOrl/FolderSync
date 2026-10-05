"""Local web interface and scheduler for FolderSync."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from settings import DirectorySettings
from sync import CONFIG_FILE, Config, LinkSync, load_config


LOG = logging.getLogger("folder_sync.web")
INDEX_PATH = Path(__file__).parent / "web" / "index.html"


class RecentLogs(logging.Handler):
    def __init__(self, capacity: int = 200):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def latest(self, limit: int = 60) -> list[str]:
        self.acquire()
        try:
            return list(self.lines)[-limit:]
        finally:
            self.release()


def list_files(root: Path) -> list[dict[str, object]]:
    files = []

    def walk_error(error: OSError) -> None:
        LOG.error("Cannot list files in %s: %s", root, error)

    for base, directories, filenames in os.walk(root, onerror=walk_error):
        directories[:] = sorted(
            (name for name in directories if not (Path(base) / name).is_symlink()),
            key=str.casefold,
        )
        for name in filenames:
            path = Path(base) / name
            try:
                metadata = path.lstat()
            except OSError as error:
                LOG.error("Cannot inspect %s: %s", path, error)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            files.append({"path": path.relative_to(root).as_posix(), "size": metadata.st_size})
    files.sort(key=lambda item: str(item["path"]).casefold())
    return files


class SyncService:
    def __init__(self, sync: LinkSync | None, logs: RecentLogs,
                 settings: DirectorySettings | None = None, config: Config | None = None,
                 interval: int = 120, stable_seconds: int = 10):
        self.sync = sync
        self.logs = logs
        self.settings = settings
        self.config = config if config is not None else (sync.config if sync else None)
        self.interval = interval
        self.stable_seconds = stable_seconds
        self.selected: dict[str, str] = {}
        self.scan_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.wake_event = threading.Event()
        self.last_scan_at: str | None = None
        self.last_result: dict[str, int] | None = None
        self.last_error: str | None = None

    def restore_settings(self) -> None:
        if self.settings is None:
            return
        try:
            saved = self.settings.read()
            if saved is not None:
                selected, destination, source = saved
                self.sync = LinkSync(destination, source, self.config, self.stable_seconds,
                                     registry_path=self.settings.root / ".foldersync-links.json")
                self.selected = selected
        except (OSError, ValueError, RuntimeError) as error:
            self.last_error = f"Saved folder settings need attention: {error}"
            LOG.error("%s", self.last_error)

    def configure(self, raw: object) -> dict[str, str]:
        if self.settings is None or self.config is None:
            raise ValueError("Directory setup is unavailable")
        if not self.scan_lock.acquire(blocking=False):
            raise RuntimeError("A scan or configuration check is running; try again shortly")
        try:
            selected, destination, source = self.settings.validate(raw)
            self.settings.check_link(source, destination)
            replacement = LinkSync(destination, source, self.config, self.stable_seconds,
                                     registry_path=self.settings.root / ".foldersync-links.json")
            self.settings.save(selected)
            with self.state_lock:
                self.sync = replacement
                self.selected = selected
                self.last_scan_at = None
                self.last_result = None
                self.last_error = None
            LOG.info("Folder settings saved: %s -> %s", source, destination)
            self.wake_event.set()
            return selected
        finally:
            self.scan_lock.release()

    def scan_now(self, pending_only: bool = False, *, retry_posters: bool = False) -> dict[str, int] | None:
        if not self.scan_lock.acquire(blocking=False):
            return None
        try:
            if self.sync is None:
                return None
            if pending_only and not self.sync.pending_ready():
                return None
            if retry_posters:
                result = self.sync.scan_once(pending_only=pending_only, retry_posters=True)
            else:
                result = self.sync.scan_once(pending_only=pending_only)
            summary = asdict(result)
            with self.state_lock:
                self.last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.last_result = summary
                self.last_error = self.sync.last_error
            self.wake_event.set()
            return summary
        except Exception as error:
            LOG.exception("Scan failed")
            with self.state_lock:
                self.last_error = str(error)
            raise
        finally:
            self.scan_lock.release()

    def status(self) -> dict[str, object]:
        with self.state_lock:
            sync = self.sync
            selected = dict(self.selected)
            last_scan_at = self.last_scan_at
            last_result = self.last_result
            last_error = self.last_error
        torrent_files = list_files(sync.torrent) if sync else []
        data_files = list_files(sync.data) if sync else []
        return {
            "configured": sync is not None,
            "data_root": str(self.settings.root) if self.settings else None,
            "directories": selected,
            "torrent_path": str(sync.torrent) if sync else "",
            "data_path": str(sync.data) if sync else "",
            "torrent_files": torrent_files,
            "data_files": data_files,
            "last_scan_at": last_scan_at,
            "last_result": last_result,
            "last_error": last_error,
            "scanning": self.scan_lock.locked(),
            "logs": self.logs.latest(),
        }


def make_handler(service: SyncService, index_html: bytes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send_body(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, code: int, value: dict[str, object]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_body(code, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self.send_body(200, index_html, "text/html; charset=utf-8")
            elif path == "/api/status":
                self.send_json(200, service.status())
            else:
                self.send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in ("/api/sync", "/api/config"):
                self.send_json(404, {"error": "Not found"})
                return
            # Custom headers prevent cross-origin HTML forms from changing state.
            action = "configure" if path == "/api/config" else "sync"
            if self.headers.get("X-FolderSync-Action") != action:
                self.send_json(403, {"error": "Missing action header"})
                return
            if path == "/api/config":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        raise ValueError("Settings body must contain 1–8192 bytes")
                    raw = json.loads(self.rfile.read(length))
                    selected = service.configure(raw)
                except RuntimeError as error:
                    self.send_json(409, {"error": str(error)})
                except (ValueError, OSError) as error:
                    self.send_json(400, {"error": str(error)})
                else:
                    self.send_json(200, {"directories": selected})
                return
            if service.sync is None:
                self.send_json(409, {"error": "Choose source and destination folders first"})
                return
            try:
                result = service.scan_now(retry_posters=True)
            except Exception:
                self.send_json(500, {"error": "Scan failed; see recent logs"})
                return
            if result is None:
                self.send_json(409, {"error": "A scan is already running"})
            else:
                self.send_json(200, {"result": result})

        def log_message(self, format: str, *args: object) -> None:
            LOG.debug("HTTP %s", format % args)

    return Handler


def run_scheduler(service: SyncService, interval: int) -> None:
    next_scan = time.monotonic()
    while True:
        try:
            now = time.monotonic()
            if now >= next_scan:
                service.scan_now()
                next_scan = time.monotonic() + interval
            elif service.sync is not None and service.sync.pending_due_at is not None and now >= service.sync.pending_due_at:
                service.scan_now(pending_only=True)
        except Exception:
            next_scan = time.monotonic() + interval
        with service.scan_lock:
            service.wake_event.clear()
            due_at = service.sync.pending_due_at if service.sync is not None else None
        wake_at = min(next_scan, due_at) if due_at is not None else next_scan
        service.wake_event.wait(max(0, wake_at - time.monotonic()))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logs = RecentLogs()
    logging.getLogger().addHandler(logs)
    try:
        config = load_config(CONFIG_FILE)
        settings = DirectorySettings(Path("/app/data"))
        interval = int(os.environ.get("SCAN_INTERVAL_SECONDS", "120"))
        stable_seconds = int(os.environ.get("STABLE_SECONDS", "10"))
        if stable_seconds < 1:
            raise ValueError("STABLE_SECONDS must be at least 1")
        port = int(os.environ.get("UI_PORT", "8080"))
        if interval < 1:
            raise ValueError("SCAN_INTERVAL_SECONDS must be at least 1")
        if not 1 <= port <= 65535:
            raise ValueError("UI_PORT must be between 1 and 65535")
        index_html = INDEX_PATH.read_bytes()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        LOG.error("Startup configuration error: %s", error)
        raise SystemExit(1) from error

    service = SyncService(None, logs, settings, config, interval, stable_seconds)
    service.restore_settings()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service, index_html))
    threading.Thread(target=run_scheduler, args=(service, interval), daemon=True).start()
    LOG.info("FolderSync UI listening on port %d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
