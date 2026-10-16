> [!WARNING]
> This project is fully vibecoded, probably inefficient, but it does what I wanted lol

# Trakt → Nuvio Library + History Importer

A dependency-free Python tool for migrating a **Trakt account export ZIP into Nuvio** while keeping the Nuvio Library and tracking data separate.

It can:

- import Trakt personal/custom lists into the existing Nuvio Library;
- enrich sparse list entries with Cinemeta artwork/details;
- repair an earlier incomplete watched-history import;
- replace only Nuvio watched status + Continue Watching from Trakt;
- back up everything it is about to touch;
- verify writes by reading Nuvio back afterward;
- refuse destructive tracking resets when the Trakt export does not parse cleanly.

The history replacement path was added after finding a real Trakt-export compatibility bug: current exports can use `watched-movies.json`, while an older importer assumed only numbered names such as `watched-movies-1.json`. That caused watched movies to be silently omitted even though episode history imported.

## Repository files

```text
trakt_lists_to_nuvio.py              Main importer
README.md                             Setup, usage, architecture, and safety guide
tests/test_trakt_lists_to_nuvio.py   Regression tests for history parsing/reset safety
.gitignore                            Keeps exports/backups/previews out of Git
```

There are no required third-party Python packages.

## Requirements

- Python 3.10+
- A Trakt account export ZIP
- A Nuvio account
- Internet access for Nuvio writes
- Internet access to Cinemeta for Library enrichment and Continue Watching runtime resolution

Check Python with:

```bash
python --version
```

No `pip install` step is required.

## Recommended workflow

If your Nuvio Library is already correct and you only want to repair watched history, use **history-only mode**.

First preview the Trakt history locally:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --history-only --dry-run
```

Then perform the tracking replacement:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --history-only
```

Before deleting anything, the script:

1. reads the selected Nuvio profile;
2. downloads the current Library, watched rows, and Continue Watching rows;
3. writes a tracking backup locally;
4. validates the Trakt watched counts;
5. requires `REPLACE HISTORY` unless `--yes` was supplied.

The history replacement code **does not call Nuvio's Library write endpoint**.

## Import Library lists and repair history in one run

To import all personal Trakt lists and then rebuild tracking from the same export:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --all --replace-history
```

This has two independent phases:

```text
Trakt personal lists
        ↓
merge into existing Nuvio Library
        ↓
verify Library
        ↓
backup current tracking
        ↓
clear watched + Continue Watching only
        ↓
verify tracking is empty
        ↓
rebuild tracking from Trakt
        ↓
verify watched/progress
        ↓
verify Library membership did not change
```

If the Library already contains everything from the selected lists, no Library write occurs.

## Library-only import

The original behavior is still available.

Import every discovered personal list:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --all
```

Choose lists interactively:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip
```

Import specific lists by name or Trakt list ID:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip \
  --list "movies" \
  --list "shows"
```

### Episode/season rows inside personal lists

By default, episode and season list rows map to their parent series:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --all --episodes parent-series
```

To skip those rows:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --all --episodes skip
```

## Why history replacement does not duplicate watched entries

Nuvio stores a current watched state for a movie or episode, not every historical replay event.

For a given item, the importer keys state roughly as:

```text
movie:   content_id
series:  content_id + season + episode
```

Trakt may contain multiple watch events for the same item. The importer keeps the newest watched timestamp for each movie/episode.

When `--replace-history` or `--history-only` is used, it intentionally:

1. backs up Nuvio tracking;
2. deletes all watched-status rows for the selected profile;
3. deletes all Continue Watching rows for the selected profile;
4. repeatedly reads Nuvio until both tracking tables are verified empty;
5. writes the deduplicated Trakt state;
6. verifies every expected watched/progress key exists afterward.

Because the old tracking state is cleared first, rerunning the repair does not stack duplicate watched rows.

## The `watched-movies.json` bug that this fixes

Some Trakt exports use:

```text
watched-movies.json
```

Older/alternate exports may use:

```text
watched-movies-1.json
watched-movies-2.json
```

