"""Optional BetBoom odds via the documented third-party API-SPORT feed.

No bookmaker login, scraping, auto-wagering or balance access.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import unicodedata
import urllib.parse
from zoneinfo import ZoneInfo

from .core import BotError, Match, decimal, timestamp
from .providers import request_json
from .store import Store

SOURCE = "BetBoom via API-SPORT"
MOSCOW = ZoneInfo("Europe/Moscow")
# This is an explicit whitelist, not a fuzzy discipline guess.
CATEGORIES = {"cs2": "csgo", "counter-strike 2": "csgo", "counter-strike": "csgo",
              "dota 2": "dota2"}


def name(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def from_ms(value) -> datetime:
    if type(value) not in (int, float):
        raise BotError("API-SPORT: нет корректного времени источника.")
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise BotError("API-SPORT: неверное время источника.") from None


def identify(raw: dict, matches: list[Match], now: datetime) -> tuple[Match, bool] | None:
    """Only unique exact normalized names, discipline, BO and start ±5 min.

    Returns whether the provider's home/away order is swapped relative to A/B.
    Academy/suffixes/translations are not silently removed or guessed.
    """
    try:
        category = name((raw.get("category") or {}).get("name"))
        sport = CATEGORIES.get(category)
        home, away = name(raw["homeTeam"]["name"]), name(raw["awayTeam"]["name"])
        best_of = raw["esports"]["bestOf"]
        start = from_ms(raw.get("startTimestamp"))
    except (KeyError, TypeError, AttributeError, BotError):
        return None
    if (not sport or not home or not away or home == away or type(best_of) is not int
            or raw.get("status") != "notstarted" or start <= now):
        return None
    candidates = []
    for m in matches:
        if (not m.eligible or m.sport != sport or m.best_of != best_of
                or m.status != "not_started" or not timestamp(m.start)
                or timestamp(m.start) <= now
                or abs((timestamp(m.start)-start).total_seconds()) > 300):
            continue
        if (name(m.a), name(m.b)) == (home, away):
            candidates.append((m, False))
        elif (name(m.b), name(m.a)) == (home, away):
            candidates.append((m, True))
    return candidates[0] if len(candidates) == 1 else None


def extract(raw: dict, now: datetime) -> tuple[str, str, datetime]:
    """Never substitute another book/base odds, map market or live board."""
    try:
        if (raw.get("hasBkOdds") or {}).get("betboom") is not True:
            raise BotError("Нет линии BetBoom.")
        board = raw["oddsBk"]["betboom"]
        market = board["markets"]["result"]
        if (board.get("slug") != "betboom"
                or (board.get("isBettingActiveByStage") or {}).get("prematch") is not True
                or market.get("stage") != "prematch" or market.get("status") != "open"
                or market.get("hasArgument") is not False
                or set(market.get("stakeKeys", [])) != {"w1", "w2"}
                or set(market["stakes"]) != {"w1", "w2"}):
            raise BotError("Нет активного двухисходного прематч-рынка победителя серии.")
        observed = from_ms(board["updatedAtByStage"]["prematch"])
        if not 0 <= (now-observed).total_seconds() <= 600:
            raise BotError("Доска BetBoom старше 10 минут или время некорректно.")
        w1, w2 = market["stakes"]["w1"], market["stakes"]["w2"]
        if w1.get("suspended") is True or w2.get("suspended") is True:
            raise BotError("Исход приостановлен.")
        return (str(decimal(str(w1["factor"]), "1.01", "1000")),
                str(decimal(str(w2["factor"]), "1.01", "1000")), observed)
    except (KeyError, TypeError, AttributeError):
        raise BotError("API-SPORT: неполная структура линии BetBoom.") from None


class BetBoomFeed:
    def __init__(self, key: str) -> None:
        if not key:
            raise BotError("Нужен отдельный API_SPORT_KEY, а не пароль BetBoom.")
        self.key = key

    def fetch(self, now: datetime, max_pages: int = 3) -> tuple[list[dict], bool]:
        first = now.astimezone(MOSCOW).date()
        cursor, rows = "", {}
        for _ in range(max_pages):
            query = urllib.parse.urlencode({
                "date_from": first.isoformat(), "date_to": (first+timedelta(days=2)).isoformat(),
                "tz": "Europe/Moscow", "status": "notstarted", "sort": "asc",
                "with_bk_odds": "true", "bookmaker_ids": "betboom", "odds_stage": "all",
                "page_size": 500, "cursor": cursor,
                "fields": "id,status,startTimestamp,category,tournament,homeTeam,awayTeam,esports,hasBkOdds",
            })
            payload = request_json("https://api.api-sport.ru/v2/esports/matches?"+query,
                                   headers={"Authorization": self.key})
            if (not isinstance(payload, dict) or not isinstance(payload.get("matches"), list)
                    or "nextCursor" not in payload):
                raise BotError("API-SPORT: некорректный ответ списка матчей.")
            for raw in payload["matches"]:
                if not isinstance(raw, dict) or type(raw.get("id")) is not int:
                    raise BotError("API-SPORT: запись без корректного ID.")
                if raw["id"] in rows and rows[raw["id"]] != raw:
                    raise BotError("API-SPORT: противоречивые дубли матчей.")
                rows[raw["id"]] = raw
            next_cursor = payload["nextCursor"]
            if next_cursor is None:
                return list(rows.values()), False
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise BotError("API-SPORT: некорректный курсор.")
            cursor = next_cursor
        return list(rows.values()), True

    def sync(self, db: Store, now: datetime) -> dict:
        # A removed/blocked market must disappear, not stay fresh until the old TTL.
        # Paper ticket snapshots retain their original prices independently.
        db.remove_source_quotes(SOURCE)
        try:
            rows, partial = self.fetch(now)
            matches = db.matches()
            mappings = defaultdict(list)
            unmatched = 0
            for raw in rows:
                identified = identify(raw, matches, now)
                if identified:
                    m, swapped = identified
                    mappings[m.key].append((raw, m, swapped))
                else:
                    unmatched += 1
            imported, skipped = 0, unmatched
            for options in mappings.values():
                # Also require uniqueness in the reverse direction.
                if len(options) != 1:
                    skipped += len(options)
                    continue
                raw, m, swapped = options[0]
                try:
                    home, away, observed = extract(raw, now)
                    a, b = (away, home) if swapped else (home, away)
                    db.save_quote(m, a, b, now, SOURCE, observed_at=observed, provenance={
                        "mode": "api", "bookmaker": "betboom", "provider": "api-sport.ru",
                        "provider_match_id": raw["id"], "stage": "prematch", "market": "result",
                        "retrieved_at": now.isoformat(), "source_updated_at": observed.isoformat(),
                        "provider_home": raw["homeTeam"]["name"],
                        "provider_away": raw["awayTeam"]["name"], "swapped": swapped,
                    })
                    imported += 1
                except BotError:
                    skipped += 1
            report = {"source": SOURCE, "received": len(rows), "imported": imported,
                      "skipped": skipped, "partial": partial, "updated": now.isoformat()}
            db.set("betboom_status", report)
            return report
        except BotError as error:
            report = {"source": SOURCE, "error": str(error), "imported": 0,
                      "updated": now.isoformat()}
            db.set("betboom_status", report)
            return report
