"""SQLite cache and an integer-cent paper ledger. No bookmaker credentials."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .core import BotError, Match, decimal, payout, timestamp


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            os.chmod(target, 0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS matches (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS odds (
                key TEXT PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL,
                observed TEXT NOT NULL, source TEXT NOT NULL,
                a_id INTEGER NOT NULL, b_id INTEGER NOT NULL, start TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY, match_key TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL, stake INTEGER NOT NULL, odds TEXT NOT NULL,
                probability REAL NOT NULL, model TEXT NOT NULL, created TEXT NOT NULL,
                snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                returned INTEGER NOT NULL DEFAULT 0, settled TEXT);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(odds)")}
        if "provenance" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE odds ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}'")

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self._set(key, value)

    def _set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    def save_matches(self, matches: list[Match]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO matches VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                [(m.key, json.dumps(m.data(), ensure_ascii=False)) for m in matches])

    def matches(self) -> list[Match]:
        return [Match(**json.loads(r[0])) for r in self.db.execute("SELECT payload FROM matches")]

    def match(self, key: str) -> Match:
        row = self.db.execute("SELECT payload FROM matches WHERE key=?", (key,)).fetchone()
        if not row:
            raise BotError("Матч не найден. Сначала /matches; ID имеет вид csgo:123 или dota2:123.")
        return Match(**json.loads(row[0]))

    def quote(self, key: str):
        row = self.db.execute("SELECT * FROM odds WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["provenance"] = json.loads(result["provenance"])
        return result

    def save_quote(self, match: Match, a: str, b: str, now: datetime, source: str,
                   *, observed_at: datetime | None = None, provenance: dict | None = None) -> None:
        if not match.eligible:
            raise BotError("Нужен BO1/BO3/BO5 с двумя исходами: победитель всей серии.")
        if not timestamp(match.start) or timestamp(match.start) <= now or match.status != "not_started":
            raise BotError("Коэффициенты принимаются только до начала матча.")
        a, b = str(decimal(a, "1.01", "1000")), str(decimal(b, "1.01", "1000"))
        if not source.strip():
            raise BotError("Укажи название источника коэффициентов.")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO odds "
                            "(key,a,b,observed,source,a_id,b_id,start,provenance) VALUES (?,?,?,?,?,?,?,?,?)",
                            (match.key, a, b, (observed_at or now).isoformat(), source[:100],
                             match.a_id, match.b_id, match.start,
                             json.dumps(provenance or {"mode": "manual"}, ensure_ascii=False)))

    def remove_source_quotes(self, source: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM odds WHERE source=?", (source,))

    def initialize_bank(self, amount: int) -> None:
        if self.get("paper_initial") is not None:
            raise BotError("Виртуальный банк уже создан. История сохраняется; сброса для сокрытия потерь нет.")
        if amount <= 0:
            raise BotError("Виртуальный банк должен быть больше нуля.")
        with self.db:
            self._set("paper_initial", amount)
            self._set("paper_cash", amount)

    def tickets(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM tickets" + (" WHERE status=?" if status else "") + " ORDER BY id"
        return [dict(r) for r in self.db.execute(sql, (status,) if status else ())]

    def bank(self) -> dict:
        initial = self.get("paper_initial", 0)
        cash = self.get("paper_cash", 0)
        rows = self.tickets()
        opened = sum(t["stake"] for t in rows if t["status"] == "open")
        equity, peak, maximum_drawdown = initial, initial, 0.0
        for ticket in sorted((t for t in rows if t["status"] != "open"),
                             key=lambda t: (t["settled"], t["id"])):
            equity += ticket["returned"] - ticket["stake"]
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, (peak-equity) / peak if peak else 0)
        wagered = sum(t["stake"] for t in rows if t["status"] in ("won", "lost"))
        return {"initial": initial, "cash": cash, "open": opened,
                "equity": cash+opened, "pnl": cash+opened-initial,
                "roi": (cash+opened-initial) / wagered if wagered else None,
                "max_drawdown": maximum_drawdown, "tickets": len(rows),
                "settled": sum(t["status"] != "open" for t in rows)}

    def paper_stake(self, now: datetime) -> int:
        """A fixed experiment: 0.25% per ticket, at most 1% in rolling 24h/open.

        These are study parameters, not a claim of safe real-money stakes.
        Exposure counts stakes, not a made-up worst-case probability.
        """
        bank = self.bank()
        if not bank["initial"]:
            return 0
        since = now - timedelta(hours=24)
        recent = sum(t["stake"] for t in self.tickets() if timestamp(t["created"]) > since)
        limit = bank["equity"] // 100
        fixed = bank["equity"] // 400
        if fixed < 1 or recent+fixed > limit or bank["open"]+fixed > limit:
            return 0
        return min(fixed, bank["cash"])

    def place(self, match: Match, side: str, p: float, model: str,
              now: datetime, snapshot: dict) -> int:
        if side not in ("A", "B"):
            raise BotError("Сторона — A или B.")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM tickets WHERE match_key=?", (match.key,)).fetchone():
                raise BotError("Этот матч уже есть в журнале. Повтор и противоположный исход запрещены.")
            stake = self.paper_stake(now)
            if not stake:
                raise BotError("Нет виртуального банка или исчерпан лимит виртуального эксперимента.")
            quote = snapshot["quote"]
            odds = quote["a" if side == "A" else "b"]
            self.db.execute("""INSERT INTO tickets
                (match_key,side,stake,odds,probability,model,created,snapshot)
                VALUES (?,?,?,?,?,?,?,?)""",
                (match.key, side, stake, odds, p if side == "A" else 1-p, model,
                 now.isoformat(), json.dumps(snapshot, ensure_ascii=False)))
            self._set("paper_cash", self.get("paper_cash") - stake)
            self.db.commit()
            return stake
        except Exception:
            self.db.rollback()
            raise

    def settle(self, now: datetime) -> int:
        """Paper rule: cancel/forfeit/draw/unsupported => refund, normal winner => pay.

        Never implies the real bookmaker follows this rule. Missing/delayed results
        remain open. Corrected results after settlement require a manual audit.
        """
        count = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for ticket in self.tickets("open"):
                match = self.match(ticket["match_key"])
                status, returned = None, 0
                if match.status == "canceled" or (match.status == "finished" and not match.eligible):
                    status, returned = "void", ticket["stake"]
                elif match.finished_before(now):
                    # Team identity comes from the pre-match snapshot, not response ordering.
                    old = Match(**json.loads(ticket["snapshot"])["match"])
                    chosen = old.a_id if ticket["side"] == "A" else old.b_id
                    if {old.a_id, old.b_id} != {match.a_id, match.b_id}:
                        status, returned = "void", ticket["stake"]
                    else:
                        status = "won" if match.winner_id == chosen else "lost"
                        returned = payout(ticket["stake"], ticket["odds"]) if status == "won" else 0
                if status:
                    self.db.execute("UPDATE tickets SET status=?, returned=?, settled=? WHERE id=? AND status='open'",
                                    (status, returned, now.isoformat(), ticket["id"]))
                    self._set("paper_cash", self.get("paper_cash") + returned)
                    count += 1
            self.db.commit()
            return count
        except Exception:
            self.db.rollback()
            raise
