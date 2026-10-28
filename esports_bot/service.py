"""User-facing research commands. Real-money recommendation stays at zero."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .core import (MODEL_VERSION, SPORTS, BotError, Match, backtest, cents,
                   market, money, timestamp, train)
from .store import Store

MOSCOW = ZoneInfo("Europe/Moscow")
NOTICE = "CS2 / Dota 2 · экспериментальная модель · виртуальные сделки\nРекомендация для реальных денег: 0%. Гарантий дохода нет."

HELP = """ESPORTS LAB · только CS2 и Dota 2
Это исследовательский бот, модель не откалибрована.

/matches — ближайшие матчи на 48 часов
/analyze csgo:123 — разбор матча
/odds csgo:123 1.80 2.05 БК — вручную задать оба коэффициента
/betboom — состояние необязательного импорта линии BetBoom
/status — состояние подключений
Только победитель всей серии, A/B в порядке /analyze.
/today — кандидаты для виртуального наблюдения
/express — условный сценарий из двух событий; может не быть
/express csgo:123:A dota2:456:B — калькулятор экспресса
/balance 10000 — реальный баланс для справки, без доступа к БК
/paperbank 10000 — один раз создать виртуальный банк
/paper csgo:123 A — записать виртуальную сделку
/bank — балансы, открытый риск, P&L
/journal — последние записи
/model — проверка модели на истории
/subscribe 10:00 — ежедневная сводка, время Москвы
/unsubscribe — отключить сводку
/help — команды

