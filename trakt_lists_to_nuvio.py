#!/usr/bin/env python3
"""Import personal Trakt list entries from a Trakt export ZIP into a Nuvio library.

This tool reads the current Nuvio library, enriches Trakt-export rows with
Cinemeta artwork/details, merges non-destructively, then writes the merged library.
Existing Nuvio values are preserved unless they are blank or were stamped by v1.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
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
        description="Import selected personal Trakt lists from a Trakt export ZIP into a Nuvio library.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("export_zip", type=Path, help="Trakt account export ZIP")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--all", action="store_true", help="Import all personal lists without asking")
    sel.add_argument("--list", action="append", default=[], metavar="NAME_OR_ID", help="Import a named list; repeat as needed")
    p.add_argument("--episodes", choices=("parent-series", "skip"), default="parent-series", help="How to handle episode/season list entries")
    p.add_argument("--dry-run", action="store_true", help="Build an enriched preview, but do not log in to or modify Nuvio")
    p.add_argument("--preview-file", type=Path, help="Where to write the local mapping preview JSON")
    p.add_argument("--no-enrich", action="store_true", help="Skip Cinemeta artwork/details lookup (fully offline mapping)")
    p.add_argument("--cinemeta-base", action="append", default=[], metavar="URL", help="Override Cinemeta base URL; repeat for fallbacks")
    p.add_argument("--metadata-workers", type=int, default=8, help="Concurrent Cinemeta metadata requests")
    p.add_argument("--backup-dir", type=Path, default=Path.cwd(), help="Directory for Nuvio pre-import backup/report files")
    p.add_argument("--email", help="Nuvio account email (otherwise NUVIO_EMAIL or an interactive prompt)")
    p.add_argument("--profile", type=int, help="Nuvio profile ID/index (otherwise choose interactively)")
    p.add_argument("--yes", action="store_true", help="Skip the final IMPORT confirmation")
    p.add_argument("--base-url", default=os.environ.get("NUVIO_BASE_URL", DEFAULT_NUVIO_BASE), help="Nuvio API base URL")
    p.add_argument("--publishable-key", default=os.environ.get("NUVIO_PUBLISHABLE_KEY", DEFAULT_NUVIO_PUBLISHABLE_KEY), help="Nuvio public/publishable API key")
    p.add_argument("--access-token", default=os.environ.get("NUVIO_ACCESS_TOKEN"), help=argparse.SUPPRESS)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    validate_base_url(args.base_url)
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
        print(f"  {len(mapping.skipped)} unsupported/unmappable entries will be skipped.")

    enrichment: EnrichmentResult | None = None
    import_items = mapping.items
    if not args.no_enrich:
        bases = args.cinemeta_base or list(CINEMETA_BASES)
        for base in bases:
            validate_metadata_base_url(base)
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
                "Cinemeta returned no metadata for any title. No Nuvio write will be attempted. "
                "Check your internet connection or use --no-enrich if sparse entries are intentional."
            )

    stamp = safe_timestamp()
    preview_path = args.preview_file or args.backup_dir / f"trakt-to-nuvio-preview-{stamp}.json"
    write_json(preview_path, make_preview(selected, mapping, enrichment))
    print(f"Preview: {preview_path}")

    if args.dry_run:
        print("Dry run complete. Nuvio was not contacted and nothing was changed.")
        return 0

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

    print("Reading Nuvio profiles and current library…")
    profiles = api.pull_profiles()
    profile_id = choose_profile(profiles, args.profile)
    existing_raw = api.pull_library(profile_id)
    merged, added, updated, unchanged, malformed = merge_library(existing_raw, import_items)

    backup_path = args.backup_dir / f"nuvio-library-backup-profile-{profile_id}-{stamp}.json"
    write_json(
        backup_path,
        {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile_id,
            "item_count": len(existing_raw),
            "items": existing_raw,
        },
    )
    print(f"Backup:  {backup_path}")

    print("\nMerge summary:")
    print(f"  Current Nuvio library: {len(existing_raw)} items")
    print(f"  Trakt candidates:      {len(import_items)} unique titles")
    print(f"  Existing enriched:     {updated}")
    print(f"  Existing unchanged:    {unchanged}")
    print(f"  New titles to add:     {added}")
    print(f"  Final library:         {len(merged)} items")
    if malformed:
        print(f"  Warning: {len(malformed)} malformed existing item(s) could not be preserved.")
        print("  Import is blocked so the full-replace operation cannot silently drop them.")
        raise ToolError("Existing Nuvio library contained unrecognized entries; no write was performed.")

    if added == 0 and updated == 0:
        print("Nothing to add or repair. Nuvio was not changed.")
        return 0

    if not args.yes:
        print(
            "\nNuvio's library sync is a FULL REPLACE. This tool will push the merged "
            "library shown above; the backup has already been saved."
        )
        confirm = input("Type IMPORT to continue: ").strip()
        if confirm != "IMPORT":
            print("Cancelled. Nuvio was not changed.")
            return 0

    print("Pushing merged library to Nuvio…")
    api.push_library(profile_id, merged)

    print("Verifying imported IDs…")
    after = api.pull_library(profile_id)
    after_ids = {
        str(x.get("content_id"))
        for x in after
        if isinstance(x, dict) and x.get("content_id") is not None
    }
    expected_ids = {str(x.get("content_id")) for x in import_items if x.get("content_id")}
    missing = sorted(expected_ids - after_ids)

    report_path = args.backup_dir / f"trakt-to-nuvio-import-report-profile-{profile_id}-{stamp}.json"
    write_json(
        report_path,
        {
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile_id,
            "selected_lists": [x.name for x in selected],
            "before_count": len(existing_raw),
            "candidate_count": len(import_items),
            "existing_enriched": updated,
            "existing_unchanged": unchanged,
            "added_requested": added,
            "metadata_enriched": enrichment.enriched if enrichment else 0,
            "metadata_failures": enrichment.failed if enrichment else [],
            "after_count": len(after),
            "missing_after_verification": missing,
            "backup_file": str(backup_path),
            "preview_file": str(preview_path),
        },
    )

    if missing:
        raise ToolError(
            f"Nuvio accepted the write, but {len(missing)} newly-added ID(s) were not found on verification. "
            f"See {report_path} and keep the backup."
        )

    print(f"Done. Verified all {len(expected_ids)} imported/repaired title ID(s) in Nuvio.")
    print(f"Report:  {report_path}")
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
