# FolderSync

FolderSync scans a source folder and creates hard links to matching video files in a destination folder. It includes a local web interface for status, logs, and manual scans.

## Configure matching rules

Edit `config.json` before building the image. The app reads it at `/app/config.json` inside the container:

```json
{
  "extensions": [".mp4", ".mkv", ".avi", ".mov"],
  "rules": [
    {"contains": "2026", "destination": "."}
  ]
}
```

A rule's `destination` is relative to the destination folder selected in the web page; `"."` means that folder itself. Recognized Formula 1 files go into a `Season YYRR` subfolder (for example, `Season 2601`). If a rule names an older year folder such as `Season 26` or `Season 2026`, the round folder is created beside that year folder so existing configurations keep working. Other files use the rule destination unchanged. A leading `./` is accepted but unnecessary. Rules are checked in order, and `contains` matches the relative source path without case sensitivity.

## Run with Docker

Create the source and destination folders on your host, for example `data/source` and `data/destination`. Build the image:

```text
docker build -t foldersync .
```

From Windows PowerShell or Command Prompt, mount the shared `data` parent **once**:

```text
docker run -d --name foldersync --restart unless-stopped -p 127.0.0.1:8082:8080 -v "H:\Data\Progetti_Info\FolderSync\data:/app/data" -e SCAN_INTERVAL_SECONDS=120 -e STABLE_SECONDS=10 foldersync
```

Use your actual host path. On Linux or WSL, replace the Windows path with `/mnt/h/Data/Progetti_Info/FolderSync/data` or the appropriate Linux path.

## Choose folders in the web page

1. Open <http://localhost:8082>.
2. Enter `source` as the **Source folder** and `destination` as the **Destination folder**.
3. Click **Save folders**. The app validates the paths and tests hard-link support with temporary files that it removes afterwards.
4. Click **Sync now** or wait for the automatic scan.

Paths are relative to `/app/data`. Both folders must already exist, remain inside that mount, and must not overlap. The mount and selected folders must be writable for the compatibility check. Settings are saved atomically to `data/.foldersync.json` on the host and restored after container recreation. You can change the selection from the same form; if a scan is active, save again after it finishes. Invalid changes keep the previous settings. Existing linked media are left in place when you change folders.

Scans stay disabled until setup is complete. `SCAN_INTERVAL_SECONDS` controls full scans (default `120`, or 2 minutes). `STABLE_SECONDS` controls the pending wait (default `10` seconds). Pending files are checked separately after that wait and linked if unchanged; a detected change restarts the wait. New files are discovered on the next full scan or when you click **Sync now**. Both settings must be positive integers. Matching rules still come from the image's `config.json`.

## Formula 1 destination names

For recognized Formula 1 videos, FolderSync keeps the source filename and creates a hard link in `Season YYRR`, where `YY` is the final two digits of the year and `RR` is the two-digit round number (01–26). Files in each round receive incremental episode numbers as they are linked:

```text
Season 2601/
  S2601E01 - Monza Practice 1.mp4
  S2601E02 - Monza Practice 2.mp4
  S2601E03 - Monza Qualify.mp4
```

The race and session remain explicit in every filename. Episode numbers follow linking order, which may differ from session order when files arrive later. The original extension and its letter case are preserved. The `formula1` section of `config.json` lists recognized Grand Prix names and event aliases; add new ones there and rebuild the image to apply changes. If a Formula 1 filename cannot be parsed, the scan logs the reason and links it under its original name in the rule destination. Other videos also retain their original names.

FolderSync saves source-to-destination matches in `data/.foldersync-links.json` and reuses episode numbers after restarts. Keep this file with the shared data mount. If a saved destination contains a different file, FolderSync reports a conflict and leaves it untouched. On the first completed scan after an upgrade and the stability wait, existing hard links under an older year folder are moved to the matching round folder when safe. Episode files already named without the `S` prefix are renamed in place and keep their episode number. Source files and media content are never renamed or copied. Existing destination files are never overwritten.


## Generate Plex episode NFO files

FolderSync automatically creates a Plex episode `.nfo` beside every destination video named `S<season>E<episode> - <title>`. The metadata contains the filename title, season, and episode number. This happens as part of normal scans after a video is linked, and full scans also fill in missing NFOs for videos already in the destination. Existing NFO files are never overwritten. Unmatched destination video names are counted in the scan result, and NFO errors appear in the logs and status page.

The standalone CLI remains available to preview NFO creation or run it manually against another media directory:

```text
python3 nfo.py /data/f1 --dry-run
python3 nfo.py /data/f1 --write
```

For the Docker setup above, the destination path is `/app/data/destination` inside the container. Rebuild and replace the container to enable automatic metadata creation.

