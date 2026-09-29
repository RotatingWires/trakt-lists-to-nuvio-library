> [!WARNING]
> This project is fully vibecoded, probably inefficient, but it does what I wanted lol

# Trakt Lists → Nuvio Library

A dependency-free Python tool that imports **personal/custom Trakt lists from a Trakt account export ZIP into your Nuvio library**.

It was created for the case where Nuvio can import Trakt watch history, but your Trakt lists do not automatically become Nuvio library entries.

The importer reads your Trakt export, maps the selected movies/shows into Nuvio library records, optionally enriches them with Cinemeta metadata, merges them with your existing Nuvio library, creates a backup, and only then writes the merged library back to Nuvio.

## Repository files

```text
trakt_lists_to_nuvio.py   Main importer
README.md                  Setup, usage, architecture, and security guide
.gitignore                 Keeps common exports/backups/previews/secrets out of Git
```

There are no required third-party Python packages.

## Requirements

You need:

- Python 3.10+ recommended
- A Trakt account export ZIP containing your personal lists
- A Nuvio account
- Internet access for a real Nuvio import
- Internet access to Cinemeta if metadata enrichment is enabled

The script uses only Python's standard library, including `urllib`, `zipfile`, `json`, `getpass`, and `concurrent.futures`.

Check Python with:

```bash
python3 --version
```

No `pip install` step is required.

## Getting started

Clone or download this repository, then place your Trakt export somewhere convenient. You do **not** need to extract the Trakt export ZIP yourself.

Show the command-line help:

```bash
python3 trakt_lists_to_nuvio.py --help
```

For a first run, use a dry run:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --dry-run
```

A dry run:

- reads your Trakt export;
- maps your selected lists;
- contacts Cinemeta for metadata unless `--no-enrich` is used;
- writes a local preview JSON;
- does **not** log in to Nuvio;
- does **not** modify your Nuvio library.

For a completely offline parse/mapping test:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --dry-run --no-enrich
```

## Performing an import

Import all personal lists:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all
```

Or let the tool ask which lists to use:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip
```

Import specific lists by name or Trakt list ID:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip \
  --list "Movies to Watch" \
  --list "Favorites"
```

Unless `--yes` is supplied, the tool shows a merge summary and requires you to type:

```text
IMPORT
```

before it writes anything to Nuvio.

## Episode and season entries

Nuvio's library stores movie/series titles rather than individual Trakt episode-list rows.

By default, episode and season entries are mapped to their parent series:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --episodes parent-series
```

To ignore episode/season entries instead:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --episodes skip
```

## Metadata enrichment

Trakt exports contain useful IDs and title information, but they do not necessarily contain the artwork/details Nuvio normally displays.

By default, the importer uses **Cinemeta** to enrich mapped titles with data such as:

- poster
- poster shape
- background
- title logo when available
- description
- release information
- IMDb rating
- genres

The importer prefers stable IMDb IDs when possible. If a row initially only has a TMDB or Trakt fallback ID, Cinemeta may allow the tool to resolve it to an IMDb ID.

Disable metadata lookups with:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --no-enrich
```

If enrichment is enabled and Cinemeta returns no metadata at all for the selected titles, the importer stops before contacting Nuvio rather than unexpectedly writing a completely sparse library import.

## How the import works

At a high level:

```text
Trakt export ZIP
      ↓
Discover personal/custom lists
      ↓
Select one, several, or all lists
      ↓
Normalize movie/show IDs
      ↓
Optionally enrich through Cinemeta
      ↓
Write local preview JSON
      ↓
Log in to Nuvio
      ↓
Read profiles + current library
      ↓
Save pre-import library backup
      ↓
Merge selected Trakt titles into current library
      ↓
Show summary + ask for confirmation
      ↓
Push merged library
      ↓
Read library again and verify imported IDs
      ↓
Write import report
```

### ID mapping

The script reads Trakt/IMDb/TMDB identifiers from the export. It prefers IMDb IDs such as:

```text
tt1234567
```

When an IMDb ID is not available, temporary fallback IDs such as `tmdb:...` or `trakt:...` may be used. Enrichment can sometimes resolve those fallback IDs to IMDb IDs before the Nuvio write.

### Merge behavior

The tool first downloads your current Nuvio library and merges the selected Trakt titles into it.

For matching entries it is intentionally conservative:

- existing nonblank Nuvio metadata is preserved;
- blank fields can be filled from enriched Trakt/Cinemeta data;
- old fallback IDs can be upgraded to IMDb IDs when safely resolved;
- duplicate entries are avoided during ID upgrades;
- entries previously created by the older importer can be repaired rather than requiring you to delete them first.

If the current Nuvio library contains malformed/unrecognized rows that the script cannot safely preserve, the import is blocked rather than silently dropping them.

## Important: Nuvio library writes are full replacement

