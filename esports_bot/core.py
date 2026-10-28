"""Data contracts, a chronological Elo baseline and transparent risk arithmetic."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SPORTS = {"csgo": "CS2", "dota2": "Dota 2"}
MODEL_VERSION = "elo-series-format-v1-k24"


class BotError(Exception):
    """An expected error safe to display, without secrets or request URLs."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, AttributeError):
        return None


def decimal(value: str, lower: str, upper: str) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        raise BotError("Нужно обычное конечное число.") from None
    if not result.is_finite() or not Decimal(lower) <= result <= Decimal(upper):
        raise BotError(f"Число должно быть от {lower} до {upper}.")
    return result


def cents(value: str) -> int:
    number = decimal(value, "0", "100000000")
    if number != number.quantize(Decimal("0.01")):
        raise BotError("Укажи сумму максимум с двумя знаками после запятой.")
    return int(number * 100)


def money(value: int) -> str:
    return f"{value / 100:,.2f}".replace(",", " ")


def payout(stake: int, odds: str) -> int:
    return int((Decimal(stake) * decimal(odds, "1.01", "1000")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Match:
    id: int
    sport: str
    a_id: int
    a: str
    b_id: int
    b: str
    start: str | None
    end: str | None
    status: str
    best_of: int
    match_type: str
    winner_id: int | None
    tournament: str
    tournament_id: int | None
    special: bool = False

    @property
    def key(self) -> str:
        return f"{self.sport}:{self.id}"

    @property
    def eligible(self) -> bool:
        return (self.sport in SPORTS and self.match_type == "best_of"
                and self.best_of in (1, 3, 5) and not self.special
                and self.a_id != self.b_id)

    def finished_before(self, cutoff: datetime) -> bool:
        start, end = timestamp(self.start), timestamp(self.end)
        return bool(self.eligible and self.status == "finished"
                    and self.winner_id in (self.a_id, self.b_id)
                    and start and end and start < end < cutoff)

    def data(self) -> dict:
        return asdict(self)


def normalize(raw: dict, sport: str) -> Match | None:
    """Only team vs team. Missing fields fail closed for model eligibility."""
    try:
        teams = raw.get("opponents", [])
        if len(teams) != 2 or any(t.get("type") != "Team" for t in teams):
            return None
        a, b = (t["opponent"] for t in teams)
        game_advantage = raw.get("game_advantage")
        league = (raw.get("league") or {}).get("name", "")
        tournament = raw.get("tournament") or {}
        winner = raw.get("winner_id")
        return Match(
            id=int(raw["id"]), sport=sport, a_id=int(a["id"]), a=str(a["name"]),
            b_id=int(b["id"]), b=str(b["name"]),
            start=raw.get("begin_at") or raw.get("scheduled_at"), end=raw.get("end_at"),
            status=str(raw.get("status", "unknown")),
            best_of=int(raw.get("number_of_games") or 0),
            match_type=str(raw.get("match_type") or "unknown"),
            winner_id=int(winner) if winner is not None else None,
            tournament=f"{league} / {tournament.get('name', '')}".strip(" /"),
            tournament_id=tournament.get("id"),
            special=bool(raw.get("forfeit") or raw.get("draw")
                         or game_advantage not in (None, 0, False)),
        )
    except (KeyError, ValueError, TypeError):
        return None


class Elo:
    """Separate series-level ratings for each game AND BO format; no map claims."""
    def __init__(self) -> None:
        self.rating: dict[tuple, float] = {}
        self.count: dict[tuple, int] = {}
        self.last: dict[tuple, datetime] = {}

    @staticmethod
    def teamkey(match: Match, team: int) -> tuple:
        return match.sport, match.best_of, team

    def probability(self, match: Match) -> float:
        a = self.rating.get(self.teamkey(match, match.a_id), 1500.0)
        b = self.rating.get(self.teamkey(match, match.b_id), 1500.0)
        # Clipping is a numerical safeguard, not a confidence interval.
        return min(.95, max(.05, 1 / (1 + 10 ** ((b - a) / 400))))

    def update(self, match: Match) -> None:
        p = self.probability(match)
        change = 24 * ((match.winner_id == match.a_id) - p)
        for team, delta in ((match.a_id, change), (match.b_id, -change)):
            key = self.teamkey(match, team)
            self.rating[key] = self.rating.get(key, 1500.0) + delta
            self.count[key] = self.count.get(key, 0) + 1
            self.last[key] = timestamp(match.end)

    def issues(self, match: Match, now: datetime) -> list[str]:
        if not match.eligible:
            return ["Формат/ничья/технический результат не поддерживается."]
        result = []
        for team, name in ((match.a_id, match.a), (match.b_id, match.b)):
            key = self.teamkey(match, team)
            if self.count.get(key, 0) < 10:
                result.append(f"{name}: меньше 10 результатов этого BO в загруженной истории.")
            last = self.last.get(key)
            if not last or (now - last).total_seconds() > 45 * 86400:
                result.append(f"{name}: нет недавнего результата за 45 дней.")
        return result


def train(history: list[Match], cutoff: datetime) -> Elo:
    model = Elo()
    for match in sorted((m for m in history if m.finished_before(cutoff)),
                        key=lambda m: (timestamp(m.end), m.key)):
        model.update(match)
    return model


def backtest(history: list[Match], cutoff: datetime) -> dict:
    """For every historical start, use only results that ENDED before that start.

    No tuning or calibration occurs here. This is a simple walk-forward baseline.
    Historical revisions/coverage bias remain: these are not point-in-time data.
    """
    rows = sorted((m for m in history if m.finished_before(cutoff)),
                  key=lambda m: (timestamp(m.start), m.key))
    endings = sorted(rows, key=lambda m: (timestamp(m.end), m.key))
    model, index, predictions = Elo(), 0, []
    for match in rows:
        begin = timestamp(match.start)
        while index < len(endings) and timestamp(endings[index].end) < begin:
            model.update(endings[index])
            index += 1
        if not model.issues(match, begin):
            p = model.probability(match)
            y = int(match.winner_id == match.a_id)
            predictions.append({"p": p, "y": y, "sport": match.sport,
                                "format": match.best_of, "key": match.key})
    def metrics(subset: list[dict]) -> dict:
        n = len(subset)
        if not n:
            return {"n": 0}
        return {"n": n,
                "brier": sum((r["p"] - r["y"]) ** 2 for r in subset) / n,
                "log_loss": -sum(r["y"] * math.log(r["p"]) +
                                 (1-r["y"]) * math.log(1-r["p"]) for r in subset) / n,
                "accuracy": sum((r["p"] >= .5) == bool(r["y"]) for r in subset) / n}
    bins = []
    for left in range(0, 100, 10):
        sample = [r for r in predictions if left / 100 <= r["p"] < (left+10) / 100]
        if sample:
            bins.append({"range": f"{left}–{left+10}%", "n": len(sample),
                         "predicted": sum(r["p"] for r in sample) / len(sample),
                         "observed": sum(r["y"] for r in sample) / len(sample)})
    return {"model": MODEL_VERSION, "total_history": len(rows),
            "overall": metrics(predictions),
            "by_game_format": {f"{sport}/BO{bo}": metrics([
                r for r in predictions if r["sport"] == sport and r["format"] == bo])
                for sport in SPORTS for bo in (1, 3, 5)},
            "calibration_bins": bins,
            "real_money_enabled": False,
            "limitations": ["Не откалибровано; вероятность — выход Elo, не установленная частота.",
                            "Нет исторических коэффициентов: ROI по истории не вычисляется.",
                            "Нет point-in-time составов, veto, патча и драфта.",
                            "Загруженная история может быть неполной и исправленной задним числом."]}


def market(odds_a: str, odds_b: str, p_a: float) -> dict:
    oa, ob = float(decimal(odds_a, "1.01", "1000")), float(decimal(odds_b, "1.01", "1000"))
    total = 1 / oa + 1 / ob
    return {"overround": total - 1,
            "market_a": (1 / oa) / total, "market_b": (1 / ob) / total,
            "break_even_a": 1 / oa, "break_even_b": 1 / ob,
            "ev_a": p_a * oa - 1, "ev_b": (1-p_a) * ob - 1}
