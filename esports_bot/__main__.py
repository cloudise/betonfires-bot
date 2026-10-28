"""python -m esports_bot demo|doctor|sync|report|run|export"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import SPORTS, BotError, Match, backtest, utcnow
from .betboom import BetBoomFeed
from .providers import PandaScore, Telegram
from .service import MOSCOW, Service
from .store import Store


def load_env(path: str = ".env") -> None:
    """Simple KEY=value only. Never executes shell code or overrides the process env."""
    target = Path(path)
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise BotError(".env: нужна строка ИМЯ=значение без shell-команд.")
        value = value.strip()
        if value.startswith(("'", '"')) and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def authorized(message: dict, owner_id: int) -> bool:
    chat, sender = message.get("chat", {}), message.get("from", {})
    return (chat.get("type") == "private" and chat.get("id") == owner_id
            and sender.get("id") == owner_id and not sender.get("is_bot", False))


def demo_store(now: datetime) -> Store:
    """Entirely fictional data, never persisted to the production database."""
    db = Store(":memory:")
    rows = []
    for sport in SPORTS:
        for pair in (0, 1):
            a, b = 1+pair*2, 2+pair*2
            for i in range(80):
                start = now-timedelta(hours=(80-i)*6+pair*2)
                rows.append(Match(
                    id=pair*1000+i+1, sport=sport, a_id=a, a=f"DEMO Team {a}",
                    b_id=b, b=f"DEMO Team {b}", start=start.isoformat(),
                    end=(start+timedelta(hours=1)).isoformat(), status="finished",
                    best_of=3, match_type="best_of", winner_id=a if i%4 else b,
                    tournament="ВЫМЫШЛЕННЫЙ турнир", tournament_id=pair+1))
        upcoming = Match(id=90001, sport=sport, a_id=1, a="DEMO Team 1",
                         b_id=2, b="DEMO Team 2", start=(now+timedelta(hours=6)).isoformat(),
                         end=None, status="not_started", best_of=3, match_type="best_of",
                         winner_id=None, tournament="ВЫМЫШЛЕННЫЙ турнир", tournament_id=99)
        rows.append(upcoming)
        db.set("sync:"+sport, now.isoformat())
        db.save_quote(upcoming, "1.90", "1.90", now, "DEMO: вымышленные коэффициенты")
    db.save_matches(rows)
    db.initialize_bank(1000000)
    return db


def digest_due(db: Store, now: datetime) -> bool:
    slot = db.get("digest_time")
    local = now.astimezone(MOSCOW)
    return bool(slot and local.strftime("%H:%M") >= slot
                and db.get("digest_last_date") != local.date().isoformat())


def claim_owner(db: Store, message: dict, code: str) -> int | None:
    """First owner must prove possession of the host's random pairing secret."""
    sender = message.get("from", {}).get("id")
    if type(sender) is not int or sender <= 0 or not authorized(message, sender):
        return None
    words = message.get("text", "").split()
    if len(words) != 2 or words[0].split("@")[0].lower() != "/pair":
        return None
    if len(code) < 24:
        raise BotError("Для привязки нужен PAIRING_CODE не короче 24 символов в настройках хоста.")
    now = utcnow()
    attempts = db.get(f"pair_fail:{sender}", {"count": 0, "at": now.isoformat()})
    began = datetime.fromisoformat(attempts["at"])
    if (now-began).total_seconds() >= 86400:
        attempts = {"count": 0, "at": now.isoformat()}
    if attempts["count"] >= 5:
        return None
    if not hmac.compare_digest(words[1].encode("utf-8"), code.encode("utf-8")):
        attempts["count"] += 1
        db.set(f"pair_fail:{sender}", attempts)
        return None
    db.db.execute("BEGIN IMMEDIATE")
    try:
        if db.get("telegram_owner") is not None:
            raise BotError("Владелец уже привязан; повторная привязка отключена.")
        db._set("telegram_owner", sender)
        db.db.commit()
    except Exception:
        db.db.rollback()
        raise
    return sender


