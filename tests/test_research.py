from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from esports_bot.__main__ import authorized, demo_store, digest_due
from esports_bot.core import (MODEL_VERSION, BotError, Elo, Match, backtest,
                               cents, market, normalize, payout, train)
from esports_bot.providers import PandaScore
from esports_bot.service import Service
from esports_bot.store import Store

NOW = datetime(2026, 10, 28, 11, tzinfo=timezone.utc)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.db = demo_store(NOW)
        self.service = Service(self.db)
        self.match = self.db.match("csgo:90001")

    def tearDown(self):
        self.db.close()

    def test_no_real_money_recommendation_and_paper_execution(self):
        row = self.service.analysis(self.match, NOW)
        self.assertEqual([], row["blockers"])
        output = self.service.handle("/paper csgo:90001 A", NOW)
        self.assertIn("Только виртуальная", output)
        ticket = self.db.tickets()[0]
        self.assertEqual(2500, ticket["stake"])
        self.assertEqual(997500, self.db.bank()["cash"])
        self.assertIn("Реальная ставка: 0%", self.service.render_analysis(row, NOW))

    def test_duplicate_trade_and_ledger_reset_blocked(self):
        self.service.handle("/paper csgo:90001 A", NOW)
        with self.assertRaises(BotError):
            self.service.handle("/paper csgo:90001 A", NOW)
        with self.assertRaises(BotError):
            self.db.initialize_bank(1000000)
        self.assertEqual(1, len(self.db.tickets()))

    def test_settlement_is_idempotent_and_uses_original_team_identity(self):
        self.service.handle("/paper csgo:90001 A", NOW)
        # Provider swaps the display order; winner team ID remains unchanged.
        ended = replace(self.match, a_id=2, a="DEMO Team 2", b_id=1, b="DEMO Team 1",
                        status="finished", end=(NOW+timedelta(hours=7)).isoformat(), winner_id=1)
        self.db.save_matches([ended])
        self.assertEqual(1, self.db.settle(NOW+timedelta(hours=8)))
        self.assertEqual(0, self.db.settle(NOW+timedelta(hours=8)))
        self.assertEqual(1002250, self.db.bank()["cash"])
        self.assertEqual("won", self.db.tickets()[0]["status"])

    def test_cancellation_returns_stake_in_paper_policy(self):
        self.service.handle("/paper csgo:90001 A", NOW)
        self.db.save_matches([replace(self.match, status="canceled", special=True)])
        self.db.settle(NOW)
        self.assertEqual(1000000, self.db.bank()["cash"])
        self.assertEqual("void", self.db.tickets()[0]["status"])

    def test_money_nan_infinity_negative_and_rounding(self):
        for invalid in ("NaN", "Infinity", "-1", "100.001", "1e1000"):
            with self.assertRaises(BotError):
                cents(invalid)
        self.assertEqual(10050, cents("100,50"))
        self.assertEqual(303, payout(150, "2.02"))

    def test_margin_and_ev_are_different_calculations(self):
        row = market("1.9", "1.9", .6)
        self.assertAlmostEqual(.5, row["market_a"])
        self.assertAlmostEqual(1/1.9, row["break_even_a"])
        self.assertAlmostEqual(.14, row["ev_a"])

    def test_stale_and_future_odds_block_paper(self):
        for time in (NOW-timedelta(minutes=11), NOW+timedelta(seconds=1)):
            self.db.save_quote(self.match, "1.9", "1.9", time, "DEMO")
            self.assertTrue(self.service.analysis(self.match, NOW)["blockers"])
            with self.assertRaises(BotError):
                self.service.handle("/paper csgo:90001 A", NOW)

    def test_stale_source_and_failed_last_sync_block(self):
        self.db.set("sync:csgo", (NOW-timedelta(minutes=31)).isoformat())
        self.assertTrue(self.service.analysis(self.match, NOW)["blockers"])
        self.db.set("sync:csgo", NOW.isoformat())
        self.db.set("sync_error:csgo", "temporary error")
        self.assertTrue(self.service.analysis(self.match, NOW)["blockers"])

    def test_changed_sides_or_time_invalidates_quote(self):
        changed = replace(self.match, a_id=2, b_id=1)
        self.assertTrue(any("стороны" in b for b in self.service.analysis(changed, NOW)["blockers"]))
        changed = replace(self.match, start=(NOW+timedelta(days=1)).isoformat())
        self.assertTrue(any("время" in b for b in self.service.analysis(changed, NOW)["blockers"]))

    def test_new_team_and_bo2_abstain(self):
        for changed in (replace(self.match, a_id=123456), replace(self.match, best_of=2)):
            row = self.service.analysis(changed, NOW)
            self.assertIsNone(row["p"])
            self.assertIsNone(row["side"])
            self.assertTrue(row["blockers"])

    def test_started_match_blocks_quote_and_paper(self):
        started = replace(self.match, status="running")
        self.db.save_matches([started])
        with self.assertRaises(BotError):
            self.db.save_quote(started, "1.9", "1.9", NOW, "DEMO")
        with self.assertRaises(BotError):
            self.service.handle("/paper csgo:90001 A", NOW)

    def test_exposure_cap_stops_fifth_ticket_and_never_uses_reference_balance(self):
        self.service.handle("/balance 900000", NOW)
        row = self.service.analysis(self.match, NOW)
        for i in range(4):
            m = replace(self.match, id=100000+i)
            self.db.save_matches([m])
            self.db.place(m, "A", row["p"], MODEL_VERSION, NOW,
                          {"match": m.data(), "quote": row["quote"]})
        self.assertEqual(10000, self.db.bank()["open"])
        self.assertEqual(0, self.db.paper_stake(NOW))
        self.assertEqual(90000000, self.db.get("reference_balance"))

    def test_different_sports_and_formats_have_separate_ratings(self):
        history = self.db.matches()
        model = train([m for m in history if m.sport == "csgo"], NOW)
        self.assertEqual(.5, model.probability(replace(self.match, sport="dota2")))
        self.assertEqual(.5, model.probability(replace(self.match, best_of=1)))

    def test_training_excludes_future_and_in_progress_result(self):
        before = train(self.db.matches(), NOW).probability(self.match)
        future = replace(self.match, id=99123, status="finished", winner_id=2,
                         start=(NOW-timedelta(hours=1)).isoformat(),
                         end=(NOW+timedelta(hours=1)).isoformat())
        after = train(self.db.matches()+[future], NOW).probability(self.match)
        self.assertEqual(before, after)

    def test_walk_forward_does_not_see_overlapping_match_winner(self):
        history = [m for m in self.db.matches() if m.status == "finished"]
        # Target starts while another match is still in progress. Its final winner
        # must not affect the target's prediction, even with full history available.
        overlapping = replace(self.match, id=99123, status="finished", winner_id=1,
                              start=(NOW-timedelta(hours=3)).isoformat(),
                              end=(NOW-timedelta(minutes=15)).isoformat())
        target = replace(self.match, id=99124, status="finished", winner_id=1,
                         start=(NOW-timedelta(hours=2)).isoformat(),
                         end=(NOW-timedelta(hours=1)).isoformat())
        original = Elo.probability
        def predict(rows):
            captured = []
            def traced(model, match):
                result = original(model, match)
                if match.id == target.id:
                    captured.append(result)
                return result
            with patch.object(Elo, "probability", traced):
                backtest(rows, NOW)
            return captured
        self.assertEqual(predict(history+[overlapping, target]),
                         predict(history+[replace(overlapping, winner_id=2), target]))

    def test_express_rejects_same_match_shared_team_and_tournament(self):
        with self.assertRaises(BotError):
            self.service.handle("/express csgo:90001:A csgo:90001:B", NOW)
        self.assertTrue(Service.related(self.match, replace(self.match, id=90002)))
        self.assertTrue(Service.related(self.match, replace(self.match, id=90003, a_id=5, b_id=6)))
        self.assertFalse(Service.related(self.match, replace(self.match, sport="dota2")))
        text = self.service.handle("/express csgo:90001:A dota2:90001:A", NOW)
        self.assertIn("Независимость не доказана", text)

    def test_owner_acl_blocks_other_users_groups_and_bots(self):
        good = {"chat": {"type": "private", "id": 123}, "from": {"id": 123}}
        self.assertTrue(authorized(good, 123))
        self.assertFalse(authorized(good, 456))
        self.assertFalse(authorized({**good, "chat": {"type": "group", "id": 123}}, 123))
        self.assertFalse(authorized({**good, "from": {"id": 123, "is_bot": True}}, 123))

    def test_digest_runs_once_per_moscow_date(self):
        self.service.handle("/subscribe 15:00", NOW)  # NOW is 14:00 Moscow
        self.assertFalse(digest_due(self.db, NOW))
        later = NOW+timedelta(hours=2)
        self.assertTrue(digest_due(self.db, later))
        self.db.set("digest_last_date", "2026-10-28")
        self.assertFalse(digest_due(self.db, later))
        self.assertTrue(digest_due(self.db, later+timedelta(days=1)))

    def test_schema_adapter_fails_closed_for_unknown_format(self):
        raw = {"id": 123, "opponents": [
            {"type": "Team", "opponent": {"id": 1, "name": "A"}},
            {"type": "Team", "opponent": {"id": 2, "name": "B"}}]}
        self.assertFalse(normalize(raw, "csgo").eligible)
        self.assertIsNone(normalize({"id": 123}, "csgo"))
        raw.update({"match_type": "best_of", "number_of_games": 3, "forfeit": True})
        self.assertFalse(normalize(raw, "csgo").eligible)

    def test_api_contract_uses_only_game_routes_and_bearer_auth(self):
        with patch("esports_bot.providers.request_json", return_value=[]) as request:
            provider = PandaScore("test_token_DO_NOT_PRINT")
            provider.page("csgo", "upcoming", 2, sort="scheduled_at")
            url = request.call_args.args[0]
            self.assertIn("/csgo/matches/upcoming", url)
            self.assertIn("page=2", url)
            self.assertNotIn("test_token", url)
            self.assertEqual("Bearer test_token_DO_NOT_PRINT", request.call_args.kwargs["headers"]["Authorization"])
        with self.assertRaises(BotError):
            provider.page("football", "upcoming")

    def test_sync_failure_does_not_mark_data_fresh(self):
        old = (NOW-timedelta(days=1)).isoformat()
        self.db.set("sync:csgo", old)
        with patch.object(PandaScore, "page", side_effect=BotError("Источник недоступен.")):
            report = PandaScore("test").sync(self.db, 1)
        self.assertIn("error", report["csgo"])
        self.assertEqual(old, self.db.get("sync:csgo"))


if __name__ == "__main__":
    unittest.main()
