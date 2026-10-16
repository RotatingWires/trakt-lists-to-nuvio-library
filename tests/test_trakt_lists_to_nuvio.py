import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "trakt_lists_to_nuvio.py"
spec = importlib.util.spec_from_file_location("trakt_lists_to_nuvio", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def write_zip(path: Path, files: dict[str, object]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, value in files.items():
            zf.writestr(name, json.dumps(value))


class HistoryParserTests(unittest.TestCase):
    def test_current_watched_movies_filename_and_history_fallback(self):
        movie = {
            "title": "Movie A",
            "year": 2024,
            "ids": {"imdb": "tt1000001", "tmdb": 10, "trakt": 20},
        }
        show = {
            "title": "Show A",
            "year": 2023,
            "ids": {"imdb": "tt2000002", "tmdb": 30, "trakt": 40},
        }
        files = {
            "user-stats.json": {
                "movies": {"watched": 1, "plays": 2},
                "episodes": {"watched": 1, "plays": 1},
            },
            "watched-movies.json": [
                {"movie": movie, "last_watched_at": "2026-01-01T00:00:00Z"},
            ],
            "watched-history-1.json": [
                {"type": "movie", "movie": movie, "watched_at": "2026-01-02T00:00:00Z"},
                {
                    "type": "episode",
                    "show": show,
                    "episode": {"season": 1, "number": 1, "title": "Pilot"},
                    "watched_at": "2026-01-03T00:00:00Z",
                },
            ],
            "watched-playback.json": [],
        }
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "trakt-export.zip"
            write_zip(archive, files)
            plan = mod.build_history_plan(archive, list(mod.CINEMETA_BASES), include_progress=False)

        self.assertEqual(plan.movie_states, 1)
        self.assertEqual(plan.episode_states, 1)
        self.assertEqual(len(plan.watched_items), 2)
        movie_state = next(item for item in plan.watched_items if item["content_type"] == "movie")
        self.assertEqual(movie_state["watched_at"], mod.parse_iso_ms("2026-01-02T00:00:00Z"))

    def test_numbered_watched_movies_filename_still_supported(self):
        movie = {
            "title": "Movie B",
            "year": 2022,
            "ids": {"imdb": "tt3000003", "tmdb": 50, "trakt": 60},
        }
        files = {
            "user-stats.json": {
                "movies": {"watched": 1, "plays": 1},
                "episodes": {"watched": 0, "plays": 0},
            },
            "watched-movies-1.json": [
                {"movie": movie, "last_watched_at": "2026-02-01T00:00:00Z"},
            ],
            "watched-history-1.json": [
                {"type": "movie", "movie": movie, "watched_at": "2026-02-01T00:00:00Z"},
            ],
            "watched-playback.json": [],
        }
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "trakt-export.zip"
            write_zip(archive, files)
            plan = mod.build_history_plan(archive, list(mod.CINEMETA_BASES), include_progress=False)

        self.assertEqual(plan.movie_states, 1)
        self.assertEqual(plan.episode_states, 0)

    def test_stats_mismatch_blocks_destructive_history_plan(self):
        movie = {
            "title": "Movie C",
            "year": 2021,
            "ids": {"imdb": "tt4000004", "tmdb": 70, "trakt": 80},
        }
        files = {
            "user-stats.json": {
                "movies": {"watched": 2, "plays": 1},
                "episodes": {"watched": 0, "plays": 0},
            },
            "watched-movies.json": [
                {"movie": movie, "last_watched_at": "2026-03-01T00:00:00Z"},
            ],
            "watched-history-1.json": [
                {"type": "movie", "movie": movie, "watched_at": "2026-03-01T00:00:00Z"},
            ],
            "watched-playback.json": [],
        }
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "trakt-export.zip"
            write_zip(archive, files)
            with self.assertRaises(mod.ToolError):
                mod.build_history_plan(archive, list(mod.CINEMETA_BASES), include_progress=False)


class TrackingReplacementTests(unittest.TestCase):
    def test_history_replacement_never_writes_library(self):
        library = [{"content_id": "tt9999999", "content_type": "movie", "name": "Keep Me"}]
        watched_old = [{
            "content_id": "tt1111111", "content_type": "movie", "title": "Old",
            "season": None, "episode": None, "watched_at": 1,
        }]
        watched_new = [{
            "content_id": "tt2222222", "content_type": "movie", "title": "New",
            "season": None, "episode": None, "watched_at": 2,
        }]

        class FakeAPI:
            def __init__(self):
                self.watched = list(watched_old)
                self.progress = []
                self.library = list(library)

            def pull_watched(self, profile_id):
                return list(self.watched)

            def pull_progress(self, profile_id, limit=10_000):
                return list(self.progress)

            def pull_library(self, profile_id):
                return list(self.library)

            def delete_watched(self, profile_id, keys, origin):
                self.watched = []

            def delete_progress(self, profile_id, keys, origin):
                self.progress = []

            def push_watched(self, profile_id, items, origin):
                self.watched.extend(items)

            def push_progress(self, profile_id, items, origin):
                self.progress.extend(items)

            def push_library(self, *args, **kwargs):
                raise AssertionError("history replacement must never write the Library")

        plan = mod.HistoryPlan(
            watched_items=watched_new,
            progress_items=[],
            movie_states=1,
            episode_states=0,
            movie_plays=1,
            episode_plays=0,
            source_history_rows=1,
            stats_expected_movies=1,
            stats_expected_episodes=0,
            resolved_parent_ids=0,
            unresolved_fallback_ids=[],
            unresolved_progress=[],
            warnings=[],
        )
        api = FakeAPI()
        original_sleep = mod.time.sleep
        mod.time.sleep = lambda _: None
        try:
            with tempfile.TemporaryDirectory() as td:
                _, report = mod.replace_tracking_from_plan(
                    api=api,
                    profile_id=1,
                    plan=plan,
                    library_before=library,
                    backup_dir=Path(td),
                    stamp="test",
                    yes=True,
                )
        finally:
            mod.time.sleep = original_sleep

        self.assertEqual(
            {mod.watched_item_key(x) for x in api.watched},
            {mod.watched_item_key(x) for x in watched_new},
        )
        self.assertFalse(report["library_key_set_changed"])
        self.assertEqual(api.library, library)

    def test_progress_reseed_survives_delete_tombstone_and_opaque_server_key(self):
        library = [{"content_id": "tt9999999", "content_type": "movie", "name": "Keep Me"}]
        progress_new = [{
            "content_id": "tt5555555",
            "content_type": "movie",
            "video_id": "tt5555555",
            "season": None,
            "episode": None,
            "position": 500,
            "duration": 1000,
            "last_watched": 123,
            "progress_key": "tt5555555",
        }]

        class FakeAPI:
            def __init__(self):
                self.library = list(library)
                self.watched = []
                self.progress = [{
                    "content_id": "tt4444444",
                    "content_type": "movie",
                    "video_id": "tt4444444",
                    "season": None,
                    "episode": None,
                    "position": 100,
                    "duration": 1000,
                    "last_watched": 50,
                    "progress_key": "tt4444444",
                }]
                self.progress_delete_tombstone_ms = 0

            def pull_watched(self, profile_id):
                return list(self.watched)

            def pull_progress(self, profile_id, limit=10_000):
                return list(self.progress)

            def pull_library(self, profile_id):
                return list(self.library)

            def delete_watched(self, profile_id, keys, origin):
                self.watched = []

            def delete_progress(self, profile_id, keys, origin):
                self.progress = []
                self.progress_delete_tombstone_ms = int(mod.time.time() * 1000)

            def push_watched(self, profile_id, items, origin):
                self.watched.extend(items)

            def push_progress(self, profile_id, items, origin):
                for raw in items:
                    # Mirrors hosted Nuvio's stale-resurrection protection:
                    # an older historical timestamp is ignored after a delete event.
                    if int(raw["last_watched"]) <= self.progress_delete_tombstone_ms:
                        continue
                    stored = dict(raw)
                    # Also simulate a legacy/opaque server progress key. Verification
                    # should reconcile by logical content identity, like Nuvio does.
                    stored["progress_key"] = "opaque/server/key"
                    self.progress.append(stored)

            def push_library(self, *args, **kwargs):
                raise AssertionError("history replacement must never write the Library")

        plan = mod.HistoryPlan(
            watched_items=[],
            progress_items=progress_new,
            movie_states=0,
            episode_states=0,
            movie_plays=0,
            episode_plays=0,
            source_history_rows=0,
            stats_expected_movies=0,
            stats_expected_episodes=0,
            resolved_parent_ids=0,
            unresolved_fallback_ids=[],
            unresolved_progress=[],
            warnings=[],
        )
        api = FakeAPI()
        original_sleep = mod.time.sleep
        mod.time.sleep = lambda _: None
        try:
            with tempfile.TemporaryDirectory() as td:
                _, report = mod.replace_tracking_from_plan(
                    api=api,
                    profile_id=1,
                    plan=plan,
                    library_before=library,
                    backup_dir=Path(td),
                    stamp="test",
                    yes=True,
                )
        finally:
            mod.time.sleep = original_sleep

        self.assertEqual(len(api.progress), 1)
        self.assertGreater(api.progress[0]["last_watched"], 123)
        self.assertEqual(report["missing_progress_keys"], [])
        self.assertEqual(len(report["progress_key_remaps"]), 1)
        self.assertEqual(len(report["progress_timestamp_reseed"]), 1)
        self.assertFalse(report["library_key_set_changed"])
        self.assertEqual(api.library, library)



if __name__ == "__main__":
    unittest.main()
