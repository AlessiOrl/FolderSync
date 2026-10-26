# FolderSync

FolderSync scans a source folder and creates hard links to matching video files in a destination folder. It includes a local web interface for status, logs, and manual scans.

## Configure matching rules

The app reads `config.json` at `/app/config.json` inside the container. Mount your host file there, along with `/app/defaults`, to edit Formula 1 naming while the app is running:

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
docker run -d --name foldersync --restart unless-stopped -p 127.0.0.1:8082:8080 -v "H:\Data\Progetti_Info\FolderSync\data:/app/data" -v "H:\Data\Progetti_Info\FolderSync\config.json:/app/config.json:ro" -v "H:\Data\Progetti_Info\FolderSync\defaults:/app/defaults:ro" -e SCAN_INTERVAL_SECONDS=120 -e STABLE_SECONDS=10 foldersync
```

Use your actual host path. On Linux or WSL, replace the Windows path with `/mnt/h/Data/Progetti_Info/FolderSync/data` or the appropriate Linux path.

## Automatic permissions in Docker

The image runs as root and automatically sets the selected destination and its existing contents to `root:100` when saved folders are restored at container startup, and when you select folders in the web page. It also applies permissions to newly linked videos, generated NFOs, artwork, and JSON files:

- Files: `0774` (`-rwxrwxr--`) — root and members of the selected group can read, write and execute; everyone else can read.
- Folders: `0774` (`drwxrwxr--`) — root and members of the selected group can list, enter and write to folders. Everyone else can list entry names but cannot access files inside because they lack directory execute permission. Your Windows share login must map to the selected group. Set `DIRECTORY_MODE=2775` if everyone needs traversal access.

Set `PGID` to the numeric ID of the host's `users` group. On Unraid this is usually `100`; check with `getent group users`. Add these options before the image name in your existing `docker run` command:

```text
-e PGID=100 -e FILE_MODE=0774 -e DIRECTORY_MODE=0774
```

The defaults above are built into the image, so rebuild it and recreate your container to apply them. Ownership requires running the container as root with the normal Docker ownership/permission capabilities. Docker matches groups by numeric ID; the group name displayed inside the container can differ from the host. These settings require a filesystem that supports Unix ownership and permissions.

If media and metadata do not need executable permission, use `-e FILE_MODE=0664` instead. Everyone still has read access, and root and the group retain write access. With `0774`, root and the selected group have execution permission. Executable files may appear with a trailing `*` in `ls` output; this indicator is not part of the filename.

**Hard links share ownership and permissions.** Applying these settings to a destination video also changes its source inode, including other hard links to it. The startup repair covers the selected destination only, skips symbolic links, and does not recursively change unrelated source files. Existing destination metadata is preserved while its permissions are repaired. Parent folders above the selected destination must already allow traversal. When running Python outside Docker, permission management is disabled unless `PGID` is set.

## Choose folders in the web page

1. Open <http://localhost:8082>.
2. Enter `source` as the **Source folder** and `destination` as the **Destination folder**.
3. Click **Save folders**. The app validates the paths and tests hard-link support with temporary files that it removes afterwards.
4. Click **Sync now** or wait for the automatic scan.

Paths are relative to `/app/data`. Both folders must already exist, remain inside that mount, and must not overlap. The mount and selected folders must be writable for the compatibility check. Settings are saved atomically to `data/.foldersync.json` on the host and restored after container recreation. You can change the selection from the same form; if a scan is active, save again after it finishes. Invalid changes keep the previous settings. Existing linked media are left in place when you change folders.

Scans stay disabled until setup is complete. `SCAN_INTERVAL_SECONDS` controls full scans (default `120`, or 2 minutes). `STABLE_SECONDS` controls the pending wait (default `10` seconds). Pending files are checked separately after that wait and linked if unchanged; a detected change restarts the wait. New files are discovered on the next full scan or when you click **Sync now**. Both settings must be positive integers. Matching rules come from `/app/config.json`; changes to rules, posters, metadata, or extensions require an app restart. Formula 1 naming changes take effect on the next scan.

## Formula 1 destination names

For recognized Formula 1 videos, FolderSync keeps the source filename and creates a hard link in `Season YYRR`, where `YY` is the final two digits of the year and `RR` is the two-digit round number (01–26). Files in each round receive incremental episode numbers as they are linked:

```text
Season 2601/
  S2601E01 - Monza Practice 1.mp4
  S2601E02 - Monza Practice 2.mp4
  S2601E03 - Monza Qualify.mp4