Никаких автоматических ставок и пополнений. Виртуальная ставка:
0.25% банка; максимум 1% за 24 часа и в открытых сделках.
Это параметры эксперимента, а не безопасные проценты реальных ставок.
"""


class Service:
    def __init__(self, db: Store) -> None:
        self.db = db

    def slate(self, now: datetime) -> list[Match]:
        return sorted((m for m in self.db.matches()
                       if m.status == "not_started" and timestamp(m.start)
                       and now < timestamp(m.start) <= now+timedelta(hours=48)),
                      key=lambda m: (timestamp(m.start), m.key))

    def analysis(self, match: Match, now: datetime, model=None) -> dict:
        model = model or train(self.db.matches(), now)
        blockers = model.issues(match, now)
        synced = timestamp(self.db.get("sync:"+match.sport))
        if not synced or not 0 <= (now-synced).total_seconds() <= 1800:
            blockers.append("Расписание и результаты не обновлялись в последние 30 минут.")
        if self.db.get("sync_error:"+match.sport):
            blockers.append("Последнее обновление источника завершилось ошибкой.")
        if (match.status != "not_started" or not timestamp(match.start)
                or timestamp(match.start) <= now):
            blockers.append("Матч уже начался, отменён, перенесён или время неизвестно.")
        quote = self.db.quote(match.key)
        p = model.probability(match) if not model.issues(match, now) else None
        stats = None
        if quote:
            observed = timestamp(quote["observed"])
            if not observed or not 0 <= (now-observed).total_seconds() <= 600:
                blockers.append("Коэффициенты старше 10 минут или имеют неверное время.")
            if (quote["a_id"], quote["b_id"], quote["start"]) != (match.a_id, match.b_id, match.start):
                blockers.append("После ввода коэффициентов изменились стороны или время матча.")
            if p is not None:
                stats = market(quote["a"], quote["b"], p)
                if not 0 <= stats["overround"] <= .25:
                    blockers.append("Проверь линию: сумма обратных коэффициентов вне ожидаемого диапазона.")
        else:
            blockers.append("Нет коэффициентов: /odds ID КФ_A КФ_B источник.")
        side = None
        if not blockers and stats:
            if max(stats["ev_a"], stats["ev_b"]) > 0:
                side = "A" if stats["ev_a"] >= stats["ev_b"] else "B"
            else:
                blockers.append("У базовой модели нет положительного расчётного EV.")
        return {"match": match, "p": p, "quote": quote, "market": stats,
                "blockers": blockers, "side": side, "synced": synced}

    @staticmethod
    def headline(match: Match) -> str:
        when = timestamp(match.start)
        time = when.astimezone(MOSCOW).strftime("%d.%m %H:%M МСК") if when else "время неизвестно"
        return (f"{SPORTS[match.sport]} · BO{match.best_of} · {time}\n"
                f"{match.key} · A: {match.a} / B: {match.b}\n{match.tournament}")

    def render_analysis(self, row: dict, now: datetime) -> str:
        m, p = row["match"], row["p"]
        lines = [self.headline(m)]
        if p is None:
            lines.append("Вероятность не опубликована: недостаточно подходящих данных.")
        else:
            lines += [f"Сырой выход Elo: A {p:.1%}, B {1-p:.1%}.",
                      "Это не подтверждённые вероятности; калибровка не выполнена."]
        if row["quote"]:
            q = row["quote"]
            mode = "импорт API" if q.get("provenance", {}).get("mode") == "api" else "ручной ввод"
            lines.append(f"КФ A/B: {q['a']} / {q['b']} · источник: {q['source']} ({mode})")
            observed = timestamp(q["observed"])
            if observed:
                lines.append("Время источника коэффициентов: " + observed.astimezone(MOSCOW).strftime("%d.%m %H:%M:%S МСК"))
        if row["market"]:
            s = row["market"]
            lines += [f"Маржа линии: {s['overround']:.1%}; нормированные веса рынка: "
                      f"A {s['market_a']:.1%}, B {s['market_b']:.1%}.",
                      f"Порог безубыточности: A {s['break_even_a']:.1%}, B {s['break_even_b']:.1%}.",
                      f"Условный EV модели: A {s['ev_a']:+.1%}, B {s['ev_b']:+.1%}.",
                      "EV использует непроверенную модель, поэтому не доказывает преимущество над БК."]
        if row["blockers"]:
            lines.append("ПРОПУСК виртуальной сделки:\n" + "\n".join("• "+x for x in row["blockers"]))
        elif row["side"]:
            side = row["side"]
            stake = self.db.paper_stake(now)
            lines.append(f"Кандидат только для теста: {side}. Виртуальная сумма: {money(stake)} ед.")
        lines += ["Не учтены: актуальные составы/замены, veto и карты CS2, патч и драфт Dota 2.",
                  "Реальная ставка: 0%. Полная сумма ставки может быть потеряна."]
        return "\n".join(lines)

    def daily(self, now: datetime) -> str:
        matches = self.slate(now)
        model = train(self.db.matches(), now)
        lines = [NOTICE, "Сводка ближайших 48 часов"]
        candidates = []
        for match in matches:
            row = self.analysis(match, now, model)
            if row["side"]:
                candidates.append(row)
        candidates.sort(key=lambda r: max(r["market"]["ev_a"], r["market"]["ev_b"]), reverse=True)
        if not candidates:
            lines.append("Подходящих кандидатов для виртуального теста сейчас нет.")
        for row in candidates[:5]:
            lines.append(self.headline(row["match"]) + f"\nВиртуальный кандидат {row['side']}; /analyze {row['match'].key}")
        lines.append(self.express([], now, candidates=candidates))
        lines.append(self.betboom_text())
        return "\n\n".join(lines)

    def express(self, args: list[str], now: datetime, candidates=None) -> str:
        model = train(self.db.matches(), now)
        if args:
            if len(args) != 2:
                raise BotError("Формат: /express csgo:123:A dota2:456:B (ровно два разных матча).")
            selected = []
            for value in args:
                try:
                    key, side = value.rsplit(":", 1)
                except ValueError:
                    raise BotError("Нужен ID матча и сторона :A или :B.") from None
                side = side.upper()
                row = self.analysis(self.db.match(key), now, model)
                if side not in ("A", "B") or row["blockers"]:
                    raise BotError("Для расчёта нужны два исхода A/B без блокировок в /analyze.")
                selected.append((row, side))
        else:
            candidates = candidates if candidates is not None else [
                r for m in self.slate(now) if (r := self.analysis(m, now, model))["side"]]
            selected = []
            for row in candidates:
                if not selected or not self.related(selected[0][0]["match"], row["match"]):
                    selected.append((row, row["side"]))
                if len(selected) == 2:
                    break
            if len(selected) < 2:
                return "Экспресс дня не сформирован: нет двух подходящих событий для расчёта."
        (first, sa), (second, sb) = selected
        if self.related(first["match"], second["match"]):
            raise BotError("Совпали матч, команда или турнир. Такой экспресс не рассчитывается.")
        pa = first["p"] if sa == "A" else 1-first["p"]
        pb = second["p"] if sb == "A" else 1-second["p"]
        odds = float(first["quote"]["a" if sa == "A" else "b"]) * float(second["quote"]["a" if sb == "A" else "b"])
        return (f"Условный сценарий экспресса: {first['match'].key}:{sa} + {second['match'].key}:{sb}\n"
                f"Произведение КФ: {odds:.3f} (в БК может отличаться).\n"
                f"ТОЛЬКО при независимости и верности Elo: оба исхода {pa*pb:.1%}, "
                f"проигрыш всей ставки {1-pa*pb:.1%}.\n"
                "Независимость не доказана; это калькулятор, не прогноз точности. Реальная ставка: 0%.")

    @staticmethod
    def related(a: Match, b: Match) -> bool:
        if a.key == b.key:
            return True
        return a.sport == b.sport and (
            bool({a.a_id, a.b_id} & {b.a_id, b.b_id}) or
            (a.tournament_id is not None and a.tournament_id == b.tournament_id))

    def handle(self, text: str, now: datetime) -> str:
        aliases = {"Матчи": "/matches", "Банк": "/bank", "Статистика": "/model", "Помощь": "/help"}
        words = aliases.get(text.strip(), text).split()
        if not words:
            return HELP
        cmd, args = words[0].split("@")[0].lower(), words[1:]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/matches":
            matches = self.slate(now)
            lines = [NOTICE]
            for sport, label in SPORTS.items():
                sync = timestamp(self.db.get("sync:"+sport))
                value = sync.astimezone(MOSCOW).strftime("%d.%m %H:%M") if sync else "ещё не было"
                lines.append(f"{label}: обновление {value} МСК" +
                             (" · ошибка последнего обновления" if self.db.get("sync_error:"+sport) else ""))
            lines.extend(self.headline(m) for m in matches[:20])
            if not matches:
                lines.append("Матчей в кэше на следующие 48 часов нет. Это не доказывает, что матчей нет вообще.")
            if len(matches) > 20:
                lines.append(f"Показано 20 из {len(matches)}; полная сводка доступна в CLI report.")
            return "\n\n".join(lines)
        if cmd == "/analyze" and len(args) == 1:
            return self.render_analysis(self.analysis(self.db.match(args[0]), now), now)
        if cmd == "/odds" and len(args) >= 4:
            m = self.db.match(args[0])
            self.db.save_quote(m, args[1], args[2], now, " ".join(args[3:]))
            return "Оба коэффициента сохранены на 10 минут.\n" + self.render_analysis(self.analysis(m, now), now)
        if cmd == "/balance" and len(args) == 1:
            self.db.set("reference_balance", cents(args[0]))
            return "Справочный реальный баланс сохранён. Он не участвует в виртуальном журнале.\n" + self.bank_text()
        if cmd == "/paperbank" and len(args) == 1:
            self.db.initialize_bank(cents(args[0]))
            return self.bank_text()
        if cmd in ("/bank", "/stats"):
            return self.bank_text()
        if cmd == "/paper" and len(args) == 2:
            m = self.db.match(args[0])
            row = self.analysis(m, now)
            side = args[1].upper()
            if row["blockers"] or side != row["side"]:
                raise BotError("Виртуальная сделка не разрешена этим экспериментом.\n" + self.render_analysis(row, now))
            amount = self.db.place(m, side, row["p"], MODEL_VERSION, now,
                                   {"match": m.data(), "quote": row["quote"],
                                    "market": row["market"], "synced": row["synced"].isoformat()})
            return f"Только виртуальная запись: {m.key}, {side}, {money(amount)} ед.\n" + self.bank_text()
        if cmd == "/journal":
            rows = self.db.tickets()[-10:]
            return "Виртуальный журнал\n" + ("\n".join(
                f"#{t['id']} {t['match_key']} {t['side']} · {money(t['stake'])} @ {t['odds']} · "
                f"{t['status']} · возврат {money(t['returned'])}" for t in rows) or "Пока пусто.")
        if cmd == "/today":
            return self.daily(now)
        if cmd == "/betboom":
            return self.betboom_text()
        if cmd == "/status":
            configured = self.db.get("matches_source_configured")
            return ("Владелец привязан; команды доступны в личном чате.\n"
                    + ("Источник расписания настроен. Свежесть данных — /matches.\n" if configured
                       else "Источник расписания пока не настроен: добавь PANDASCORE_TOKEN в настройки хоста.\n")
                    + self.betboom_text() + "\nМодель экспериментальная; реальные ставки отключены.")
        if cmd == "/express":
            return self.express(args, now)
        if cmd == "/model":
            report = backtest(self.db.matches(), now)
            lines = [f"Модель {MODEL_VERSION}", f"Подходящих результатов в кэше: {report['total_history']}"]
            for key, value in report["by_game_format"].items():
                if value["n"]:
                    lines.append(f"{key}: n={value['n']}, Brier={value['brier']:.4f}, "
                                 f"log loss={value['log_loss']:.4f}, доля угаданных={value['accuracy']:.1%}")
                else:
                    lines.append(f"{key}: недостаточно истории после прогрева.")
            lines += ["Справочный прогноз 50/50: Brier 0.25; log loss 0.6931.",
                      "Каждый прогноз использует результаты, завершённые до начала этого матча.",
                      "Откалиброванность/преимущество не подтверждены. Победа над 50/50 не означает прибыль."]
            lines.extend(report["limitations"])
            return "\n".join(lines)
        if cmd == "/subscribe" and len(args) == 1:
            try:
                h, m = map(int, args[0].split(":"))
                if not 0 <= h <= 23 or not 0 <= m <= 59:
                    raise ValueError
            except ValueError:
                raise BotError("Время по Москве, например /subscribe 10:00.") from None
            self.db.set("digest_time", f"{h:02}:{m:02}")
            # New subscription starts with the next daily slot, never sends an old slot immediately.
            self.db.set("digest_last_date", now.astimezone(MOSCOW).date().isoformat()
                        if now.astimezone(MOSCOW).strftime("%H:%M") >= f"{h:02}:{m:02}" else None)
            return f"Сводка включена на {h:02}:{m:02} МСК. Она приходит, пока процесс бота работает."
        if cmd == "/unsubscribe":
            self.db.set("digest_time", None)
            return "Ежедневная сводка отключена."
        raise BotError("Команда или аргументы не распознаны. Примеры: /help.")

    def betboom_text(self) -> str:
        report = self.db.get("betboom_status")
        if not report:
            return "BetBoom: импорт линии не выполнялся. Доступен ручной ввод /odds."
        updated = timestamp(report.get("updated"))
        when = updated.astimezone(MOSCOW).strftime("%d.%m %H:%M МСК") if updated else "время неизвестно"
        if report.get("error"):
            return f"BetBoom через API-SPORT: ошибка обновления ({when}). Старые импортированные цены отключены."
        return (f"BetBoom через сторонний API-SPORT: последняя загрузка {when}. "
                f"Импортировано {report['imported']}, пропущено {report['skipped']}. "
                + ("Выборка ограничена числом страниц. " if report.get("partial") else "")
                + "Свежесть конкретной цены проверяется в /analyze; баланс БК вводится вручную.")

    def bank_text(self) -> str:
        b = self.db.bank()
        ref = self.db.get("reference_balance")
        return (f"Реальный баланс (ручная справка): {money(ref) if ref is not None else 'не задан'}\n"
                f"Виртуальный: доступно {money(b['cash'])}, в открытых сделках {money(b['open'])} ед.\n"
                f"Виртуальный P&L: {money(b['pnl'])}; ROI по закрытым ставкам: "
                f"{format(b['roi'], '.2%') if b['roi'] is not None else 'нет данных'}\n"
                f"Максимальная просадка по закрытым результатам: {b['max_drawdown']:.2%}.\n"
                f"Записей {b['tickets']}, закрыто {b['settled']}. Реальные деньги бот не переводит.")
