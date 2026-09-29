# Trakt Lists → Nuvio Library

Import media from a Trakt data export into Nuvio's library format.

The tool reads the Trakt export ZIP, extracts list items and their Trakt/TMDB/IMDb identifiers, then maps those records into the structure Nuvio expects so they can be imported into the Nuvio library.

## Usage

Run the Python script and follow its prompts:

```bash
python3 trakt_lists_to_nuvio.py
```

Keep your Trakt export ZIP available locally. The repository intentionally does not include personal Trakt export data or generated Nuvio library backups.

## Notes

- Designed around Trakt export data and Nuvio's current library/import structure.
- Matching relies on stable media IDs where available rather than only titles.
- Back up your Nuvio data before importing a large library.

The complete cleaned project source is included in `trakt-lists-to-nuvio-source.zip`.
