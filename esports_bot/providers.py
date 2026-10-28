"""Documented HTTP integrations using Python's standard library."""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .core import BotError, SPORTS, normalize, utcnow
from .store import Store


def request_json(url: str, payload: dict | None = None,
                 headers: dict | None = None, timeout: int = 20):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "EsportsResearchBot/0.1", **(headers or {})})
    # Do not redirect authenticated requests to a different server.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        messages = {401: "Неверный API-токен.", 403: "Нет доступа на текущем плане API.",
                    409: "Telegram: уже работает другой polling-процесс или webhook.",
                    429: "Лимит запросов API. Повтор будет позже."}
        raise BotError(messages.get(code, f"Источник данных вернул HTTP {code}.")) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        raise BotError("Источник данных временно недоступен.") from None
    except (ValueError, TypeError):
        raise BotError("Источник вернул некорректный JSON.") from None


class PandaScore:
    def __init__(self, token: str) -> None:
        if not token:
            raise BotError("Нужен PANDASCORE_TOKEN в локальном .env.")
        self.token = token

    def page(self, sport: str, kind: str, number: int = 1, **params) -> list:
        if sport not in SPORTS or kind not in ("upcoming", "past", "all"):
            raise BotError("Неподдерживаемый запрос.")
        route = f"/{sport}/matches" + ("" if kind == "all" else f"/{kind}")
        query = urllib.parse.urlencode({"page": number, "per_page": 100, **params})
        data = request_json("https://api.pandascore.co" + route + "?" + query,
                            headers={"Authorization": "Bearer " + self.token})
        if not isinstance(data, list):
            raise BotError("PandaScore: ожидался список матчей.")
        if any(not isinstance(item, dict) for item in data):
            raise BotError("PandaScore: некорректная запись матча.")
        return data

    def sync(self, db: Store, pages: int = 2) -> dict:
        report = {}
        for sport in SPORTS:
            try:
                rows = []
                for number in range(1, pages+1):
                    batch = self.page(sport, "past", number, sort="-begin_at")
                    rows.extend(batch)
                    if len(batch) < 100:
                        break
                # Bound upcoming volume explicitly; report the cap rather than claim completeness.
                for number in range(1, 4):
                    batch = self.page(sport, "upcoming", number, sort="scheduled_at")
                    rows.extend(batch)
                    if len(batch) < 100:
                        break
                # Refresh canceled/postponed recent fixtures, and all open paper positions.
                rows.extend(self.page(sport, "all", sort="-modified_at"))
                opened = [t["match_key"].split(":")[1] for t in db.tickets("open")
                          if t["match_key"].startswith(sport+":")]
                for offset in range(0, len(opened), 50):
                    rows.extend(self.page(sport, "all", **{"filter[id]": ",".join(opened[offset:offset+50])}))
                normalized = [m for raw in rows if (m := normalize(raw, sport))]
                db.save_matches(normalized)
                db.set("sync:"+sport, utcnow().isoformat())
                db.set("sync_error:"+sport, None)
                report[sport] = {"received": len(rows), "parsed": len(normalized),
                                 "past_page_cap": pages, "upcoming_page_cap": 3}
            except BotError as error:
                db.set("sync_error:"+sport, str(error))
                report[sport] = {"error": str(error)}
        report["paper_settled"] = db.settle(utcnow())
        return report


class Telegram:
    def __init__(self, token: str) -> None:
        self.base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method: str, **payload):
        result = request_json(self.base + method, payload, timeout=35)
        if not isinstance(result, dict) or not result.get("ok"):
            raise BotError("Telegram не принял запрос.")
        return result["result"]

    def send(self, chat_id: int, message: str) -> None:
        for index in range(0, len(message), 3500):
            self.call("sendMessage", chat_id=chat_id, text=message[index:index+3500])