```

The race and session remain explicit in every filename. Episode numbers follow linking order, which may differ from session order when files arrive later. The original extension and its letter case are preserved. The files `defaults/grand_prix.json` and `defaults/events.json` supply recognized Grand Prix names and event aliases. The `formula1` section of `config.json` can override either list and contains the Grand Prix aliases. If a Formula 1 filename cannot be parsed, the scan logs the reason and links it under its original name in the rule destination. Other videos also retain their original names.

Formula 1 naming defaults are stored as JSON data:

- `defaults/grand_prix.json`: an array of canonical GP names, such as `["Australia", "United States"]`.
- `defaults/events.json`: an object mapping event aliases to output names, such as `{"Qualifying": "Qualify", "Race": "Race"}`.
- `config.json` → `formula1.grand_prix_aliases`: an object grouping venue and country aliases by canonical GP name.

To override the GP or event defaults for a deployment, add `formula1.grand_prix` or `formula1.events` in `config.json`. Each override replaces its entire default list or map. Omit the key to use the corresponding file in `defaults/`. GP lists and event maps must be nonempty.

Grand Prix aliases are editable defaults under `formula1.grand_prix_aliases` in `config.json`, grouped by the canonical GP name. For example:

```json
"grand_prix_aliases": {
  "United States": ["USA.COTA", "USA", "COTA", "Austin"],
  "Great Britain": ["UK", "Silverstone"],
  "São Paulo": ["Brazil", "Sao.Paulo", "Interlagos"]
}
```

The shipped file includes aliases for the other supported venues. Matching ignores case and accepts dots, spaces, underscores, or hyphens. Compound names such as `USA.Miami` and `USA.Las.Vegas` resolve to their respective events. Aliases apply only when their canonical Grand Prix is enabled in `formula1.grand_prix`; explicit configured names take precedence over aliases of the same length.

Save changes to the mounted `config.json`, then click **Sync now** or wait for the next scan. The app reloads the GP list, event map, and GP aliases before every full or pending scan without a restart or rebuild, including changes to the mounted `defaults/` files. Invalid or temporarily unreadable files produce a warning and retain the entire last valid naming configuration; the next valid edit is picked up automatically. Set a GP's alias list to `[]` to disable its aliases, or set `grand_prix_aliases` to `{}` to disable all aliases. Omitting the section also means no aliases. Other configuration sections require a restart.

FolderSync saves source-to-destination matches in `data/.foldersync-links.json` and reuses episode numbers after restarts. Keep this file with the shared data mount. If a saved destination contains a different file, FolderSync reports a conflict and leaves it untouched. On the first completed scan after an upgrade and the stability wait, existing hard links under an older year folder are moved to the matching round folder when safe. Episode files already named without the `S` prefix are renamed in place and keep their episode number. Source files and media content are never renamed or copied. Existing destination files are never overwritten.


## Generate Plex episode NFO files

For recognized F1 videos, FolderSync prepares episode NFOs, `tvshow.nfo` and `season.nfo` **before creating the video hard link**. Episodes include readable titles, descriptions and the calendar year. The show includes Sports/Motorsport genres, an expanded series synopsis and a custom season name such as `2026 Azerbaijan Grand Prix`, using Plex's supported `namedseason` tag. Episode air dates are included only when explicitly configured. Preparation errors prevent publishing a new video and are retried on subsequent scans. FolderSync updates its own marked metadata; user-authored NFOs remain untouched.

The show year is the lowest year decoded from known `YYRR` season codes, including recorded `namedseason` entries and existing `Season YYRR` folders with matching episode files. Empty folders and legacy calendar-year folders without matching YYRR episodes do not supply a year. For example, `2515` and `2615` give the show year 2025. Adding an older GP lowers that year; processing a newer GP does not raise it. Plex has one series year, while individual GP seasons retain their own year in their names. FolderSync writes both `year` and Plex's supported `premiered` date to `tvshow.nfo`, using January 1 of that lowest year as a **collection date convention**, not an actual premiere or session date. Full scans also refresh marked show NFOs when their source videos are no longer present. Recorded season names remain part of the collection history even if their folders are removed.

After rebuilding and replacing the container, run **Sync now**, then **Refresh Metadata** on the Formula 1 show in Plex using the Plex NFO Series agent. Tautulli's existing show template `<b>{title}</b> ({year})` can then use the imported show year, and `{summary}` can show the expanded synopsis. FolderSync does not refresh Plex or resend past notifications itself.

Other destination videos named `S<season>E<episode> - <title>` receive basic missing NFOs during full scans. These contain title, season and episode number. Full scans also backfill basic missing NFOs for existing videos. Existing unmarked NFOs are preserved.

Use **Regenerate metadata** in the **Folder metadata** panel to rebuild metadata throughout the selected destination without needing the original source videos. Recognized F1 episode filenames supply the GP, session and YYRR code, allowing regeneration of episode, season and show NFOs and the lowest show year. Other conventionally named episodes receive basic metadata. The action replaces FolderSync-marked NFOs, repairs malformed marked NFOs, and upgrades episode NFOs that exactly match the older basic CLI output. Other unmarked custom NFOs and symlinks remain untouched. Existing posters and videos are preserved; the action does not link videos or download posters. The page and logs report created, updated, unchanged, preserved and unparsed files plus errors. Individual failures do not stop other folders. Refresh metadata on the show in Plex afterwards to import the changes. Regeneration shares the scan/configuration lock, so simultaneous requests return a busy response.

Use the **Plex NFO agent** (Plex Media Server 1.43.1 or newer) for this metadata. Plex mostly ignores `season.nfo`; custom GP names are therefore also stored in `tvshow.nfo`. See [Plex's NFO documentation](https://support.plex.tv/articles/using-nfo-metadata-files-with-plex/). Tautulli reads the metadata Plex imports, so creating files cannot by itself guarantee a particular notification format.

### Customize F1 metadata and artwork

The `metadata` section in `config.json` supports show title, description and genres, optional show `poster` and `background`, and event overrides keyed by YYRR. Artwork paths refer to existing JPEG/PNG files **relative to the selected Formula 1 show folder**, for example `artwork/f1-background.jpg`. Files must remain inside that folder. FolderSync validates and copies them to Plex's local asset names before linking a video, preserving existing artwork. A background is show-level; it is not automatically discovered or made by stretching a GP poster.

For example, customize a GP without changing the video naming:

```json
"metadata": {
  "title": "Formula 1",
  "description": "Formula 1 Grand Prix coverage.",
  "genres": ["Sports", "Motorsport"],
  "poster": "artwork/f1-poster.jpg",
  "background": "artwork/f1-background.jpg",
  "events": {
    "2615": {
      "name": "2026 Azerbaijan Grand Prix",
      "description": "Formula 1 weekend in Baku.",
      "poster": "artwork/baku-2026.png",
      "sessions": {
        "Race": {
          "title": "Azerbaijan Grand Prix — Race",
          "description": "Race coverage from Baku."
        }
      }
    }
  }
}
```

Add a session `date` in `YYYY-MM-DD` format when known; it must belong to the event's year. Session keys use the normalized names, such as `Race`, `Qualify`, `Practice 1` or `Sprint Qualifying`. A bare year is stored in the episode NFO, but Plex's documented episode date field is `aired`: configure the actual session date to populate that field. Rebuild and replace the container after configuration changes.

The standalone CLI remains available to preview NFO creation or run it manually against another media directory:

```text
python3 nfo.py /data/f1 --dry-run
python3 nfo.py /data/f1 --write
```

For the Docker setup above, the destination path is `/app/data/destination` inside the container. Rebuild and replace the container to enable automatic metadata creation.

## Download a GP poster

When a recognized GP video is first detected, FolderSync creates the show and season NFO files, prepares configured artwork, and queues one background poster lookup for its `Season YYRR` folder during the file stability wait. Videos remain pending until a season poster is saved or 60 seconds have elapsed since the season's first detection. Sessions in the same season share this deadline; rescans do not restart it. Episode NFO files are written before their video hardlinks. The full year and GP name come from the source filename. Saved lookup outcomes survive restarts. Other GPs and non-F1 videos can continue syncing.

If a poster is missing, failed or still downloading at the 60-second deadline, videos link anyway once the source file passes its stability check. Poster lookup continues in the background. With automatic lookup disabled, videos do not wait for artwork unless `posters.require_before_link` is `true`; that option also has the same one-minute limit. The GP posters section shows the lookup outcome. Configured custom GP artwork is prepared before lookup and takes priority.

The **GP posters** section shows each detected folder's result. Use **Retry lookup / download URL** with an empty URL to repeat discovery, or paste a public direct JPEG, PNG, WebP, or media-kit PDF URL to download your selected artwork. Public Google Drive file links also work. This manual option also works when automatic lookup is disabled. A missing, ambiguous, blocked, or invalid poster is reported separately from video scan errors.

Artwork uses Plex's season-poster naming inside the matching GP folder: `Season2615.jpg` or `Season2615.png` for the videos numbered `S2615E…` in `Season 2615`. JPEG and PNG downloads keep their original bytes. WebP downloads retain the original `poster.webp` and add a PNG version for Plex, without changing image dimensions. The image must be portrait and at least 600 pixels wide; responses are limited to 20 MB and images to 20 million pixels. `poster.json` records the GP, year, source page, asset URL, attempt time, and actual artwork filename. Existing artwork is never overwritten. If a saved poster is removed, retry manually to restore it.

On Unraid, place the GP folders inside the Formula 1 show folder already scanned by your Plex TV library:

```text
Formula 1/
  Season 2615/
    S2615E01 - Azerbaijan Qualify.mkv
    S2615E01 - Azerbaijan Qualify.nfo
    S2615E02 - Azerbaijan Race.mkv
    Season2615.jpg
    poster.json
