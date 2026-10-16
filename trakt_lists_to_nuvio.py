#!/usr/bin/env python3
"""Import a Trakt export into Nuvio without sacrificing the existing Library.

Features:
- Non-destructive personal-list -> Nuvio Library merge (with Cinemeta metadata enrichment).
- Optional watched-history replacement: backs up Nuvio tracking, deletes ONLY watched
  status + Continue Watching, verifies those tracking tables are empty, then rebuilds
  them from Trakt and verifies the Nuvio Library membership did not change.
- Supports both watched-movies.json (current Trakt export name) and older numbered names.
- Validates parsed watched counts against user-stats.json before any destructive operation.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_NUVIO_BASE = "https://api.nuvio.tv"
# Nuvio's public client/publishable key, also published in Nuvio's self-host example config.
DEFAULT_NUVIO_PUBLISHABLE_KEY = "sb_publishable_1Clq8rlTVACkdcZuqr6_AD__xUUC_EN"
TRAKT_BASE = "https://trakt.tv"
CINEMETA_BASES = ("https://v3-cinemeta.strem.io", "https://cinemeta-live.strem.io")
LIBRARY_FIELDS = (
    "content_id",
    "content_type",
    "name",
    "poster",
    "poster_shape",
    "background",
    "logo",
    "description",
    "release_info",
    "imdb_rating",
    "genres",
    "addon_base_url",
    "added_at",
)


class ToolError(RuntimeError):
    pass


@dataclass
class ExportList:
    name: str
    trakt_id: str
    slug: str
    filename: str
    item_count: int
    entries: list[dict[str, Any]]


@dataclass
class MappingResult:
    items: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    source_rows: list[dict[str, Any]]
    episode_entries: int
    episode_parent_series: int


@dataclass
class EnrichmentResult:
    items: list[dict[str, Any]]
    enriched: int
    resolved_ids: int
    failed: list[dict[str, Any]]


def parse_iso_ms(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    text = value.strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return int(datetime.now(timezone.utc).timestamp() * 1000)


def stable_content_id(ids: dict[str, Any]) -> str | None:
    imdb = ids.get("imdb")
    if isinstance(imdb, str) and re.fullmatch(r"tt\d+", imdb.strip()):
        return imdb.strip()
    tmdb = ids.get("tmdb")
    if isinstance(tmdb, int) or (isinstance(tmdb, str) and tmdb.isdigit()):
        return f"tmdb:{tmdb}"
    trakt = ids.get("trakt")
    if isinstance(trakt, int) or (isinstance(trakt, str) and str(trakt).isdigit()):
        return f"trakt:{trakt}"
    return None


def stable_aliases(ids: dict[str, Any]) -> list[str]:
    out: list[str] = []
    imdb = ids.get("imdb")
    if isinstance(imdb, str) and re.fullmatch(r"tt\d+", imdb.strip()):
        out.append(imdb.strip())
    tmdb = ids.get("tmdb")
    if isinstance(tmdb, int) or (isinstance(tmdb, str) and str(tmdb).isdigit()):
        out.append(f"tmdb:{tmdb}")
    trakt = ids.get("trakt")
    if isinstance(trakt, int) or (isinstance(trakt, str) and str(trakt).isdigit()):
        out.append(f"trakt:{trakt}")
    return list(dict.fromkeys(out))


def clean_library_item(item: dict[str, Any]) -> dict[str, Any] | None:
    content_id = item.get("content_id")
    if not isinstance(content_id, str) or not content_id.strip():
        return None

    content_type = item.get("content_type")
    if content_type == "show":
        content_type = "series"
    if content_type not in ("movie", "series"):
        if not isinstance(content_type, str) or not content_type:
            return None

    added_at = item.get("added_at")
    try:
        added_at = int(float(added_at))
    except (TypeError, ValueError):
        added_at = int(datetime.now(timezone.utc).timestamp() * 1000)

    genres = item.get("genres")
    if not isinstance(genres, list):
        genres = []

    rating = item.get("imdb_rating")
    if rating not in (None, ""):
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            rating = None
    else:
        rating = None

    return {
        "content_id": content_id.strip(),
        "content_type": content_type,
        "name": item.get("name") or "Untitled",
        "poster": item.get("poster"),
        "poster_shape": item.get("poster_shape") or "POSTER",
        "background": item.get("background"),
        "logo": item.get("logo"),
        "description": item.get("description"),
        "release_info": item.get("release_info"),
        "imdb_rating": rating,
        "genres": genres,
        "addon_base_url": item.get("addon_base_url"),
        "added_at": added_at,
    }


def export_entry_type(entry: dict[str, Any]) -> str:
    typ = entry.get("type")
    if isinstance(typ, str):
        return typ.lower()
    # Compatibility with older/alternate exports that omit `type`.
    if "episode" in entry:
        return "episode"
    if "season" in entry:
        return "season"
    if "movie" in entry:
        return "movie"
    if "show" in entry:
        return "show"
    return "unknown"


def discover_export_lists(zip_path: Path) -> list[ExportList]:
    if not zip_path.exists():
        raise ToolError(f"Trakt export ZIP not found: {zip_path}")
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ToolError(f"Cannot open Trakt export ZIP: {exc}") from exc

    with zf:
        names = zf.namelist()
        if "lists-lists.json" not in names:
            raise ToolError("This ZIP does not contain lists-lists.json from a Trakt export.")
        try:
            metadata = json.load(zf.open("lists-lists.json"))
        except (json.JSONDecodeError, KeyError) as exc:
            raise ToolError(f"Could not read lists-lists.json: {exc}") from exc
        if not isinstance(metadata, list):
            raise ToolError("lists-lists.json has an unexpected format.")

        output: list[ExportList] = []
        for meta in metadata:
            if not isinstance(meta, dict):
                continue
            ids = meta.get("ids") if isinstance(meta.get("ids"), dict) else {}
            trakt_id = str(ids.get("trakt") or "")
            if not trakt_id:
                continue
            prefix = f"lists-list-{trakt_id}-"
            candidates = [n for n in names if n.startswith(prefix) and n.endswith(".json")]
            # Some exporters may omit the slug suffix.
            exact = f"lists-list-{trakt_id}.json"
            if exact in names:
                candidates.insert(0, exact)
            if not candidates:
                continue
            filename = candidates[0]
            try:
                entries = json.load(zf.open(filename))
            except json.JSONDecodeError as exc:
                raise ToolError(f"Could not parse {filename}: {exc}") from exc
            if not isinstance(entries, list):
                continue
            output.append(
                ExportList(
                    name=str(meta.get("name") or f"Trakt list {trakt_id}"),
                    trakt_id=trakt_id,
                    slug=str(ids.get("slug") or ""),
                    filename=filename,
                    item_count=len(entries),
                    entries=entries,
                )
            )
    return output


def type_counts(entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        typ = export_entry_type(entry)
        counts[typ] = counts.get(typ, 0) + 1
    return counts


def select_lists(all_lists: list[ExportList], requested: list[str], select_all: bool) -> list[ExportList]:
    if not all_lists:
        raise ToolError("No personal Trakt lists with item files were found in the export.")
    if select_all:
        return all_lists

    if requested:
        selected: list[ExportList] = []
        missing: list[str] = []
        for needle in requested:
            n = needle.strip().casefold()
            matches = [
                x for x in all_lists
                if n in (x.name.casefold(), x.trakt_id.casefold(), x.slug.casefold())
            ]
            if not matches:
                missing.append(needle)
            else:
                for match in matches:
                    if match not in selected:
                        selected.append(match)
        if missing:
            raise ToolError("List(s) not found: " + ", ".join(missing))
        return selected

    print("\nPersonal Trakt lists in the export:")
    for i, item in enumerate(all_lists, 1):
        counts = ", ".join(f"{v} {k}" for k, v in sorted(type_counts(item.entries).items()))
        print(f"  {i}. {item.name} — {item.item_count} items ({counts})")
    raw = input("\nLists to import [all, or comma-separated numbers]: ").strip()
    if not raw or raw.casefold() == "all":
        return all_lists
    indexes: list[int] = []
    for part in raw.split(","):
        try:
            idx = int(part.strip())
        except ValueError as exc:
            raise ToolError(f"Invalid list selection: {part!r}") from exc
        if idx < 1 or idx > len(all_lists):
            raise ToolError(f"List number out of range: {idx}")
        indexes.append(idx - 1)
    return [all_lists[i] for i in dict.fromkeys(indexes)]


def map_selected_lists(selected: list[ExportList], episode_mode: str) -> MappingResult:
    mapped_by_id: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    episode_entries = 0
    episode_parent_ids: set[str] = set()

    for trakt_list in selected:
        for entry in trakt_list.entries:
            typ = export_entry_type(entry)
            media: dict[str, Any] | None = None
            content_type: str | None = None
            transformed_from_episode = False

            if typ == "movie":
                media = entry.get("movie")
                content_type = "movie"
            elif typ == "show":
                media = entry.get("show")
                content_type = "series"
            elif typ in ("episode", "season"):
                if typ == "episode":
                    episode_entries += 1
                if episode_mode == "skip":
                    skipped.append({
                        "list": trakt_list.name,
                        "type": typ,
                        "name": (entry.get(typ) or {}).get("title") if isinstance(entry.get(typ), dict) else None,
                        "reason": f"{typ} entries were skipped by --episodes skip",
                    })
                    continue
                media = entry.get("show")
                content_type = "series"
                transformed_from_episode = True
            else:
                skipped.append({
                    "list": trakt_list.name,
                    "type": typ,
                    "name": None,
                    "reason": "Nuvio library import supports movie/series titles only",
                })
                continue

            if not isinstance(media, dict):
                skipped.append({
                    "list": trakt_list.name,
                    "type": typ,
                    "name": None,
                    "reason": "Entry is missing movie/show metadata",
                })
                continue

            ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
            content_id = stable_content_id(ids)
            if not content_id:
                skipped.append({
                    "list": trakt_list.name,
                    "type": typ,
                    "name": media.get("title"),
                    "reason": "No usable IMDb, TMDB, or Trakt ID",
                })
                continue

            year = media.get("year")
            item = {
                "content_id": content_id,
                "content_type": content_type,
                "name": media.get("title") or "Untitled",
                "poster": None,
                "poster_shape": "POSTER",
                "background": None,
                "logo": None,
                "description": media.get("overview") if isinstance(media.get("overview"), str) else None,
                "release_info": str(year) if year not in (None, "") else None,
                "imdb_rating": media.get("rating") if isinstance(media.get("rating"), (int, float)) else None,
                "genres": media.get("genres") if isinstance(media.get("genres"), list) else [],
                "addon_base_url": None,
                "added_at": parse_iso_ms(entry.get("listed_at") or entry.get("updated_at")),
                "_aliases": stable_aliases(ids),
            }
            source_row = {
                "list": trakt_list.name,
                "entry_type": typ,
                "content_id": content_id,
                "content_type": content_type,
                "name": item["name"],
                "year": year,
                "converted_to_parent_series": transformed_from_episode,
            }
            if typ == "episode" and isinstance(entry.get("episode"), dict):
                ep = entry["episode"]
                source_row["episode"] = {
                    "title": ep.get("title"),
                    "season": ep.get("season"),
                    "number": ep.get("number"),
                }
            source_rows.append(source_row)

            if transformed_from_episode:
                episode_parent_ids.add(content_id)

            previous = mapped_by_id.get(content_id)
            if previous is None or item["added_at"] > previous["added_at"]:
                mapped_by_id[content_id] = item

    items = sorted(mapped_by_id.values(), key=lambda x: x["added_at"], reverse=True)
    return MappingResult(
        items=items,
        skipped=skipped,
        source_rows=source_rows,
        episode_entries=episode_entries,
        episode_parent_series=len(episode_parent_ids),
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _year_from_text(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"(?:19|20)\d{2}", str(value))
    return match.group(0) if match else None


def _get_json_url(url: str, timeout: int = 15) -> dict[str, Any] | None:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "trakt-lists-to-nuvio/2.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _cinemeta_meta(content_type: str, imdb_id: str, bases: list[str], timeout: int) -> dict[str, Any] | None:
    kind = "series" if content_type == "series" else "movie"
    for base in bases:
        data = _get_json_url(f"{base.rstrip('/')}/meta/{kind}/{urllib.parse.quote(imdb_id)}.json", timeout=timeout)
        meta = data.get("meta") if isinstance(data, dict) else None
        if isinstance(meta, dict) and meta:
            return meta
    return None


def _cinemeta_search(item: dict[str, Any], bases: list[str], timeout: int) -> tuple[str | None, dict[str, Any] | None]:
    name = str(item.get("name") or "").strip()
    if not name:
        return None, None
    kind = "series" if item.get("content_type") == "series" else "movie"
    wanted_title = _normalize_title(name)
    wanted_year = _year_from_text(item.get("release_info"))
    encoded = urllib.parse.quote(name, safe="")

    for base in bases:
        data = _get_json_url(f"{base.rstrip('/')}/catalog/{kind}/top/search={encoded}.json", timeout=timeout)
        metas = data.get("metas") if isinstance(data, dict) else None
        if not isinstance(metas, list):
            continue
        candidates = [m for m in metas if isinstance(m, dict) and isinstance(m.get("id"), str) and re.fullmatch(r"tt\d+", m["id"])]
        if not candidates:
            continue

        exact_title = [m for m in candidates if _normalize_title(str(m.get("name") or "")) == wanted_title]
        pool = exact_title or candidates
        if wanted_year:
            exact_year = [m for m in pool if _year_from_text(m.get("releaseInfo") or m.get("year")) == wanted_year]
            if exact_year:
                pool = exact_year
        if len(pool) > 1 and not exact_title:
            # Avoid guessing if the search is ambiguous and the title itself did not match.
            continue
        chosen = pool[0]
        imdb_id = chosen["id"]
        full = _cinemeta_meta(str(item.get("content_type")), imdb_id, bases, timeout)
        return imdb_id, full or chosen
    return None, None


def _apply_cinemeta(item: dict[str, Any], meta: dict[str, Any]) -> bool:
    changed = False

    def fill(key: str, value: Any) -> None:
        nonlocal changed
        current = item.get(key)
        blank = current is None or current == "" or (isinstance(current, list) and not current)
        if blank and value not in (None, "", []):
            item[key] = value
            changed = True

    fill("poster", _text(meta.get("poster")))
    shape = _text(meta.get("posterShape"))
    if shape:
        shape_up = shape.upper()
        if shape_up in ("POSTER", "SQUARE", "LANDSCAPE") and item.get("poster_shape") != shape_up:
            item["poster_shape"] = shape_up
            changed = True
    fill("background", _text(meta.get("background") or meta.get("banner")))
    fill("logo", _text(meta.get("logo")))
    fill("description", _text(meta.get("description")))
    fill("release_info", _text(meta.get("releaseInfo")) or _year_from_text(meta.get("year")))

    rating = meta.get("imdbRating")
    if rating not in (None, "") and item.get("imdb_rating") in (None, ""):
        try:
            item["imdb_rating"] = float(rating)
            changed = True
        except (TypeError, ValueError):
            pass

    genres = meta.get("genres")
    if isinstance(genres, list) and genres and not item.get("genres"):
        item["genres"] = [str(x) for x in genres if str(x).strip()]
        changed = True
    return changed


def enrich_one_item(item: dict[str, Any], bases: list[str], timeout: int) -> tuple[dict[str, Any], bool, bool, str | None]:
    out = dict(item)
    out["_aliases"] = list(item.get("_aliases") or [])
    cid = str(out.get("content_id") or "")
    meta: dict[str, Any] | None = None
    resolved = False

    if re.fullmatch(r"tt\d+", cid):
        meta = _cinemeta_meta(str(out.get("content_type")), cid, bases, timeout)
    else:
        imdb_id, meta = _cinemeta_search(out, bases, timeout)
        if imdb_id:
            if cid and cid not in out["_aliases"]:
                out["_aliases"].append(cid)
            if imdb_id not in out["_aliases"]:
                out["_aliases"].append(imdb_id)
            out["content_id"] = imdb_id
            resolved = True

    if not meta:
        return out, False, resolved, "No Cinemeta metadata found"
    changed = _apply_cinemeta(out, meta)
    return out, changed, resolved, None


def enrich_items(items: list[dict[str, Any]], bases: list[str], workers: int = 8, timeout: int = 15) -> EnrichmentResult:
    if not items:
        return EnrichmentResult([], 0, 0, [])
    workers = max(1, min(workers, 16))
    enriched = 0
    resolved = 0
    failed: list[dict[str, Any]] = []
    output: list[dict[str, Any] | None] = [None] * len(items)

    print(f"Fetching artwork/details from Cinemeta for {len(items)} title(s)…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(enrich_one_item, item, bases, timeout): i for i, item in enumerate(items)}
        done = 0
        for future in as_completed(futures):
            i = futures[future]
            try:
                out, changed, did_resolve, error = future.result()
            except Exception as exc:  # keep one bad title from aborting the batch
                out, changed, did_resolve, error = dict(items[i]), False, False, str(exc)
            output[i] = out
            if changed:
                enriched += 1
            if did_resolve:
                resolved += 1
            if error:
                failed.append({
                    "content_id": out.get("content_id"),
                    "name": out.get("name"),
                    "reason": error,
                })
            done += 1
            if done % 20 == 0 or done == len(items):
                print(f"  metadata: {done}/{len(items)}")

    # Re-dedupe after title search may have converted fallback IDs to IMDb IDs.
    by_id: dict[str, dict[str, Any]] = {}
    for raw in output:
        if raw is None:
            continue
        cid = str(raw.get("content_id") or "")
        if not cid:
            continue
        previous = by_id.get(cid)
        if previous is None or int(raw.get("added_at") or 0) > int(previous.get("added_at") or 0):
            by_id[cid] = raw
    final = sorted(by_id.values(), key=lambda x: int(x.get("added_at") or 0), reverse=True)
    return EnrichmentResult(final, enriched, resolved, failed)

@dataclass
class HistoryPlan:
    watched_items: list[dict[str, Any]]
    progress_items: list[dict[str, Any]]
    movie_states: int
    episode_states: int
    movie_plays: int
    episode_plays: int
    source_history_rows: int
    stats_expected_movies: int | None
    stats_expected_episodes: int | None
    resolved_parent_ids: int
    unresolved_fallback_ids: list[dict[str, Any]]
    unresolved_progress: list[dict[str, Any]]
    warnings: list[str]


def _zip_json_files(zip_path: Path) -> dict[str, Any]:
    if not zip_path.exists():
        raise ToolError(f"Trakt export ZIP not found: {zip_path}")
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ToolError(f"Cannot open Trakt export ZIP: {exc}") from exc
    out: dict[str, Any] = {}
    with zf:
        for raw_name in zf.namelist():
            name = Path(raw_name).name
            if not name.lower().endswith(".json"):
                continue
            try:
                out[name] = json.load(zf.open(raw_name))
            except (json.JSONDecodeError, OSError) as exc:
                raise ToolError(f"Could not parse {raw_name}: {exc}") from exc
    return out


def _json_list(files: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = files.get(name)
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def _matching_json_rows(files: dict[str, Any], pattern: str) -> list[dict[str, Any]]:
    rx = re.compile(pattern, re.I)
    rows: list[dict[str, Any]] = []
    for name in sorted(files, key=lambda x: [int(y) if y.isdigit() else y for y in re.split(r"(\d+)", x)]):
        if rx.fullmatch(name):
            rows.extend(_json_list(files, name))
    return rows


def _latest(existing: dict[str, Any] | None, candidate: dict[str, Any], field: str) -> dict[str, Any]:
    if existing is None:
        return candidate
    return candidate if int(candidate.get(field) or 0) > int(existing.get(field) or 0) else existing


def watched_item_key(item: dict[str, Any]) -> str:
    return f"{str(item.get('content_id') or '').strip()}|{item.get('season') if item.get('season') is not None else ''}|{item.get('episode') if item.get('episode') is not None else ''}"


def progress_item_key(item: dict[str, Any]) -> str:
    key = str(item.get("progress_key") or "").strip()
    if key:
        return key
    cid = str(item.get("content_id") or "").strip()
    season = item.get("season")
    episode = item.get("episode")
    if season is not None and episode is not None:
        return f"{cid}_s{season}e{episode}"
    return cid


def _strip_private(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not str(k).startswith("_")}


def parse_runtime_minutes(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if number > 0 else None
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    hours_match = re.search(r"(\d+(?:\.\d+)?)\s*h", raw)
    minutes_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|m\b)", raw)
    hours = float(hours_match.group(1)) if hours_match else 0.0
    minutes = float(minutes_match.group(1)) if minutes_match else 0.0
    total = hours * 60.0 + minutes
    if total > 0:
        return total
    numeric = re.sub(r"[^0-9.]", "", raw)
    try:
        number = float(numeric)
    except ValueError:
        return None
    return number if number > 0 else None


def _runtime_minutes_from_meta(meta: dict[str, Any] | None, season: int | None = None, episode: int | None = None) -> float | None:
    if not isinstance(meta, dict):
        return None
    if season is not None and episode is not None:
        videos = meta.get("videos")
        if isinstance(videos, list):
            for video in videos:
                if not isinstance(video, dict):
                    continue
                try:
                    s = int(video.get("season"))
                    e = int(video.get("episode"))
                except (TypeError, ValueError):
                    continue
                if s == season and e == episode:
                    runtime = parse_runtime_minutes(video.get("runtime"))
                    if runtime:
                        return runtime
    return parse_runtime_minutes(meta.get("runtime"))


def _resolve_media_content_id(
    media: dict[str, Any],
    content_type: str,
    bases: list[str],
    timeout: int,
    cache: dict[tuple[str, str, str, str], tuple[str | None, dict[str, Any] | None]],
    need_meta: bool = False,
) -> tuple[str | None, dict[str, Any] | None, bool]:
    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    raw_id = stable_content_id(ids)
    if not raw_id:
        return None, None, False
    imdb = ids.get("imdb")
    if isinstance(imdb, str) and re.fullmatch(r"tt\d+", imdb.strip()):
        imdb_id = imdb.strip()
        return imdb_id, (_cinemeta_meta(content_type, imdb_id, bases, timeout) if need_meta else None), False

    title = str(media.get("title") or "").strip()
    year = str(media.get("year") or "")
    cache_key = (content_type, raw_id, title.casefold(), year)
    if cache_key not in cache:
        dummy = {
            "content_id": raw_id,
            "content_type": content_type,
            "name": title,
            "release_info": year or None,
        }
        cache[cache_key] = _cinemeta_search(dummy, bases, timeout)
    resolved_id, meta = cache[cache_key]
    if resolved_id:
        return resolved_id, meta, True
    return raw_id, meta, False


def build_history_plan(zip_path: Path, bases: list[str], timeout: int = 15, include_progress: bool = True) -> HistoryPlan:
    """Map Trakt watched state and Continue Watching into Nuvio's current-state model.

    Trakt can contain multiple play events for the same movie/episode. Nuvio Sync stores
    one current watched state per movie/episode, so the newest timestamp wins by design.
    """
    files = _zip_json_files(zip_path)
    stats = files.get("user-stats.json") if isinstance(files.get("user-stats.json"), dict) else {}
    movie_stats = stats.get("movies") if isinstance(stats.get("movies"), dict) else {}
    episode_stats = stats.get("episodes") if isinstance(stats.get("episodes"), dict) else {}
    expected_movies = int(movie_stats.get("watched")) if isinstance(movie_stats.get("watched"), int) else None
    expected_episodes = int(episode_stats.get("watched")) if isinstance(episode_stats.get("watched"), int) else None
    movie_plays = int(movie_stats.get("plays")) if isinstance(movie_stats.get("plays"), int) else 0
    episode_plays = int(episode_stats.get("plays")) if isinstance(episode_stats.get("plays"), int) else 0

    # IMPORTANT: current Trakt exports use watched-movies.json (no numeric suffix),
    # while some older exports used watched-movies-1.json. Support both.
    movie_summary = _matching_json_rows(files, r"watched-movies(?:-\d+)?\.json")
    history_rows = _matching_json_rows(files, r"watched-history-\d+\.json")
    cache: dict[tuple[str, str, str, str], tuple[str | None, dict[str, Any] | None]] = {}
    resolved_parent_ids = 0
    unresolved_fallback: list[dict[str, Any]] = []

    watched_map: dict[str, dict[str, Any]] = {}

    def add_movie(entry: dict[str, Any], when_value: Any) -> None:
        nonlocal resolved_parent_ids
        movie = entry.get("movie")
        if not isinstance(movie, dict):
            return
        cid, _meta, resolved = _resolve_media_content_id(movie, "movie", bases, timeout, cache)
        if not cid:
            return
        if resolved:
            resolved_parent_ids += 1
        if not str(cid).startswith("tt"):
            unresolved_fallback.append({"type": "movie", "title": movie.get("title"), "content_id": cid})
        item = {
            "content_id": cid,
            "content_type": "movie",
            "title": movie.get("title") or cid,
            "season": None,
            "episode": None,
            "watched_at": parse_iso_ms(when_value),
            "_parent_name": movie.get("title"),
            "_year": movie.get("year"),
            "_aliases": stable_aliases(movie.get("ids") if isinstance(movie.get("ids"), dict) else {}),
        }
        key = watched_item_key(item)
        watched_map[key] = _latest(watched_map.get(key), item, "watched_at")

    for entry in movie_summary:
        add_movie(entry, entry.get("last_watched_at") or entry.get("last_updated_at"))

    # Movie history is also used as a fallback. This is what protects us if Trakt changes
    # the summary filename again or an export omits watched-movies.json entirely.
    for entry in history_rows:
        if str(entry.get("type") or "").lower() == "movie":
            add_movie(entry, entry.get("watched_at"))

    for entry in history_rows:
        if str(entry.get("type") or "").lower() != "episode":
            continue
        show = entry.get("show")
        ep = entry.get("episode")
        if not isinstance(show, dict) or not isinstance(ep, dict):
            continue
        try:
            season = int(ep.get("season"))
            episode = int(ep.get("number"))
        except (TypeError, ValueError):
            continue
        cid, _meta, resolved = _resolve_media_content_id(show, "series", bases, timeout, cache)
        if not cid:
            continue
        if resolved:
            resolved_parent_ids += 1
        if not str(cid).startswith("tt"):
            unresolved_fallback.append({
                "type": "series",
                "title": show.get("title"),
                "year": show.get("year"),
                "content_id": cid,
                "season": season,
                "episode": episode,
            })
        episode_title = ep.get("title")
        title = episode_title or f"{show.get('title') or cid} S{season:02d}E{episode:02d}"
        item = {
            "content_id": cid,
            "content_type": "series",
            "title": title,
            "season": season,
            "episode": episode,
            "watched_at": parse_iso_ms(entry.get("watched_at")),
            "_parent_name": show.get("title"),
            "_year": show.get("year"),
            "_aliases": stable_aliases(show.get("ids") if isinstance(show.get("ids"), dict) else {}),
        }
        key = watched_item_key(item)
        watched_map[key] = _latest(watched_map.get(key), item, "watched_at")

    movie_states = sum(1 for x in watched_map.values() if x.get("content_type") == "movie")
    episode_states = sum(1 for x in watched_map.values() if x.get("content_type") == "series" and x.get("season") is not None)
    warnings: list[str] = []

    # These are strong corruption guards. The old web importer failed here silently because
    # its test fixture used watched-movies-1.json while this real export uses watched-movies.json.
    if expected_movies is not None and movie_states != expected_movies:
        raise ToolError(
            f"History sanity check failed: Trakt user-stats says {expected_movies} watched movies, "
            f"but the parser found {movie_states}. No Nuvio changes will be made."
        )
    if expected_episodes is not None and episode_states != expected_episodes:
        raise ToolError(
            f"History sanity check failed: Trakt user-stats says {expected_episodes} watched episodes, "
            f"but the parser found {episode_states}. No Nuvio changes will be made."
        )

    total_history_rows = len(history_rows)
    expected_plays = movie_plays + episode_plays
    if expected_plays and total_history_rows != expected_plays:
        warnings.append(
            f"Trakt stats report {expected_plays} plays but watched-history files contain {total_history_rows} rows. "
            "Watched state is still based on the newest row per movie/episode."
        )

    progress_map: dict[str, dict[str, Any]] = {}
    unresolved_progress: list[dict[str, Any]] = []
    if include_progress:
        for entry in _json_list(files, "watched-playback.json"):
            typ = str(entry.get("type") or "").lower()
            media = entry.get("movie") if typ == "movie" else entry.get("show") if typ == "episode" else None
            if not isinstance(media, dict):
                continue
            content_type = "movie" if typ == "movie" else "series"
            cid, meta, resolved = _resolve_media_content_id(media, content_type, bases, timeout, cache, need_meta=True)
            if not cid:
                continue
            if resolved:
                resolved_parent_ids += 1
            try:
                percent = float(entry.get("progress"))
            except (TypeError, ValueError):
                continue
            if not (0 < percent < 100):
                continue
            season = episode = None
            if typ == "episode":
                ep = entry.get("episode")
                if not isinstance(ep, dict):
                    continue
                try:
                    season = int(ep.get("season"))
                    episode = int(ep.get("number"))
                except (TypeError, ValueError):
                    continue
            minutes = _runtime_minutes_from_meta(meta, season, episode)
            if not minutes:
                unresolved_progress.append({
                    "title": media.get("title"),
                    "content_id": cid,
                    "season": season,
                    "episode": episode,
                    "progress_percent": percent,
                    "reason": "Could not resolve runtime from Cinemeta",
                })
                continue
            duration = max(1, int(round(minutes * 60_000)))
            position = max(1, min(duration - 1, int(round(duration * percent / 100.0))))
            video_id = f"{cid}:{season}:{episode}" if season is not None and episode is not None else cid
            progress_key = f"{cid}_s{season}e{episode}" if season is not None and episode is not None else cid
            item = {
                "content_id": cid,
                "content_type": content_type,
                "video_id": video_id,
                "season": season,
                "episode": episode,
                "position": position,
                "duration": duration,
                "last_watched": parse_iso_ms(entry.get("paused_at")),
                "progress_key": progress_key,
                "_progress_percent": percent,
                "_parent_name": media.get("title"),
                "_year": media.get("year"),
                "_aliases": stable_aliases(media.get("ids") if isinstance(media.get("ids"), dict) else {}),
            }
            key = progress_item_key(item)
            progress_map[key] = _latest(progress_map.get(key), item, "last_watched")

    # De-duplicate the warning list (e.g. all episodes of one TMDB-only show).
    seen_fallback: set[tuple[Any, ...]] = set()
    fallback_unique: list[dict[str, Any]] = []
    for row in unresolved_fallback:
        sig = (row.get("type"), row.get("title"), row.get("content_id"))
        if sig not in seen_fallback:
            seen_fallback.add(sig)
            fallback_unique.append(row)
    if fallback_unique:
        warnings.append(
            f"{len(fallback_unique)} title(s) could not be normalized to IMDb IDs. They will use TMDB/Trakt IDs, "
            "which may not match the ID used by a Nuvio metadata addon."
        )

    return HistoryPlan(
        watched_items=sorted(watched_map.values(), key=lambda x: int(x.get("watched_at") or 0), reverse=True),
        progress_items=sorted(progress_map.values(), key=lambda x: int(x.get("last_watched") or 0), reverse=True),
        movie_states=movie_states,
        episode_states=episode_states,
        movie_plays=movie_plays,
        episode_plays=episode_plays,
        source_history_rows=total_history_rows,
        stats_expected_movies=expected_movies,
        stats_expected_episodes=expected_episodes,
        resolved_parent_ids=resolved_parent_ids,
        unresolved_fallback_ids=fallback_unique,
        unresolved_progress=unresolved_progress,
        warnings=warnings,
    )


def history_preview(plan: HistoryPlan) -> dict[str, Any]:
    return {
        "movie_watched_states": plan.movie_states,
        "episode_watched_states": plan.episode_states,
        "movie_plays_in_trakt": plan.movie_plays,
        "episode_plays_in_trakt": plan.episode_plays,
        "history_rows_in_export": plan.source_history_rows,
        "nuvio_watched_states": len(plan.watched_items),
        "continue_watching_items": len(plan.progress_items),
        "resolved_fallback_parent_ids_to_imdb": plan.resolved_parent_ids,
        "unresolved_fallback_ids": plan.unresolved_fallback_ids,
        "unresolved_progress": plan.unresolved_progress,
        "warnings": plan.warnings,
        "watched_items": [_strip_private(x) for x in plan.watched_items],
        "progress_items": [_strip_private(x) for x in plan.progress_items],
    }


def _chunk(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _library_key_set(items: list[dict[str, Any]]) -> set[str]:
    return {
        f"{str(x.get('content_type') or '').lower()}|{str(x.get('content_id') or '')}"
        for x in items if isinstance(x, dict) and x.get("content_id")
    }


def _tracking_backup_payload(profile_id: int, library: list[dict[str, Any]], watched: list[dict[str, Any]], progress: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile_id": profile_id,
        "library_count": len(library),
        "watched_count": len(watched),
        "progress_count": len(progress),
        "library": library,
        "watched_items": watched,
        "watch_progress": progress,
    }


def replace_tracking_from_plan(
    api: "NuvioAPI",
    profile_id: int,
    plan: HistoryPlan,
    library_before: list[dict[str, Any]],
    backup_dir: Path,
    stamp: str,
    yes: bool,
) -> tuple[Path, dict[str, Any]]:
    """Destructively replace watched/progress only. The Library endpoint is never called here."""
    watched_before = api.pull_watched(profile_id)
    progress_before = api.pull_progress(profile_id, limit=10_000)
    backup_path = backup_dir / f"nuvio-tracking-backup-profile-{profile_id}-{stamp}.json"
    write_json(backup_path, _tracking_backup_payload(profile_id, library_before, watched_before, progress_before))

    print("\nTracking replacement summary:")
    print(f"  Existing Nuvio watched states: {len(watched_before)}")
    print(f"  Trakt watched states to write: {len(plan.watched_items)} ({plan.movie_states} movies + {plan.episode_states} episodes)")
    print(f"  Existing Continue Watching:    {len(progress_before)}")
    print(f"  Trakt Continue Watching:       {len(plan.progress_items)}")
    print(f"  Library items preserved:       {len(library_before)}")
    print(f"  Tracking backup:               {backup_path}")

    if plan.unresolved_progress:
        raise ToolError(
            f"{len(plan.unresolved_progress)} Trakt Continue Watching item(s) have unresolved runtimes. "
            "Refusing to delete current tracking data until every playback row can be rebuilt. "
            f"See the preview/backup details."
        )

    if not yes:
        print("\nThis will DELETE all Nuvio watched-status and Continue Watching rows for this profile, then rebuild them from Trakt.")
        print("It does NOT delete or replace your Nuvio Library.")
        confirm = input("Type REPLACE HISTORY to continue: ").strip()
        if confirm != "REPLACE HISTORY":
            raise ToolError("History replacement cancelled before any tracking data was deleted.")

    origin = f"trakt-import-{uuid.uuid4()}"
    print("Clearing Nuvio watched status and Continue Watching…")
    if watched_before:
        keys = [
            {
                "content_id": str(x.get("content_id") or ""),
                **({"season": int(x["season"])} if x.get("season") is not None else {}),
                **({"episode": int(x["episode"])} if x.get("episode") is not None else {}),
            }
            for x in watched_before if x.get("content_id")
        ]
        for batch in _chunk(keys, 300):
            api.delete_watched(profile_id, batch, origin)
    if progress_before:
        keys = [progress_item_key(x) for x in progress_before if progress_item_key(x)]
        for batch in _chunk(keys, 300):
            api.delete_progress(profile_id, batch, origin)

    # Nuvio Sync can be briefly eventually-consistent. Re-read and delete any stragglers.
    remaining_watched: list[dict[str, Any]] = []
    remaining_progress: list[dict[str, Any]] = []
    for delay in (0.5, 1.2, 2.5, 5.0):
        time.sleep(delay)
        remaining_watched = api.pull_watched(profile_id)
        remaining_progress = api.pull_progress(profile_id, limit=10_000)
        if not remaining_watched and not remaining_progress:
            break
        if remaining_watched:
            retry_keys = [
                {
                    "content_id": str(x.get("content_id") or ""),
                    **({"season": int(x["season"])} if x.get("season") is not None else {}),
                    **({"episode": int(x["episode"])} if x.get("episode") is not None else {}),
                }
                for x in remaining_watched if x.get("content_id")
            ]
            for batch in _chunk(retry_keys, 300):
                api.delete_watched(profile_id, batch, origin)
        if remaining_progress:
            retry_keys = [progress_item_key(x) for x in remaining_progress if progress_item_key(x)]
            for batch in _chunk(retry_keys, 300):
                api.delete_progress(profile_id, batch, origin)

    if remaining_watched or remaining_progress:
        raise ToolError(
            "Nuvio did not become empty after repeated tracking-delete verification. "
            f"Stopped before importing new history. Backup: {backup_path}"
        )

    print("Tracking clear verified. Rebuilding from Trakt…")
    watched_clean = [_strip_private(x) for x in plan.watched_items]
    progress_clean = [_strip_private(x) for x in plan.progress_items]
    for batch in _chunk(watched_clean, 300):
        api.push_watched(profile_id, batch, origin)
    for batch in _chunk(progress_clean, 200):
        api.push_progress(profile_id, batch, origin)

    expected_watched = {watched_item_key(x) for x in watched_clean}
    expected_progress = {progress_item_key(x) for x in progress_clean}
    watched_after: list[dict[str, Any]] = []
    progress_after: list[dict[str, Any]] = []
    missing_watched: set[str] = set()
    missing_progress: set[str] = set()
    for delay in (0.5, 1.2, 2.5, 5.0):
        time.sleep(delay)
        watched_after = api.pull_watched(profile_id)
        progress_after = api.pull_progress(profile_id, limit=max(10_000, len(expected_progress) + 500))
        actual_watched = {watched_item_key(x) for x in watched_after}
        actual_progress = {progress_item_key(x) for x in progress_after}
        missing_watched = expected_watched - actual_watched
        missing_progress = expected_progress - actual_progress
        if not missing_watched and not missing_progress:
            break

    library_after = api.pull_library(profile_id)
    library_before_keys = _library_key_set(library_before)
    library_after_keys = _library_key_set(library_after)
    library_changed = library_before_keys != library_after_keys

    report = {
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile_id": profile_id,
        "backup_file": str(backup_path),
        "watched_before": len(watched_before),
        "watched_expected": len(expected_watched),
        "watched_after": len(watched_after),
        "missing_watched_keys": sorted(missing_watched),
        "progress_before": len(progress_before),
        "progress_expected": len(expected_progress),
        "progress_after": len(progress_after),
        "missing_progress_keys": sorted(missing_progress),
        "library_before": len(library_before),
        "library_after": len(library_after),
        "library_key_set_changed": library_changed,
    }
    if missing_watched or missing_progress:
        raise ToolError(
            f"History was written but verification is missing {len(missing_watched)} watched row(s) and "
            f"{len(missing_progress)} progress row(s). Backup: {backup_path}"
        )
    if library_changed:
        raise ToolError(
            "Tracking import verified, but the Nuvio Library key set changed while the operation was running. "
            "The script never called the Library write endpoint during history replacement, so do not auto-restore; "
            f"inspect concurrent device changes and the backup at {backup_path}."
        )
    print(f"Verified {len(expected_watched)} watched states and {len(expected_progress)} Continue Watching item(s).")
    print(f"Verified Library membership stayed unchanged at {len(library_after)} item(s).")
    return backup_path, report


class NuvioAPI:
    def __init__(self, base_url: str, publishable_key: str, access_token: str | None = None, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.publishable_key = publishable_key
        self.access_token = access_token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Any = None, auth: bool = True) -> Any:
        url = self.base_url + path
        headers = {
            "apikey": self.publishable_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "trakt-lists-to-nuvio/2.0",
        }
        if auth:
            if not self.access_token:
                raise ToolError("Nuvio access token is missing. Log in first.")
            headers["Authorization"] = f"Bearer {self.access_token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                detail = parsed.get("msg") or parsed.get("message") or parsed.get("error_description") or parsed.get("error") or raw
            except json.JSONDecodeError:
                detail = raw or str(exc)
            raise ToolError(f"Nuvio API HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ToolError(f"Could not reach Nuvio API at {self.base_url}: {exc.reason}") from exc

    def login(self, email: str, password: str) -> None:
        path = "/auth/v1/token?grant_type=password"
        result = self._request("POST", path, {"email": email, "password": password}, auth=False)
        if not isinstance(result, dict) or not result.get("access_token"):
            raise ToolError("Nuvio login succeeded without returning an access token.")
        self.access_token = str(result["access_token"])

    def rpc(self, name: str, body: dict[str, Any]) -> Any:
        return self._request("POST", f"/rest/v1/rpc/{name}", body, auth=True)

    def pull_profiles(self) -> list[dict[str, Any]]:
        result = self.rpc("sync_pull_profiles", {})
        if result is None:
            return []
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict)]
        if isinstance(result, dict):
            # Be tolerant of wrappers used by alternate/self-host deployments.
            for key in ("profiles", "items", "data"):
                if isinstance(result.get(key), list):
                    return [x for x in result[key] if isinstance(x, dict)]
        raise ToolError("Nuvio returned an unexpected profile response.")

    def pull_library(self, profile_id: int, page_size: int = 500) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = self.rpc(
                "sync_pull_library",
                {"p_profile_id": profile_id, "p_limit": page_size, "p_offset": offset},
            )
            if result is None:
                batch: list[dict[str, Any]] = []
            elif isinstance(result, list):
                batch = [x for x in result if isinstance(x, dict)]
            elif isinstance(result, dict):
                possible = result.get("items") or result.get("data") or result.get("library")
                if isinstance(possible, list):
                    batch = [x for x in possible if isinstance(x, dict)]
                else:
                    raise ToolError("Nuvio returned an unexpected library response.")
            else:
                raise ToolError("Nuvio returned an unexpected library response.")
            out.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)
        return out

    def push_library(self, profile_id: int, items: list[dict[str, Any]]) -> Any:
        return self.rpc("sync_push_library", {"p_profile_id": profile_id, "p_items": items})

    @staticmethod
    def _list_result(result: Any, *wrapper_keys: str) -> list[dict[str, Any]]:
        if result is None:
            return []
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict)]
        if isinstance(result, dict):
            for key in wrapper_keys:
                value = result.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        raise ToolError("Nuvio returned an unexpected tracking response.")

    def pull_watched(self, profile_id: int, page_size: int = 900) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self.rpc(
                "sync_pull_watched_items",
                {"p_profile_id": profile_id, "p_page": page, "p_page_size": page_size},
            )
            batch = self._list_result(result, "items", "data", "watched_items")
            out.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return out

    def pull_progress(self, profile_id: int, limit: int = 10_000) -> list[dict[str, Any]]:
        result = self.rpc(
            "sync_pull_watch_progress",
            {"p_profile_id": profile_id, "p_limit": int(limit)},
        )
        return self._list_result(result, "items", "data", "progress")

    def push_watched(self, profile_id: int, items: list[dict[str, Any]], origin_client_id: str) -> Any:
        return self.rpc(
            "sync_push_watched_items",
            {"p_profile_id": profile_id, "p_items": items, "p_origin_client_id": origin_client_id},
        )

    def delete_watched(self, profile_id: int, keys: list[dict[str, Any]], origin_client_id: str) -> Any:
        return self.rpc(
            "sync_delete_watched_items",
            {"p_profile_id": profile_id, "p_keys": keys, "p_origin_client_id": origin_client_id},
        )

    def push_progress(self, profile_id: int, entries: list[dict[str, Any]], origin_client_id: str) -> Any:
        return self.rpc(
            "sync_push_watch_progress",
            {"p_profile_id": profile_id, "p_entries": entries, "p_origin_client_id": origin_client_id},
        )

    def delete_progress(self, profile_id: int, keys: list[str], origin_client_id: str) -> Any:
        return self.rpc(
            "sync_delete_watch_progress",
            {"p_profile_id": profile_id, "p_keys": keys, "p_origin_client_id": origin_client_id},
        )


def profile_identifier(profile: dict[str, Any]) -> int | None:
    for key in ("profile_id", "id", "profile_index", "index"):
        value = profile.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def profile_label(profile: dict[str, Any], fallback: str) -> str:
    for key in ("name", "profile_name", "display_name"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def choose_profile(profiles: list[dict[str, Any]], requested: int | None) -> int:
    normalized = [(profile_identifier(p), p) for p in profiles]
    normalized = [(pid, p) for pid, p in normalized if pid is not None]
    if not normalized:
        raise ToolError("No usable Nuvio profiles were returned for this account.")

    if requested is not None:
        if requested not in {pid for pid, _ in normalized}:
            available = ", ".join(str(pid) for pid, _ in normalized)
            raise ToolError(f"Nuvio profile {requested} was not found. Available IDs: {available}")
        return requested

    if len(normalized) == 1:
        pid, profile = normalized[0]
        print(f"Using Nuvio profile {pid}: {profile_label(profile, 'default profile')}")
        return pid

    print("\nNuvio profiles:")
    for pid, profile in normalized:
        print(f"  {pid}. {profile_label(profile, f'Profile {pid}')}")
    raw = input("Profile ID to import into: ").strip()
    try:
        chosen = int(raw)
    except ValueError as exc:
        raise ToolError("Profile ID must be a number.") from exc
    if chosen not in {pid for pid, _ in normalized}:
        raise ToolError(f"Profile {chosen} was not returned by Nuvio.")
    return chosen


def safe_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == []


def merge_library(
    existing_raw: list[dict[str, Any]],
    imported: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int, int, list[str]]:
    existing: list[dict[str, Any]] = []
    malformed: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}

    for i, raw in enumerate(existing_raw):
        clean = clean_library_item(raw)
        if clean is None:
            malformed.append(f"existing item #{i + 1}")
            continue
        cid = clean["content_id"]
        if cid not in by_id:
            by_id[cid] = clean
            existing.append(clean)

    added = 0
    updated = 0
    unchanged = 0
    for raw in imported:
        clean = clean_library_item(raw)
        if clean is None:
            continue
        canonical = clean["content_id"]
        aliases = list(dict.fromkeys([canonical] + [str(x) for x in raw.get("_aliases", []) if x]))
        match_id = next((alias for alias in aliases if alias in by_id), None)

        if match_id is None:
            by_id[canonical] = clean
            existing.append(clean)
            added += 1
            continue

        target = by_id[match_id]
        changed = False

        # If the previous importer had to use a tmdb:/trakt: fallback ID but Cinemeta
        # now resolved the title to IMDb, normalize the stored ID without duplicating it.
        if canonical != match_id and canonical.startswith("tt") and canonical not in by_id:
            del by_id[match_id]
            target["content_id"] = canonical
            by_id[canonical] = target
            changed = True

        if _blank(target.get("name")) or target.get("name") == "Untitled":
            if not _blank(clean.get("name")):
                target["name"] = clean["name"]
                changed = True

        for key in ("poster", "background", "logo", "description", "release_info", "imdb_rating"):
            if _blank(target.get(key)) and not _blank(clean.get(key)):
                target[key] = clean[key]
                changed = True

        if not target.get("genres") and clean.get("genres"):
            target["genres"] = clean["genres"]
            changed = True

        if clean.get("poster_shape") in ("SQUARE", "LANDSCAPE") and target.get("poster_shape") == "POSTER":
            target["poster_shape"] = clean["poster_shape"]
            changed = True

        # v1 incorrectly stamped Trakt's website as an addon URL. It is not a
        # Stremio/Nuvio addon endpoint, so repair only matching imported rows.
        if str(target.get("addon_base_url") or "").rstrip("/") == TRAKT_BASE:
            target["addon_base_url"] = clean.get("addon_base_url")
            changed = True

        if changed:
            updated += 1
        else:
            unchanged += 1

    existing.sort(key=lambda x: int(x.get("added_at") or 0), reverse=True)
    return existing, added, updated, unchanged, malformed


def make_preview(selected: list[ExportList], mapping: MappingResult, enrichment: EnrichmentResult | None = None) -> dict[str, Any]:
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "selected_lists": [
            {
                "name": x.name,
                "trakt_id": x.trakt_id,
                "filename": x.filename,
                "item_count": x.item_count,
                "type_counts": type_counts(x.entries),
            }
            for x in selected
        ],
        "unique_nuvio_candidates": len(mapping.items),
        "episode_entries_seen": mapping.episode_entries,
        "distinct_parent_series_from_episode_entries": mapping.episode_parent_series,
        "skipped": mapping.skipped,
        "source_rows": mapping.source_rows,
        "metadata_enrichment": None if enrichment is None else {
            "enriched": enrichment.enriched,
            "resolved_fallback_ids_to_imdb": enrichment.resolved_ids,
            "failed": enrichment.failed,
        },
        "items": mapping.items if enrichment is None else enrichment.items,
    }


def validate_base_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    raise ToolError("For credential safety, --base-url must use HTTPS (HTTP is allowed only for localhost).")


def validate_metadata_base_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    raise ToolError("--cinemeta-base must use HTTPS (HTTP is allowed only for localhost testing).")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Import Trakt personal lists into the Nuvio Library and, optionally, replace Nuvio watched/Continue-Watching "
            "state from the same Trakt export. Library and tracking data are handled independently."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("export_zip", type=Path, help="Trakt account export ZIP")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--all", action="store_true", help="Import all personal lists without asking")
    sel.add_argument("--list", action="append", default=[], metavar="NAME_OR_ID", help="Import a named list; repeat as needed")
    p.add_argument("--episodes", choices=("parent-series", "skip"), default="parent-series", help="How to handle episode/season entries inside personal lists")
    p.add_argument(
        "--replace-history",
        action="store_true",
        help="Also delete Nuvio watched status + Continue Watching for the selected profile and rebuild them from Trakt (Library is preserved)",
    )
    p.add_argument(
        "--history-only",
        action="store_true",
        help="Skip personal-list Library import and only replace watched status + Continue Watching from Trakt",
    )
    p.add_argument("--skip-progress", action="store_true", help="When replacing history, do not import Trakt Continue Watching/playback rows")
    p.add_argument("--dry-run", action="store_true", help="Build previews only; do not log in to or modify Nuvio")
    p.add_argument("--preview-file", type=Path, help="Where to write the combined local preview JSON")
    p.add_argument("--no-enrich", action="store_true", help="Skip Cinemeta artwork/details lookup for Library items")
    p.add_argument("--cinemeta-base", action="append", default=[], metavar="URL", help="Override Cinemeta base URL; repeat for fallbacks")
    p.add_argument("--metadata-workers", type=int, default=8, help="Concurrent Cinemeta metadata requests for Library enrichment")
    p.add_argument("--backup-dir", type=Path, default=Path.cwd(), help="Directory for Nuvio backups/reports")
    p.add_argument("--email", help="Nuvio account email (otherwise NUVIO_EMAIL or an interactive prompt)")
    p.add_argument("--profile", type=int, help="Nuvio profile ID/index (otherwise choose interactively)")
    p.add_argument("--yes", action="store_true", help="Skip typed confirmations (use only after reviewing the dry run)")
    p.add_argument("--base-url", default=os.environ.get("NUVIO_BASE_URL", DEFAULT_NUVIO_BASE), help="Nuvio API base URL")
    p.add_argument("--publishable-key", default=os.environ.get("NUVIO_PUBLISHABLE_KEY", DEFAULT_NUVIO_PUBLISHABLE_KEY), help="Nuvio public/publishable API key")
    p.add_argument("--access-token", default=os.environ.get("NUVIO_ACCESS_TOKEN"), help=argparse.SUPPRESS)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    validate_base_url(args.base_url)
    do_history = bool(args.replace_history or args.history_only)
    do_library = not args.history_only

    bases = args.cinemeta_base or list(CINEMETA_BASES)
    for base in bases:
        validate_metadata_base_url(base)

    selected: list[ExportList] = []
    mapping: MappingResult | None = None
    enrichment: EnrichmentResult | None = None
    import_items: list[dict[str, Any]] = []

    if do_library:
        all_lists = discover_export_lists(args.export_zip)
        selected = select_lists(all_lists, args.list, args.all)
        mapping = map_selected_lists(selected, args.episodes)

        print("\nSelected Trakt lists:")
        for item in selected:
            counts = ", ".join(f"{v} {k}" for k, v in sorted(type_counts(item.entries).items()))
            print(f"  • {item.name}: {item.item_count} entries ({counts})")
        print(f"\nMapped to {len(mapping.items)} unique Nuvio movie/series candidates.")
        if mapping.episode_entries:
            if args.episodes == "parent-series":
                print(
                    f"  {mapping.episode_entries} episode entries map to "
                    f"{mapping.episode_parent_series} distinct parent series."
                )
            else:
                print(f"  {mapping.episode_entries} episode entries are skipped.")
        if mapping.skipped:
            print(f"  {len(mapping.skipped)} unsupported/unmappable list entries will be skipped.")

        import_items = mapping.items
        if not args.no_enrich:
            enrichment = enrich_items(import_items, bases, workers=args.metadata_workers)
            import_items = enrichment.items
            print(
                f"Metadata enrichment: {enrichment.enriched}/{len(import_items)} title(s) received details; "
                f"{enrichment.resolved_ids} fallback ID(s) resolved to IMDb."
            )
            if enrichment.failed:
                print(f"  Warning: metadata was not found for {len(enrichment.failed)} title(s); those remain sparse.")
            if import_items and enrichment.enriched == 0:
                raise ToolError(
                    "Cinemeta returned no metadata for any Library title. No Nuvio write will be attempted. "
                    "Check your internet connection or use --no-enrich if sparse entries are intentional."
                )

    history_plan: HistoryPlan | None = None
    if do_history:
        print("\nReading Trakt watched history…")
        history_plan = build_history_plan(
            args.export_zip,
            bases,
            include_progress=not args.skip_progress,
        )
        print(
            f"History parser found {history_plan.movie_states} watched movies + "
            f"{history_plan.episode_states} watched episodes = {len(history_plan.watched_items)} unique Nuvio watched states."
        )
        print(
            f"Trakt contains {history_plan.movie_plays + history_plan.episode_plays} total play events; "
            "Nuvio stores one latest watched state per movie/episode, so rewatches are intentionally collapsed."
        )
        print(f"Continue Watching rows ready: {len(history_plan.progress_items)}")
        if history_plan.resolved_parent_ids:
            print(f"Resolved {history_plan.resolved_parent_ids} fallback TMDB/Trakt parent ID occurrence(s) to IMDb.")
        for warning in history_plan.warnings:
            print(f"  Warning: {warning}")
        if history_plan.unresolved_progress and not args.skip_progress:
            print(f"  BLOCKER: {len(history_plan.unresolved_progress)} Continue Watching row(s) are missing runtime metadata.")

    stamp = safe_timestamp()
    preview_path = args.preview_file or args.backup_dir / f"trakt-to-nuvio-preview-{stamp}.json"
    preview: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": {
            "library": do_library,
            "replace_history": do_history,
            "include_progress": do_history and not args.skip_progress,
        },
    }
    if do_library and mapping is not None:
        preview["library"] = make_preview(selected, mapping, enrichment)
    if history_plan is not None:
        preview["history"] = history_preview(history_plan)
    write_json(preview_path, preview)
    print(f"Preview: {preview_path}")

    if args.dry_run:
        print("Dry run complete. Nuvio was not contacted and nothing was changed.")
        return 0

    if history_plan is not None and history_plan.unresolved_progress and not args.skip_progress:
        raise ToolError(
            "History replacement is blocked because some Trakt playback rows cannot be rebuilt exactly. "
            "Run the dry run and inspect unresolved_progress, or use --skip-progress if you intentionally want to omit Continue Watching."
        )

    api = NuvioAPI(args.base_url, args.publishable_key, access_token=args.access_token)
    if not api.access_token:
        email = args.email or os.environ.get("NUVIO_EMAIL") or input("\nNuvio email: ").strip()
        if not email:
            raise ToolError("Nuvio email is required.")
        password = os.environ.get("NUVIO_PASSWORD") or getpass.getpass("Nuvio password (not saved): ")
        if not password:
            raise ToolError("Nuvio password is required.")
        print("Logging in to Nuvio…")
        api.login(email, password)

    print("Reading Nuvio profiles and current Library…")
    profiles = api.pull_profiles()
    profile_id = choose_profile(profiles, args.profile)
    library_current = api.pull_library(profile_id)

    combined_report: dict[str, Any] = {
        "completed_at_utc": None,
        "profile_id": profile_id,
        "preview_file": str(preview_path),
        "library": None,
        "history": None,
    }

    # Library migration is the same non-destructive merge from the earlier tool.
    if do_library:
        library_before_count = len(library_current)
        merged, added, updated, unchanged, malformed = merge_library(library_current, import_items)
        library_backup = args.backup_dir / f"nuvio-library-backup-profile-{profile_id}-{stamp}.json"
        write_json(
            library_backup,
            {
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "profile_id": profile_id,
                "item_count": len(library_current),
                "items": library_current,
            },
        )
        print(f"Library backup: {library_backup}")
        print("\nLibrary merge summary:")
        print(f"  Current Nuvio Library: {len(library_current)} items")
        print(f"  Trakt list candidates: {len(import_items)} unique titles")
        print(f"  Existing enriched:     {updated}")
        print(f"  Existing unchanged:    {unchanged}")
        print(f"  New titles to add:     {added}")
        print(f"  Final Library:         {len(merged)} items")
        if malformed:
            raise ToolError(
                f"Existing Nuvio Library contained {len(malformed)} unrecognized item(s). "
                "Library write blocked so nothing can be silently dropped."
            )

        library_missing: list[str] = []
        if added or updated:
            if not args.yes:
                print("\nLibrary sync is a FULL REPLACE, but this tool pushes the already-merged Library and has saved a backup.")
                confirm = input("Type IMPORT LIBRARY to continue: ").strip()
                if confirm != "IMPORT LIBRARY":
                    print("Cancelled before any Nuvio changes.")
                    return 0
            print("Pushing merged Library to Nuvio…")
            api.push_library(profile_id, merged)
            library_after = api.pull_library(profile_id)
            after_ids = {str(x.get("content_id")) for x in library_after if x.get("content_id")}
            expected_ids = {str(x.get("content_id")) for x in import_items if x.get("content_id")}
            library_missing = sorted(expected_ids - after_ids)
            if library_missing:
                raise ToolError(
                    f"Nuvio accepted the Library write, but {len(library_missing)} imported ID(s) were not found on verification. "
                    f"Keep the backup at {library_backup}."
                )
            library_current = library_after
            print(f"Verified all {len(expected_ids)} imported/repaired Library IDs.")
        else:
            print("Library already contains everything from the selected Trakt lists; no Library write needed.")

        combined_report["library"] = {
            "backup_file": str(library_backup),
            "before_count": library_before_count,
            "after_count": len(library_current),
            "candidate_count": len(import_items),
            "existing_enriched": updated,
            "existing_unchanged": unchanged,
            "added_requested": added,
            "missing_after_verification": library_missing,
        }

    if do_history and history_plan is not None:
        # IMPORTANT: this function never calls sync_push_library. It backs up the current Library
        # only so we can verify its membership was not touched by the destructive tracking reset.
        tracking_backup, tracking_report = replace_tracking_from_plan(
            api=api,
            profile_id=profile_id,
            plan=history_plan,
            library_before=library_current,
            backup_dir=args.backup_dir,
            stamp=stamp,
            yes=args.yes,
        )
        combined_report["history"] = {
            **tracking_report,
            "backup_file": str(tracking_backup),
            "trakt_movie_plays": history_plan.movie_plays,
            "trakt_episode_plays": history_plan.episode_plays,
            "trakt_unique_movie_states": history_plan.movie_states,
            "trakt_unique_episode_states": history_plan.episode_states,
            "unresolved_fallback_ids": history_plan.unresolved_fallback_ids,
        }

    combined_report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    report_path = args.backup_dir / f"trakt-to-nuvio-import-report-profile-{profile_id}-{stamp}.json"
    write_json(report_path, combined_report)
    print(f"\nDone. Report: {report_path}")
    if do_history:
        print("If the Nuvio app still does not show these watched states, make sure its watched/progress source is set to Nuvio Sync rather than a connected tracking provider.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)