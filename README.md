# Trakt Lists → Nuvio Library

A command-line importer that takes **personal Trakt lists from a Trakt account export ZIP** and adds those movies/shows to a **Nuvio** library.

It was made for the case where Nuvio can import Trakt watch history but does not directly import your Trakt lists into the Nuvio library.

## What it does

The tool:

1. Opens a Trakt account export ZIP.
2. Finds your personal/custom Trakt lists and their items.
3. Lets you choose one, several, or all lists.
4. Converts Trakt IDs into IDs Nuvio can use.
5. Optionally asks Cinemeta for posters, backgrounds, descriptions, genres, ratings, and other metadata.
6. Logs in to your Nuvio account.
7. Reads your current Nuvio library first and creates a local backup/report.
8. Merges the imported items into the existing library instead of blindly replacing it.
9. Writes the merged library back to Nuvio only after confirmation.

Episode/season entries can either add their **parent series** or be skipped.

## Requirements

- Python 3.10+ recommended
- A Trakt account export ZIP containing your lists
- A Nuvio account
- Internet access for Nuvio; Cinemeta access is optional

**No third-party Python packages are required.** The script uses Python's standard library (`urllib`, `zipfile`, `json`, etc.).

## Getting the source

This repository includes `trakt-lists-to-nuvio-source.zip`. Extract it:

```bash
unzip trakt-lists-to-nuvio-source.zip
cd trakt-lists-to-nuvio
```

The main program is:

```text
trakt_lists_to_nuvio.py
```

## Basic use

Start with a dry run. A dry run reads and maps your Trakt export but does **not** log in to or modify Nuvio:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --dry-run
```

Normal interactive import:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip
```

Import every personal list without asking which lists:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all
```

Import one or more lists by name or Trakt list ID:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip \
  --list "Movies to Watch" \
  --list "Favorites"
```

## Useful options

```text
--dry-run
    Build a preview only; do not log in to or modify Nuvio.

--all
    Import all personal Trakt lists.

--list NAME_OR_ID
    Select a list by name or Trakt ID. Repeat for multiple lists.

--episodes parent-series
    Convert episode/season entries to their parent TV series. This is the default.

--episodes skip
    Ignore episode/season entries.

--no-enrich
    Skip Cinemeta metadata lookups. Mapping is then done without artwork/details enrichment.

--preview-file FILE
    Write the local mapping preview JSON to a specific path.

--backup-dir DIR
    Directory where the pre-import Nuvio backup and report are stored.

--email EMAIL
    Supply the Nuvio account email. Otherwise the script can use NUVIO_EMAIL or prompt.

--profile PROFILE
    Choose a Nuvio profile by ID/index instead of selecting interactively.

--yes
    Skip the final IMPORT confirmation.
```

See everything with:

```bash
python3 trakt_lists_to_nuvio.py --help
```

## How the mapping works

Trakt exports describe items using Trakt/IMDb/TMDB identifiers. Nuvio's library expects its own content records, so the importer builds a normalized library item for each selected Trakt row.

When enrichment is enabled, the tool queries Cinemeta using known IMDb/TMDB IDs and uses the returned metadata to fill fields such as poster, background, description, release info, rating, and genres. If enrichment fails for an item, that failure is recorded rather than silently pretending metadata was found.

Before writing anything, the tool downloads the current Nuvio library and saves a backup. The merge is intended to be non-destructive: existing Nuvio values are preserved unless they are blank or were previously populated by this importer.

## Nuvio login and profiles

The script uses Nuvio's API, not browser automation. It uses Nuvio's public/publishable client key to start the login flow. Your account email can be provided with `--email`, the `NUVIO_EMAIL` environment variable, or an interactive prompt.

If your Nuvio account has multiple profiles, the script can ask which one to use or you can specify one with `--profile`.

## Backups and reports

A normal import creates a **pre-import backup** of the current Nuvio library and a report describing what was mapped, enriched, skipped, or changed. Keep these until you have confirmed the import looks correct.

For a first run, the safest workflow is:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --dry-run
python3 trakt_lists_to_nuvio.py trakt-export.zip
```

Review the preview/backup before confirming the live import.

## Security and privacy

- A Trakt export can contain personal viewing/list data. Do not publish your export ZIP unless you intentionally want that data public.
- Nuvio login information is used only for the API login flow. Do not hard-code passwords or authentication tokens into this repository.
- Prefer `NUVIO_EMAIL` or the interactive prompt for account-specific configuration instead of editing your email into the script.
- Backup and preview JSON files contain your Nuvio library data. Treat them as private. This repository's `.gitignore` excludes common export, backup, preview, and environment files.
- `--yes` removes the final safety confirmation. Use it only when you already trust the exact command and input.
- The default Nuvio publishable key is a **public client key**, not a private account secret. Do not confuse it with session/access tokens returned during authentication.
- The importer depends on current Nuvio/Cinemeta API behavior. If those services change, inspect a dry run and backup before allowing a live write.

## Repository files

```text
README.md                         This guide
trakt-lists-to-nuvio-source.zip   Source package containing the Python script
.gitignore                        Keeps local exports/backups/secrets out of Git
```

After extracting the source package, the Python file itself also contains detailed CLI help and comments describing the import flow.