def pair_owner(db: Store, telegram: Telegram, code: str) -> int:
    if len(code) < 24:
        raise BotError("Укажи OWNER_TELEGRAM_ID или PAIRING_CODE не короче 24 символов в настройках хоста.")
    print("Ожидание личной привязки через /pair. Код в лог не выводится.", flush=True)
    backoff = 2
    while True:
        try:
            updates = telegram.call("getUpdates", offset=db.get("telegram_offset", 0),
                                    timeout=25, allowed_updates=["message"])
            for update in updates:
                db.set("telegram_offset", update["update_id"]+1)
                message = update.get("message", {})
                owner = claim_owner(db, message, code)
                if owner:
                    telegram.send(owner, "Бот привязан к твоему аккаунту. Начинаю работу. Команды: /help.")
                    return owner
                sender = message.get("from", {}).get("id")
                if type(sender) is int and authorized(message, sender):
                    text = message.get("text", "")
                    if text.split("@")[0].strip() == "/start" or text.startswith("/pair "):
                        telegram.send(sender, "Для привязки отправь /pair КОД_ИЗ_НАСТРОЕК_ХОСТА. Чужие команды пока отключены.")
            backoff = 2
        except BotError as error:
            # The owner can already be committed even if the confirmation failed.
            if db.get("telegram_owner") is not None:
                return db.get("telegram_owner")
            print(str(error), file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(60, backoff*2)


def run(db: Store) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    requested_owner = os.environ.get("OWNER_TELEGRAM_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise BotError("Заполни TELEGRAM_BOT_TOKEN в настройках хоста или локальном .env.")
    if requested_owner and (not requested_owner.isdigit() or int(requested_owner) <= 0):
        raise BotError("OWNER_TELEGRAM_ID должен быть положительным числовым ID.")
    previous_owner = db.get("telegram_owner")
    if requested_owner and previous_owner is not None and previous_owner != int(requested_owner):
        raise BotError("Эта база привязана к другому владельцу. Для другого аккаунта используй отдельную базу.")
    telegram, service = Telegram(token), Service(db)
    pandascore_key = os.environ.get("PANDASCORE_TOKEN", "")
    provider = PandaScore(pandascore_key) if pandascore_key else None
    db.set("matches_source_configured", provider is not None)
    use_betboom = os.environ.get("BETBOOM_AUTO", "0") == "1"
    odds_feed = BetBoomFeed(os.environ.get("API_SPORT_KEY", "")) if use_betboom else None
    try:
        odds_interval = int(os.environ.get("BETBOOM_POLL_SECONDS", "300"))
        if not 60 <= odds_interval <= 3600:
            raise ValueError
    except ValueError:
        raise BotError("BETBOOM_POLL_SECONDS должен быть целым числом от 60 до 3600.") from None
    db.set("betboom_auto", use_betboom)
    me = telegram.call("getMe")
    print(f"Бот @{me['username']}: личный режим. Ctrl+C — остановка.", flush=True)
    # Never delete an existing webhook or discard queued updates implicitly.
    hook = telegram.call("getWebhookInfo")
    if hook.get("url"):
        raise BotError("У этого бота установлен webhook. Используй отдельного бота для polling или отключи webhook сам.")
    owner = int(requested_owner) if requested_owner else previous_owner
    if owner is None:
        owner = pair_owner(db, telegram, os.environ.get("PAIRING_CODE", ""))
    db.set("telegram_owner", owner)
    offset = db.get("telegram_offset", 0)
    next_sync, next_odds, backoff = 0.0, 0.0, 2
    while True:
        try:
            if provider and time.monotonic() >= next_sync:
                first_history = not db.get("history_bootstrap_attempted", False)
                db.set("history_bootstrap_attempted", True)
                report = provider.sync(db, pages=20 if first_history else 2)
                print(json.dumps({"sync": report}, ensure_ascii=False), flush=True)
                next_sync = time.monotonic()+900
            if odds_feed and time.monotonic() >= next_odds:
                print(json.dumps({"betboom": odds_feed.sync(db, utcnow())}, ensure_ascii=False), flush=True)
                next_odds = time.monotonic()+odds_interval
            if digest_due(db, utcnow()):
                # Persist intent before send to prevent duplicate digests after a restart.
                # A crash/send failure can skip one digest; /today is always available.
                db.set("digest_last_date", utcnow().astimezone(MOSCOW).date().isoformat())
                telegram.send(int(owner), service.daily(utcnow()))
            updates = telegram.call("getUpdates", offset=offset, timeout=25,
                                    allowed_updates=["message"])
            for update in updates:
                # At-most-once command execution: do not replay money mutations after a crash.
                offset = max(offset, update["update_id"]+1)
                db.set("telegram_offset", offset)
                message = update.get("message", {})
                if not authorized(message, int(owner)) or "text" not in message:
                    continue
                try:
                    response = service.handle(message["text"], utcnow())
                except BotError as error:
                    response = str(error)
                telegram.send(int(owner), response)
            backoff = 2
        except BotError as error:
            print(str(error), file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff*2, 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="CS2/Dota 2 — research and paper-trading bot")
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Офлайн-показ: только вымышленные матчи")
    sub.add_parser("doctor", help="Проверить конфигурацию, не показывая токены")
    sync = sub.add_parser("sync", help="Загрузить расписание и результаты")
    sync.add_argument("--pages", type=int, default=20)
    sub.add_parser("report", help="JSON: матчи, оценки, проверка модели")
    sub.add_parser("run", help="Запустить Telegram polling и обновления")
    sub.add_parser("betboom-sync", help="Один раз импортировать линию BetBoom через API-SPORT")
    export = sub.add_parser("export", help="Экспортировать виртуальный журнал")
    export.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        load_env()
        if args.command == "doctor":
            print("Python: " + sys.version.split()[0])
            for key in ("TELEGRAM_BOT_TOKEN", "PANDASCORE_TOKEN", "OWNER_TELEGRAM_ID", "API_SPORT_KEY"):
                print(key + ": " + ("задан" if os.environ.get(key) else "НЕ ЗАДАН"))
            print("Europe/Moscow: " + str(MOSCOW))
            print("Автоимпорт BetBoom: " + ("включён настройкой" if os.environ.get("BETBOOM_AUTO") == "1" else "выключен"))
            print("Сетевые соединения не проверялись. Реальные ставки отключены.")
            return 0
        if args.command == "demo":
            now = datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc)
            db = demo_store(now)
            try:
                print("ДЕМОНСТРАЦИЯ. ВСЕ команды, матчи, коэффициенты и результаты ВЫМЫШЛЕННЫЕ.\n")
                service = Service(db)
                print(service.daily(now))
                print("\n" + service.handle("/analyze csgo:90001", now))
                print("\n" + service.handle("/paper csgo:90001 A", now))
            finally:
                db.close()
            return 0
        db = Store(args.db or os.environ.get("BOT_DB") or "data/research.sqlite3")
        try:
            if args.command == "sync":
                if not 1 <= args.pages <= 100:
                    raise BotError("Число страниц: от 1 до 100. По 100 результатов на страницу и игру.")
                report = PandaScore(os.environ.get("PANDASCORE_TOKEN", "")).sync(db, args.pages)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return int(any("error" in report.get(sport, {}) for sport in SPORTS))
            if args.command == "run":
                run(db)
            elif args.command == "betboom-sync":
                report = BetBoomFeed(os.environ.get("API_SPORT_KEY", "")).sync(db, utcnow())
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return int("error" in report)
            elif args.command == "report":
                now, service = utcnow(), Service(db)
                model_report = backtest(db.matches(), now)
                rows = []
                for match in service.slate(now):
                    row = service.analysis(match, now)
                    row["match"] = match.data()
                    row["synced"] = row["synced"].isoformat() if row["synced"] else None
                    rows.append(row)
                print(json.dumps({"model": model_report, "matches": rows}, ensure_ascii=False, indent=2))
            elif args.command == "export":
                output = Path(args.out)
                if output.exists():
                    raise BotError("Файл экспорта уже существует. Выбери новое имя.")
                descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump({"mode": "paper_only", "bank": db.bank(), "tickets": db.tickets()},
                              file, ensure_ascii=False, indent=2)
                print("Виртуальный журнал экспортирован.")
        finally:
            db.close()
        return 0
    except BotError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Бот остановлен.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