The importer supports both with:

```text
watched-movies(?:-N)?.json
```

It also reads movie records from `watched-history-N.json` as a fallback. This prevents a filename change from silently dropping all watched movies again.

The regression tests specifically cover the unnumbered `watched-movies.json` case.

## Trakt count sanity checks

Before a destructive history reset, the importer compares the parsed unique watched-state counts against `user-stats.json` when those values are present.

For example, if Trakt says there should be 61 watched movies but the parser only finds 0, the script raises an error **before logging into or deleting tracking data**.

This is intentionally stricter than only verifying the final upload. A write can be perfectly successful while still being incomplete if the parser skipped source records, so source-side validation is required too.

## IDs and Nuvio matching

The importer prefers IMDb IDs such as:

```text
tt1234567
```

If IMDb is unavailable it can temporarily use:

```text
tmdb:12345
trakt:67890
```

For Library entries, Cinemeta can often resolve fallback IDs to IMDb IDs before the write.

For watched TV episodes, the parent show ID matters because Nuvio's watched key includes the parent content ID plus season/episode. If Trakt uses `tmdb:...` while Nuvio metadata uses an IMDb ID, the same episode can otherwise look unwatched in the app even though a server row exists.

The history parser therefore attempts cross-ID normalization through Cinemeta instead of embedding user/export-specific title mappings in the repository.

## Continue Watching

Trakt playback exports store progress as a percentage. Nuvio stores position and duration in milliseconds.

The importer therefore needs runtime metadata to convert:

```text
Trakt:  25%
```

into something like:

```text
Nuvio position:  600000 ms
Nuvio duration: 2400000 ms
```

For episodes it uses season/episode-specific runtime metadata when available.

If any Trakt playback row cannot be reconstructed with a runtime, history replacement is blocked rather than clearing good Nuvio progress and silently losing the unresolved rows.

If you intentionally do not care about Continue Watching and only want watched/unwatched state:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --history-only --skip-progress
```

`--no-enrich` disables Library artwork/details lookup only. History-progress runtime lookup is separate because exact runtime is needed to rebuild Continue Watching safely.

## Library merge behavior

The Library portion remains non-destructive at the item level.

The tool:

- reads the existing Nuvio Library first;
- keeps existing items;
- avoids duplicates using stable IDs/aliases;
- preserves existing nonblank metadata;
- fills blank poster/background/logo/description/rating/genre fields when metadata is available;
- can normalize an old TMDB/Trakt fallback ID to an IMDb ID when safely resolved;
- repairs the incorrect `https://trakt.tv` addon URL written by an older version of this importer;
- blocks the write if existing Nuvio rows cannot be safely represented.

### Important: Nuvio Library sync is a full replacement

The API call used to write Library data takes the complete Library payload. Therefore the script always pulls the current Library, merges locally, saves a backup, and pushes the complete merged result.

Unless `--yes` is supplied, a Library write requires:

```text
IMPORT LIBRARY
```

The history replacement is a separate operation and requires:

```text
REPLACE HISTORY
```

## Metadata enrichment

Trakt exports often contain IDs/title/year but not the artwork Nuvio expects for a good Library card.

By default, the Library importer uses Cinemeta to fill fields such as:

- poster
- poster shape
- background
- logo when available
- description
- release information
- IMDb rating
- genres

Disable Library enrichment with:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --all --no-enrich
```

If Library enrichment is enabled and no selected title receives any metadata at all, the script stops before writing Nuvio rather than producing a completely sparse Library import.

## Backups, previews, and reports

A preview is written before any live Nuvio change:

```text
trakt-to-nuvio-preview-YYYYMMDD-HHMMSS.json
```

Before a Library write:

```text
nuvio-library-backup-profile-<id>-YYYYMMDD-HHMMSS.json
```

Before a tracking reset:

```text
nuvio-tracking-backup-profile-<id>-YYYYMMDD-HHMMSS.json
```

The tracking backup contains:

- current Library snapshot;
- current watched rows;
- current Continue Watching rows.

After completion:

```text
trakt-to-nuvio-import-report-profile-<id>-YYYYMMDD-HHMMSS.json
```

Choose another output directory with:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --history-only --backup-dir ./private-backups
```

