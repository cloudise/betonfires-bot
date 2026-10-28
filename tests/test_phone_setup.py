from __future__ import annotations

from datetime import timedelta
import io
import os
from unittest import TestCase
from unittest.mock import patch

from esports_bot.__main__ import claim_owner, run, utcnow
from esports_bot.core import BotError
from esports_bot.store import Store

CODE = "test-only-random-pairing-code-0123456789"


def message(text: str, sender: int = 123, chat_type: str = "private") -> dict:
    return {"text": text, "chat": {"type": chat_type, "id": sender},
            "from": {"id": sender, "is_bot": False}}


class PhoneSetupTests(TestCase):
    def setUp(self):
        self.db = Store(":memory:")

    def tearDown(self):
        self.db.close()

    def test_only_exact_secret_claims_owner_and_secret_is_not_saved(self):
        self.assertIsNone(claim_owner(self.db, message("/start"), CODE))
        self.assertIsNone(claim_owner(self.db, message("/pair wrong"), CODE))
        self.assertEqual(123, claim_owner(self.db, message("/pair "+CODE), CODE))
        self.assertEqual(123, self.db.get("telegram_owner"))
        for row in self.db.db.execute("SELECT value FROM meta"):
            self.assertNotIn(CODE, row[0])

    def test_no_automatic_first_sender_binding_groups_bots_or_mismatched_sender(self):
        self.assertIsNone(claim_owner(self.db, message("/pair "+CODE, chat_type="group"), CODE))
        raw = message("/pair "+CODE)
        raw["from"]["is_bot"] = True
        self.assertIsNone(claim_owner(self.db, raw, CODE))
        raw = message("/pair "+CODE)
        raw["chat"]["id"] = 456
        self.assertIsNone(claim_owner(self.db, raw, CODE))
        self.assertIsNone(self.db.get("telegram_owner"))

    def test_existing_owner_cannot_be_rebound_even_with_original_code(self):
        self.assertEqual(123, claim_owner(self.db, message("/pair "+CODE), CODE))
        with self.assertRaises(BotError):
            claim_owner(self.db, message("/pair "+CODE, sender=456), CODE)
        self.assertEqual(123, self.db.get("telegram_owner"))

    def test_weak_code_is_rejected_and_unicode_guess_does_not_crash(self):
        with self.assertRaises(BotError):
            claim_owner(self.db, message("/pair short"), "short")
        self.assertIsNone(claim_owner(self.db, message("/pair секрет"), CODE))

    def test_pairing_is_rate_limited_per_sender_for_one_day(self):
        for _ in range(5):
            self.assertIsNone(claim_owner(self.db, message("/pair incorrect"), CODE))
        self.assertIsNone(claim_owner(self.db, message("/pair "+CODE), CODE))
        self.db.set("pair_fail:123", {"count": 5, "at": (utcnow()-timedelta(days=1, seconds=1)).isoformat()})
        self.assertEqual(123, claim_owner(self.db, message("/pair "+CODE), CODE))

    def test_cloud_can_pair_and_report_status_before_sources_are_configured(self):
        sent = []
        class FakeTelegram:
            def __init__(self, token):
                self.updates = 0
            def call(self, method, **params):
                if method == "getMe":
                    return {"username": "SyntheticTestBot"}
                if method == "getWebhookInfo":
                    return {"url": ""}
                if method == "getUpdates":
                    self.updates += 1
                    if self.updates == 1:
                        return [{"update_id": 1, "message": message("/pair "+CODE)}]
                    if self.updates == 2:
                        return [{"update_id": 2, "message": message("/status")}]
                    raise KeyboardInterrupt
                raise AssertionError(method)
            def send(self, owner, text):
                sent.append((owner, text))
        env = {"TELEGRAM_BOT_TOKEN": "123456:synthetic_test_token", "PAIRING_CODE": CODE}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch("esports_bot.__main__.Telegram", FakeTelegram), patch("sys.stdout", output):
            with self.assertRaises(KeyboardInterrupt):
                run(self.db)
        self.assertEqual(123, self.db.get("telegram_owner"))
        self.assertEqual(3, self.db.get("telegram_offset"))
        self.assertIn("Источник расписания пока не настроен", sent[-1][1])
        self.assertNotIn(CODE, output.getvalue())
        self.assertNotIn(env["TELEGRAM_BOT_TOKEN"], output.getvalue())