## Formula 1 season posters

FolderSync looks for a `[Media Kit]` image post in [r/Formula1posters](https://www.reddit.com/r/Formula1posters/) for each `Season YYRR` folder that contains a recognized GP episode. If none is found, it looks for an explicitly titled official or FIA image post for the same GP and year, then for a matching F1 team poster. When several team posters match, it chooses the one with the highest Reddit score. It uses the GP name in the episode filenames and the year in `YYRR`, rather than assuming a fixed round calendar. For example, `Season 2616` with a Bahrain episode can match `Bahrain Grand Prix In Malaysia 2026 [Media Kit]`. It accepts `F1` title prefixes and maps `Spain` to the Spanish GP; `Spain Barcelona` in round 07 of 2026 maps to the separate Barcelona-Catalunya GP.

The downloaded JPEG or PNG is saved beside the episodes as `SeasonYYRR.jpg` or `SeasonYYRR.png`, for example `Season 2616/Season2616.jpg`. This is [Plex's season artwork naming convention](https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/). Enable local assets for the Plex library and refresh its metadata to see a new poster. FolderSync never replaces an existing season poster. Automatic scans retry missing posts and search errors after six hours while the app remains running. The scan summary shows unrecognized season folders and deferred retries; click **Sync now** to retry poster lookup immediately. If the latest scan shows all zero poster counts, check that the destination contains a `Season YYRR` folder with a recognized GP episode. The container needs internet access to Reddit and Reddit's image hosts. If Reddit refuses automated requests, you can place `SeasonYYRR.jpg` or `SeasonYYRR.png` in the season folder yourself; FolderSync will preserve it.

To preview or fill missing posters manually, run:

```text
python3 poster.py /app/data/destination --dry-run
python3 poster.py /app/data/destination --write
```

The preview lists folders that need a poster without contacting Reddit. The write command searches the subreddit and downloads matching images. A folder with no recognized GP episode or with episodes naming different GPs is skipped so a wrong poster is not assigned.

Reddit [requires OAuth for Data API access](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki). Register or obtain an approved Reddit script app, then put its credentials in a private `.env` file beside the Docker command:

```text
REDDIT_CLIENT_ID=your_app_id
REDDIT_CLIENT_SECRET=your_app_secret
REDDIT_USERNAME=your_reddit_username
```

Add `--env-file .env` to the `docker run` command above. The `.env` file is excluded from this project's Git and Docker build context. Never put the secret in `config.json`. If credentials are absent or rejected, poster lookup reports an error; NFO creation and video linking still run. This uses Reddit's [app-only OAuth flow](https://github.com/reddit-archive/reddit/wiki/OAuth2#application-only-oauth).

For 2026, the recognized GP names now include China (round 02) and Barcelona-Catalunya (round 07). The Barcelona poster is matched separately from the Spanish GP at round 14. Some rounds may lack a `[Media Kit]` post. FolderSync then accepts a matching official or FIA poster from the same subreddit, followed by a team poster for the same GP and year. For example, a 2026 Miami folder can use `Williams Miami Grand Prix 2026`; a 2025 Miami poster cannot match. If no suitable post exists, the folder remains without automatic artwork until a post appears or you add local artwork yourself.

## Replace an existing container

```text
docker stop foldersync
docker rm foldersync
docker build -t foldersync .
```

Then run the command above and configure your folders in the web page. Removing the container leaves bind-mounted host media intact. A restart alone does not change the mounts. `SOURCE_DIR` and `DESTINATION_DIR` are no longer used by the web app; folder selection is managed through the page.

## Missing season folders

FolderSync creates a recognized video's round folder when it scans a matching video. For example, a 2027 round 01 video creates `Season 2701` inside the destination folder selected in the web page. The selected destination folder itself must already exist. Rule paths with leading or trailing spaces are rejected when the image starts; rebuild and recreate the container after editing `config.json` because it is copied into the image.

## Troubleshooting hard links

`Invalid cross-device link` (Errno 18) means the paths cannot share a hard link. Separate bind mounts can cause this even on the same host drive; use the single `/app/data` mount above. Both folders must also reside on the same underlying filesystem, without nested mounts that cross filesystem boundaries, and the storage must support hard links. See the [Linux link documentation](https://man7.org/linux/man-pages/man2/link.2.html).

Failed links appear in the web status and logs. FolderSync does not automatically copy media or substitute symbolic links. Existing destination files with conflicting names are left untouched. Use `docker logs foldersync` for startup errors and scan activity.

If saved folders are missing or invalid at startup, the web page remains available so you can correct the selection. To apply image or matching-rule changes, repeat the stop, remove, build, and run steps above.