## Nuvio login

The script talks directly to Nuvio's API. It does not automate a browser.

For normal login it prompts for:

- Nuvio email;
- Nuvio password using hidden terminal input.

The password is not saved by the script.

Supported environment variables include:

```text
NUVIO_EMAIL
NUVIO_PASSWORD
NUVIO_BASE_URL
NUVIO_PUBLISHABLE_KEY
NUVIO_ACCESS_TOKEN
```

Interactive password entry is preferable for normal manual use because environment variables can be exposed by shell/process/debugging configuration.

The built-in Nuvio key is a public/publishable client key, not a user password or private account token.

## Multiple Nuvio profiles

If the account has several profiles, the script asks which one to use.

You can also specify one directly:

```bash
python trakt_lists_to_nuvio.py trakt-export.zip --history-only --profile 1
```

## Main options

```text
--all
    Import every discovered personal Trakt list.

--list NAME_OR_ID
    Select a personal list by name or Trakt ID. Repeat as needed.

--episodes parent-series|skip
    Control episode/season rows inside personal lists.

--replace-history
    After the Library phase, replace watched status + Continue Watching from Trakt.

--history-only
    Do not write the Library; only replace watched/progress state.

--skip-progress
    Replace watched status but do not import Trakt Continue Watching.

--dry-run
    Build previews only; do not log in to or modify Nuvio.

--no-enrich
    Disable Library Cinemeta artwork/details enrichment.

--cinemeta-base URL
    Override Cinemeta endpoint; repeat for fallbacks.

--metadata-workers N
    Number of concurrent Library metadata requests.

--backup-dir DIR
    Directory for previews, backups, and reports.

--email EMAIL
    Supply Nuvio email instead of prompting/NUVIO_EMAIL.

--profile ID
    Select the Nuvio profile.

--yes
    Skip typed confirmations. Use only after reviewing a dry run.

--base-url URL
    Override Nuvio API base URL.

--publishable-key KEY
    Override the Nuvio public/publishable key.
```

Show the complete current help with:

```bash
python trakt_lists_to_nuvio.py --help
```

## Testing

Run the regression tests with:

```bash
python -m unittest discover -s tests -v
```

The tests currently cover:

- current `watched-movies.json` parsing;
- older numbered `watched-movies-N.json` parsing;
- movie-history fallback/deduplication;
- `user-stats.json` mismatch blocking;
- proof that the destructive tracking-replacement function never writes the Library.

Also run a syntax check after changes:

```bash
python -m py_compile trakt_lists_to_nuvio.py
```

## Security and privacy

This project handles credentials and personal viewing data.

- Do not commit Trakt export ZIPs.
- Do not commit preview/backup/report JSON files.
- Do not hard-code a Nuvio password or access token.
- Keep backups until you have verified both nuvio.tv and the Nuvio app.
- Custom service URLs must use HTTPS, except localhost HTTP for local testing.
- Be cautious with `NUVIO_PASSWORD` and `NUVIO_ACCESS_TOKEN` environment variables.
- `--yes` removes the typed safety confirmations and should not be your first run.
- Close Nuvio on other devices before a destructive history reset to reduce the chance of stale local state racing the migration.

The included `.gitignore` excludes the common Trakt export, preview, backup, and report filenames.

## If nuvio.tv shows watched but the app does not

Nuvio Mobile can use a connected tracking provider as the active watched/progress source instead of Nuvio Sync.

If the account/server page shows the imported state but the app does not, set the app's watched/progress source to **Nuvio Sync**, then fully close and reopen the app so it refreshes from the server.

Also remember that watched matching depends on the content ID. A TMDB-vs-IMDb parent-ID mismatch can make an episode row exist on the server while not matching the metadata ID the app is currently displaying; this importer attempts to normalize those IDs during the Trakt history parse.