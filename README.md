> [!WARNING]
> This project is fully vibecoded, probably inefficient, but it does what I wanted lol

# Trakt → Nuvio Library + History Importer

A dependency-free Python tool for importing Trakt personal lists into Nuvio and repairing watched history and Continue Watching, with backups and write verification.

**[Full manual](MANUAL.md)** — complete workflows, migration behavior, options, backups/reports, and troubleshooting, preserved from the original README.

## Quickstart

Requires **Python 3.10+**, a **Trakt account export ZIP**, a **Nuvio account**, and internet access. No `pip install` step is needed.

```sh
git clone https://github.com/RotatingWires/trakt-lists-to-nuvio-library.git
cd trakt-lists-to-nuvio-library
```

Put your export ZIP somewhere private. Replace `trakt-export.zip` below with its path.

To import all personal/custom lists into your Nuvio Library:

```sh
python trakt_lists_to_nuvio.py trakt-export.zip --all
```

The tool prompts for your Nuvio email/password and profile when needed. Password input is hidden and the script does not save it. It reads and backs up your current Library, merges the selected lists, and requires `IMPORT LIBRARY` before writing the complete merged Library payload.

If your Library is already correct and you only want to repair watched history, preview first:

```sh
python trakt_lists_to_nuvio.py trakt-export.zip --history-only --dry-run
```

Then replace watched history and Continue Watching:

```sh
python trakt_lists_to_nuvio.py trakt-export.zip --history-only
```

History-only mode leaves the Library untouched. It backs up tracking data and requires `REPLACE HISTORY` before clearing and rebuilding watched status and Continue Watching. Close Nuvio on other devices first; verify both nuvio.tv and the app afterward.

To import lists and replace history together, use `--all --replace-history`. Read the [combined workflow](MANUAL.md#import-library-lists-and-repair-history-in-one-run) before using it.

## Security and privacy

This project handles credentials and personal viewing data.

The repository itself is intended to contain **source code and documentation only**. It does not need, and should never contain, a real Trakt export, Nuvio backup/report, account email, local username/path, access token, password, or personal watch-history fixture. Test media records use synthetic IDs/titles.

The built-in Nuvio `sb_publishable_...` value is Nuvio's public/publishable client configuration, not a user credential. It is intentionally safe to appear in client-side source; personal session/access tokens are not.

- Do not commit Trakt export ZIPs.
- Do not commit preview/backup/report JSON files.
- Do not hard-code a Nuvio password or access token.
- Keep backups until you have verified both nuvio.tv and the Nuvio app.
- Custom service URLs must use HTTPS, except localhost HTTP for local testing.
- Be cautious with `NUVIO_PASSWORD` and `NUVIO_ACCESS_TOKEN` environment variables.
- `--yes` removes the typed safety confirmations and should not be your first run.
- Close Nuvio on other devices before a destructive history reset to reduce the chance of stale local state racing the migration.

The included `.gitignore` excludes the common Trakt export, preview, backup, and report filenames.

Interactive password entry is preferable to credential environment variables, which can be exposed by shell/process/debugging configuration.

See the manual for [backups, previews, and reports](MANUAL.md#backups-previews-and-reports), [all options](MANUAL.md#main-options), and [app sync troubleshooting](MANUAL.md#if-nuviotv-shows-watched-but-the-app-does-not).
