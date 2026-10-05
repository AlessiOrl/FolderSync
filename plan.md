# Plex NFO Metadata Generation

## Context

We have a Plex TV library containing Formula 1 recordings.

The video files are already named using Plex's season/episode convention, with a custom 4-digit season number and the episode title embedded in the filename.

Example:

```text
/data/f1/Season 2616/S2616E01 - Bahrain Qualify.mkv
```

The filename contains:

- Season: `2616`
- Episode: `1`
- Title: `Bahrain Qualify`

Plex correctly identifies the season and episode number, but it does not use the title portion of the filename as the episode title. When online metadata does not provide a matching episode, Plex displays a generic title such as `Episode 1`.

The solution is to use Plex's **Plex NFO Series** metadata agent and generate an `.nfo` file alongside each video.

For the example above:

```text
/data/f1/Season 2616/S2616E01 - Bahrain Qualify.mkv
/data/f1/Season 2616/S2616E01 - Bahrain Qualify.nfo
```

The NFO should contain:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<episodedetails>
    <title>Bahrain Qualify</title>
    <season>2616</season>
    <episode>1</episode>
</episodedetails>
```

Plex's NFO agent should then use the NFO as the authoritative episode metadata.

## Goal

Create a tool/script that scans the existing F1 media files and generates the corresponding Plex episode `.nfo` files.

**Do not rename, move, modify, or re-encode any media files.**

The only files that should be created or modified are `.nfo` metadata files.

## Filename Parsing

The parser should recognize filenames following this general pattern:

```text
S<season>E<episode> - <title>.mkv
```

Example:

```text
S2616E01 - Bahrain Qualify.mkv
```

should produce:

```text
season = 2616
episode = 1
title = Bahrain Qualify
```

The parser must support four-digit season numbers.

The title should be extracted from the portion after the ` - ` separator and before the file extension.

## NFO Generation

For every valid media file, generate an NFO with the same basename:

```text
S2616E01 - Bahrain Qualify.mkv
S2616E01 - Bahrain Qualify.nfo
```

The generated XML should contain:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<episodedetails>
    <title>Bahrain Qualify</title>
    <season>2616</season>
    <episode>1</episode>
</episodedetails>
```

Ensure XML is properly escaped if an episode title contains characters such as:

```text
&
<
>
"
'
```

## Safety Requirements

- Never modify media files.
- Never rename media files.
- Never delete files.
- Do not overwrite an existing `.nfo` by default.
- Provide a dry-run mode showing what would be generated.
- Report files that do not match the expected naming pattern.
- Handle existing NFO files safely.
- Make the operation idempotent: running the tool multiple times should not create duplicate files or produce different results.

## Suggested Workflow

1. Recursively scan the configured media directory.
2. Find supported video files, primarily `.mkv`.
3. Parse the filename using the `S<season>E<episode> - <title>` convention.
4. Extract season, episode, and title.
5. Determine the NFO path from the media filename.
6. Generate the Plex-compatible `<episodedetails>` XML.
7. In dry-run mode, show the proposed changes without writing anything.
8. When writing is enabled, create the NFO if it does not already exist.
9. Produce a summary containing:
   - number of media files scanned
   - number successfully parsed
   - number of NFOs created
   - number skipped because an NFO already exists
   - number of files that could not be parsed

## Example

Input:

```text
/data/f1/Season 2616/S2616E01 - Bahrain Qualify.mkv
/data/f1/Season 2616/S2616E02 - Bahrain Race.mkv
```

Expected output:

```text
/data/f1/Season 2616/S2616E01 - Bahrain Qualify.nfo
/data/f1/Season 2616/S2616E02 - Bahrain Race.nfo
```

First NFO:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<episodedetails>
    <title>Bahrain Qualify</title>
    <season>2616</season>
    <episode>1</episode>
</episodedetails>
```

Second NFO:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<episodedetails>
    <title>Bahrain Race</title>
    <season>2616</season>
    <episode>2</episode>
</episodedetails>
```

## Deliverable

Implement the scanner/NFO generator with a simple CLI suitable for running against `/data/f1`.

The implementation should prioritize correctness, safe file handling, clear logging, and idempotency over unnecessary complexity.