# Plan: one poster for a GP weekend

## Goal
When FolderSync successfully links the first recognized GP video, try to download a suitable poster for the GP and year into its existing Season YYRR destination folder. Accept organizer, team and fan artwork, and allow a manual image URL when discovery cannot find a suitable poster.

## Integrated workflow
- Reuse the full year, round and GP name parsed from the video filename. Do not fetch a calendar or process an entire season.
- Run discovery and downloads in one background worker so video linking and episode NFO creation continue.
- Queue once per GP folder, including already linked videos after an upgrade. Save the outcome in poster.json and do not automatically repeat unsuccessful attempts on every scan or restart.
- Save Plex season artwork as SeasonYYRR.jpg or SeasonYYRR.png inside the matching Season YYRR folder. Preserve JPEG/PNG bytes; retain WebP originals and add a PNG version for Plex. Never overwrite existing artwork.
- On full scans, prepare legacy saved posters in place without downloading again or changing their originals. Keep poster.json provenance and update its artwork path.
- Discard queued and in-flight results when the selected destination changes.

## Automatic and manual sources
- Add an optional posters section to config.json with an enabled flag and official starting-page URLs grouped by GP. Italy and Monza share configuration.
- Check those public pages and at most five relevant same-site links per starting page. Require event-poster identity for the GP and year. Accept team and driver artwork; reject unrelated, wrong-year and store/product images.
- Select the first matching standalone poster in page order. Add public gallery fallback after organizer/media-kit lookup, using matching collection titles and the first image that passes validation. Bound gallery traversal to four pages and eight image candidates per starting URL, supporting lazy-loaded CDN images.
- Add GP poster status and controls to the existing web interface. An empty URL retries discovery; a direct public image URL downloads the user's selected image.
- Use POST /api/poster with the existing action-header convention and expose results through /api/status. Accept only GP folders detected by the sync workflow.
- Allow a per-GP official media-kit cover fallback, explicitly enabled for Azerbaijan. Extract the dominant embedded portrait image from the first PDF page without rendering or recompressing it. Verify the source GP/year and linked public Drive file year. Manual media-kit PDF URLs are also supported.
- No API key, Reddit integration, season registry, separate scheduler or historical collection.

## Validation and persistence
- Bound requests to a 10-second timeout and 20 MB, with one transient retry. Validate and pin public network addresses, including redirects.
- Use Pillow to verify and decode supported image content. Require portrait orientation, at least 600 pixels in width, and at most 20 million pixels.
- Download to a temporary file and publish atomically without overwriting. Keep original bytes.
- Store race identity, status, source page, asset URL, check time, filename and failure reason in one folder-local poster.json sidecar.
- Preserve poster failures separately from video scan errors; allow manual recovery.

## Acceptance and packaging
- Cover discovery identity checks, ambiguity, download validation, duplicate requests, persistent outcomes, manual retry, destination changes, existing artwork and blocked/private URLs with mocked HTTP tests.
- Cover both automatic post-link triggering and the manual web API, including stability waiting and non-blocking media syncing.
- Run the existing tests and new tests. Smoke-test an official live source and report whether it provides a usable downloadable image; do not claim guaranteed coverage.
- Include the new module, Pillow and pypdf in Docker and document source configuration and manual fallback.

## Defaults and limits
Automatic lookup is enabled. Starter organizer domains cover Australia, Italy, the Netherlands, Great Britain, Miami, Singapore and Azerbaijan. Azerbaijan also has a configured media-kit cover fallback. Shared Salracing galleries provide organizer, team and fan artwork for other GPs without requiring individual source entries. Live post-link checks cover all three sample GPs: Azerbaijan, Bahrain and Singapore. Discovery does not render JavaScript or bypass access restrictions, so not found is a valid result.