```

The artwork number matches the full `YYRR` season code, not just the calendar year or round. Plex recognizes artwork naming; Unraid supplies the storage. In the Plex TV library's advanced settings, enable **Use local assets**, then refresh the Formula 1 show's metadata. Legacy agents require **Local Media Assets** enabled and ordered first. See [Plex's local TV artwork documentation](https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/). FolderSync does not change Plex settings or force a selected poster to refresh.

After upgrading, full scans also prepare saved posters whose source videos are no longer present. A valid legacy `poster.jpg`, `poster.jpeg`, `poster.png`, or `poster.webp` with `poster.json` gets a correctly named season artwork file in the same folder without another download. Legacy originals remain in place, provenance is retained, and existing `SeasonYYRR.*` artwork takes priority. A source video observed during syncing can also trigger preparation without a sidecar.

Configure automatic lookup in the image's `config.json`:

```json
"posters": {
  "enabled": true,
  "sources": {
    "Italy": ["https://www.monzanet.it/"],
    "Great Britain": ["https://www.silverstone.co.uk/events/formula-1-british-grand-prix/formula-1-press"]
  },
  "media_kits": {
    "Azerbaijan": ["https://www.bakucitycircuit.com/media-kit"]
  }
}
```

Source entries can be GP/organizer or team pages. Prefer a specific public poster announcement over a homepage. Italy and Monza share configuration. Each GP can have up to five starting URLs; lookup visits each starting page and at most five relevant same-site poster links. Candidates must identify poster artwork for the GP and year. Team and driver artwork is accepted; store/product images are excluded. Multiple standalone matches use the first candidate in page order.

The bundled configuration also checks public Salracing galleries after organizer pages and media-kit lookup have no match. These collections include team, official and fan artwork. Add a `galleries` mapping to configure other poster collections; `"*"` applies to every GP:

```json
"galleries": {
  "*": ["https://www.salracing.com/story", "https://www.salracing.com/story/category/Posters"]
}
```

Gallery lookup follows matching GP/year poster links on the same site, checking at most four pages and eight image candidates per starting URL. A collection's title must identify the requested GP and year. The first valid portrait image is selected in page order; unavailable, corrupt, landscape or small images are skipped. Lazy-loaded images and public image CDNs are supported, and `poster.json` retains the collection and image URLs. No search API key is needed. Homepages may not expose posters, sites may block automated requests, and JavaScript-only pages are not rendered, so an automatic match is not guaranteed. Set `"galleries": {}` to disable this fallback. Existing failed outcomes require **Retry lookup / download URL** after upgrading.

The optional `media_kits` mapping enables an official cover fallback for listed GPs only, after no standalone poster is found. Azerbaijan's public organizer page links a 2026 PDF containing the 1878 × 2658 GP promotional artwork on its cover; this source has been tested live. The downloader checks the GP/year on the official page and the year of linked public Drive media-kit files. It extracts the dominant embedded portrait image from the PDF's first page, preserving its original bytes, and records `asset_kind: "media_kit_cover"`. It does not render PDFs, guess between competing cover images, use encrypted documents, or bypass private Drive files. PDFs are limited to 200 pages. Remove a GP's `media_kits` entry to disable its automatic cover fallback. Existing failed `poster.json` outcomes still require **Retry lookup / download URL** after upgrading.

Set `"enabled": false` to disable automatic lookup. Rebuild and replace the container after changing source configuration. Failed lookups are not retried every scan; use the web control when ready. Public URLs and redirects are checked against private/local addresses. Use publicly provided artwork and retain its source information.

For local Python runs, install `requirements.txt` first. Docker installs Pillow and pypdf automatically. The opt-in `python tests/live_poster_check.py` checks real post-link discovery against every GP video in `tests/test_folder`, using temporary hard links rather than copying their contents, and leaves the fixtures unchanged.

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