Nuvio's library sync operation used by this tool replaces the library payload with the complete merged library.

That is why the importer:

1. downloads your current library first;
2. merges your Trakt entries locally;
3. saves a backup;
4. shows the before/after counts;
5. requires confirmation by default;
6. verifies the imported IDs after the write.

Do not treat the backup step as optional housekeeping. Keep the backup until you have checked the resulting Nuvio library.

## Backups, previews, and reports

Before a live write, the tool creates a backup similar to:

```text
nuvio-library-backup-profile-<id>-YYYYMMDD-HHMMSS.json
```

It also creates a preview before login/write:

```text
trakt-to-nuvio-preview-YYYYMMDD-HHMMSS.json
```

After a successful write it creates an import report containing counts, selected lists, metadata results, verification results, and the backup/preview filenames.

Choose another output directory with:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --backup-dir ./private-backups
```

## Nuvio login

The script talks directly to Nuvio's API; it does not automate a browser.

For a normal login it asks for:

- your Nuvio email;
- your Nuvio password using hidden terminal input.

The password is **not saved by the script**.

You can provide the email through an environment variable:

```bash
export NUVIO_EMAIL="you@example.com"
```

The script also supports `NUVIO_PASSWORD`, but an interactive hidden password prompt is safer for ordinary manual use because environment variables can sometimes be exposed to other processes, shell configuration, logs, debugging tools, or automation environments.

The built-in Nuvio key in the script is Nuvio's **public/publishable client key**. It is not your personal password or private access token.

## Nuvio profiles

If your account has multiple Nuvio profiles, the tool can ask you to choose one interactively.

You can also specify one:

```bash
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --profile 1
```

## Useful options

```text
--all
    Import every discovered personal Trakt list.

--list NAME_OR_ID
    Select a specific list by name or Trakt ID. Repeat as needed.

--episodes parent-series
    Convert episode/season rows to their parent series. Default.

--episodes skip
    Skip episode/season rows.

--dry-run
    Create a preview without logging in to or modifying Nuvio.

--no-enrich
    Disable Cinemeta lookups.

--cinemeta-base URL
    Override the Cinemeta endpoint. Repeat to provide fallbacks.

--metadata-workers N
    Number of concurrent Cinemeta metadata requests.

--preview-file FILE
    Choose the preview JSON filename.

--backup-dir DIR
    Choose where preview, backup, and report files are written.

--email EMAIL
    Supply the Nuvio email instead of prompting/NUVIO_EMAIL.

--profile PROFILE
    Choose a Nuvio profile ID/index.

--yes
    Skip the final IMPORT confirmation.

--base-url URL
    Override the Nuvio API endpoint.

--publishable-key KEY
    Override the Nuvio public/publishable API key.
```

## Security and privacy

This project handles account credentials and personal media-library data, so treat the working directory and generated files accordingly.

- **Trakt exports are personal data.** They can reveal your lists, titles, viewing interests, and account-related identifiers. Do not commit them to Git.
- **Preview, backup, and report JSON files are personal data.** They can contain your Nuvio library and import details. The included `.gitignore` excludes common generated filenames.
- **Do not hard-code your Nuvio password or access token into the script.** The normal password prompt uses `getpass` so the password is not echoed.
- **The bundled publishable key is public client configuration, not a user secret.** Access/session tokens obtained after login are different and should remain private.
- **Custom API endpoints are restricted for credential safety.** The script requires HTTPS for Nuvio/Cinemeta URLs, except localhost HTTP for local testing.
- **Be careful with `NUVIO_PASSWORD` and `NUVIO_ACCESS_TOKEN`.** Environment variables are convenient for automation but can be exposed by process/debugging/configuration mistakes.
- **Use `--yes` carefully.** It removes the final manual `IMPORT` confirmation before a full library replacement.
- **Keep your backup until verification is complete.** A network/API/service change can always cause unexpected behavior.
- **Review dry-run results after upstream changes.** Nuvio and Cinemeta are external services; API formats or behavior may change over time.

## `.gitignore`

The repository ignores common sensitive/local files including:

```text
.env
trakt-export*.zip
*preview*.json
*nuvio*backup*.json
*nuvio*report*.json
```

Still check `git status` before every commit if you keep personal exports or generated library files inside the repository directory.

## Example first-time workflow

```bash
# 1. Inspect the available options
python3 trakt_lists_to_nuvio.py --help

# 2. Completely offline parse test
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --dry-run --no-enrich

# 3. Enriched preview; Cinemeta is contacted, Nuvio is not
python3 trakt_lists_to_nuvio.py trakt-export.zip --all --dry-run

# 4. Real import; review the merge summary and type IMPORT
python3 trakt_lists_to_nuvio.py trakt-export.zip --all
```

That sequence gives you a chance to inspect the export mapping and metadata before the tool ever writes to Nuvio.
