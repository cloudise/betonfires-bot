"""Synthetic provider responses only: these tests do not prove live coverage."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from esports_bot.__main__ import demo_store
from esports_bot.betboom import BetBoomFeed, SOURCE, extract, identify
from esports_bot.core import BotError
from esports_bot.service import Service

NOW = datetime(2026, 10, 28, 11, tzinfo=timezone.utc)


def fixture() -> dict:
    return {"id": 123456, "status": "notstarted", "category": {"name": "CS2"},
            "startTimestamp": int((NOW+timedelta(hours=6)).timestamp()*1000),
            "homeTeam": {"id": 555, "name": "DEMO Team 1"},
            "awayTeam": {"id": 556, "name": "DEMO Team 2"},
            "esports": {"bestOf": 3}, "hasBkOdds": {"betboom": True},
            "oddsBk": {"betboom": {"slug": "betboom", "isBettingActive": True,
                "isBettingActiveByStage": {"prematch": True, "live": False},
                "updatedAtByStage": {"prematch": int((NOW-timedelta(seconds=90)).timestamp()*1000)},
                "markets": {"result": {"stage": "prematch", "status": "open",
                    "hasArgument": False, "stakeKeys": ["w1", "w2"],
                    "stakes": {"w1": {"factor": 1.90}, "w2": {"factor": 2.02}}}}}}}


class BetBoomTests(TestCase):
    def setUp(self):
        self.db = demo_store(NOW)
        self.feed = BetBoomFeed("synthetic_key")

    def tearDown(self):
        self.db.close()

    def import_rows(self, rows):
        with patch.object(BetBoomFeed, "fetch", return_value=(rows, False)):
            return self.feed.sync(self.db, NOW)

    def test_import_retains_source_time_not_time_of_download(self):
        result = self.import_rows([fixture()])
        self.assertEqual(1, result["imported"])
        quote = self.db.quote("csgo:90001")
        self.assertEqual(SOURCE, quote["source"])
        self.assertEqual((NOW-timedelta(seconds=90)).isoformat(), quote["observed"])
        self.assertEqual(NOW.isoformat(), quote["provenance"]["retrieved_at"])
        self.assertEqual(123456, quote["provenance"]["provider_match_id"])
        self.assertIn("импорт API", Service(self.db).handle("/analyze csgo:90001", NOW))

    def test_reversed_home_away_reverses_prices(self):
        raw = fixture()
        raw["homeTeam"], raw["awayTeam"] = raw["awayTeam"], raw["homeTeam"]
        self.assertEqual(1, self.import_rows([raw])["imported"])
        quote = self.db.quote("csgo:90001")
        self.assertEqual("2.02", quote["a"])
        self.assertEqual("1.9", quote["b"])
        self.assertTrue(quote["provenance"]["swapped"])

    def test_unknown_discipline_wrong_format_and_academy_are_not_guessed(self):
        raw = fixture()
        for field, value in (("category", {"name": "League of Legends"}),
                             ("esports", {"bestOf": 2}),
                             ("homeTeam", {"name": "DEMO Team 1 Academy"}),
                             ("status", "inprogress")):
            changed = deepcopy(raw)
            changed[field] = value
            self.assertIsNone(identify(changed, self.db.matches(), NOW))
        self.assertIsNone(identify({}, self.db.matches(), NOW))

    def test_start_mismatch_and_two_pandascore_candidates_abstain(self):
        from dataclasses import replace
        raw = fixture()
        raw["startTimestamp"] += 6*60*1000
        self.assertIsNone(identify(raw, self.db.matches(), NOW))
        self.db.save_matches([replace(self.db.match("csgo:90001"), id=90002)])
        self.assertEqual(0, self.import_rows([fixture()])["imported"])

    def test_two_provider_events_cannot_overwrite_one_pandascore_match(self):
        first, second = fixture(), fixture()
        second["id"] = 123457
        report = self.import_rows([first, second])
        self.assertEqual(0, report["imported"])
        self.assertEqual(2, report["skipped"])

    def test_blocked_live_missing_or_three_way_market_never_imports(self):
        for change in ("inactive", "suspended", "outcome", "live", "draw", "no_book", "base_only", "no_time"):
            raw = fixture()
            board = raw["oddsBk"]["betboom"]
            market = board["markets"]["result"]
            if change == "inactive":
                board["isBettingActiveByStage"]["prematch"] = False
            elif change == "suspended":
                market["status"] = "suspended"
            elif change == "outcome":
                market["stakes"]["w1"]["suspended"] = True
            elif change == "live":
                market["stage"] = "live"
            elif change == "draw":
                market["stakeKeys"].append("x")
                market["stakes"]["x"] = {"factor": 3.4}
            elif change == "no_book":
                del raw["oddsBk"]["betboom"]
            elif change == "base_only":
                raw["oddsBase"] = [{"market": "result", "choices": []}]
                raw.pop("oddsBk")
            else:
                board.pop("updatedAtByStage")
            with self.subTest(change=change), self.assertRaises(BotError):
                extract(raw, NOW)

    def test_old_and_future_board_never_refreshed_by_new_http_response(self):
        for when in (NOW-timedelta(minutes=11), NOW+timedelta(seconds=1)):
            raw = fixture()
            raw["oddsBk"]["betboom"]["updatedAtByStage"]["prematch"] = int(when.timestamp()*1000)
            with self.assertRaises(BotError):
                extract(raw, NOW)

    def test_removed_market_or_failed_fetch_removes_import_but_keeps_ticket(self):
        self.import_rows([fixture()])
        Service(self.db).handle("/paper csgo:90001 A", NOW)
        snapshot = self.db.tickets()[0]["snapshot"]
        self.assertEqual(123456, json.loads(snapshot)["quote"]["provenance"]["provider_match_id"])
        with patch.object(BetBoomFeed, "fetch", side_effect=BotError("Нет доступа.")):
            report = self.feed.sync(self.db, NOW)
        self.assertIn("error", report)
        self.assertIsNone(self.db.quote("csgo:90001"))
        self.assertIsNotNone(self.db.quote("dota2:90001"))  # manual quote is independent
        self.assertEqual(snapshot, self.db.tickets()[0]["snapshot"])
        self.assertEqual("open", self.db.tickets()[0]["status"])

    def test_cursor_query_filters_bookmaker_without_exposing_key(self):
        responses = [{"matches": [fixture()], "nextCursor": "opaque+/cursor"},
                     {"matches": [], "nextCursor": None}]
        with patch("esports_bot.betboom.request_json", side_effect=responses) as request:
            rows, partial = self.feed.fetch(NOW)
            self.assertEqual(1, len(rows))
            self.assertFalse(partial)
            url = request.call_args.args[0]
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(["betboom"], query["bookmaker_ids"])
            self.assertEqual(["all"], query["odds_stage"])
            self.assertEqual(["opaque+/cursor"], query["cursor"])
            self.assertNotIn("synthetic_key", url)
            self.assertEqual({"Authorization": "synthetic_key"}, request.call_args.kwargs["headers"])

    def test_partial_page_cap_is_reported_and_schema_error_is_not_success(self):
        with patch("esports_bot.betboom.request_json", return_value={"matches": [], "nextCursor": "next"}):
            _, partial = self.feed.fetch(NOW, max_pages=1)
            self.assertTrue(partial)
        with patch("esports_bot.betboom.request_json", return_value={"matches": []}):
            with self.assertRaises(BotError):
                self.feed.fetch(NOW)
