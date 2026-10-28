// Pure research arithmetic. No real wagers or unverified confidence claims.
export const GAMES = { csgo: 'CS2', dota2: 'Dota 2' };
export const MODEL = 'series-elo-free-v1';
export class UserError extends Error {}
export const iso = (ms) => new Date(ms).toISOString();
export function time(value) {
  if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(value)) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}
export function amount(input) {
  const s = String(input).replace(',', '.');
  if (!/^\d{1,9}(\.\d{1,2})?$/.test(s)) throw new UserError('Сумма: обычное число, максимум 2 знака после запятой.');
  const [whole, fraction = ''] = s.split('.');
  const result = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
  if (result > 10_000_000_000) throw new UserError('Максимум 100 000 000 условных единиц.');
  return result;
}
export function odds(input) {
  const s = String(input).replace(',', '.');
  if (!/^\d{1,4}(\.\d{1,3})?$/.test(s)) throw new UserError('Коэффициент: число от 1.01 до 1000, максимум 3 знака после запятой.');
  const [whole, fraction = ''] = s.split('.');
  const result = Number(whole) * 1000 + Number(fraction.padEnd(3, '0'));
  if (result < 1010 || result > 1_000_000) throw new UserError('Коэффициент должен быть от 1.01 до 1000.');
  return result;
}
export const money = (cents) => (cents / 100).toFixed(2);
export const pct = (p) => (p * 100).toFixed(1) + '%';
export const winReturn = (stake, milli) => Math.floor((stake * milli + 500) / 1000);
export function eligible(m) {
  return !!(m && GAMES[m.sport] && m.match_type === 'best_of' && [1, 3, 5].includes(m.bo)
    && !m.special && m.a_id !== m.b_id);
}
export function finished(m, now) {
  return eligible(m) && m.status === 'finished' && [m.a_id, m.b_id].includes(m.winner)
    && Number.isFinite(m.start) && Number.isFinite(m.end) && m.start < m.end && m.end < now;
}
export function normalize(raw, sport) {
  if (!raw || !GAMES[sport] || !Number.isSafeInteger(raw.id)) return null;
  const teams = raw.opponents;
  if (!Array.isArray(teams) || teams.length !== 2 || teams.some(t => t?.type !== 'Team')) return null;
  const [a, b] = teams.map(t => t.opponent);
  if (![a?.id, b?.id].every(Number.isSafeInteger) || !a.name || !b.name) return null;
  return { id: `${sport}:${raw.id}`, match_id: raw.id, sport,
    a_id: a.id, b_id: b.id, a: String(a.name).slice(0, 100), b: String(b.name).slice(0, 100),
    start: time(raw.begin_at || raw.scheduled_at), end: time(raw.end_at),
    status: raw.status || 'unknown', bo: raw.number_of_games || 0,
    match_type: raw.match_type || 'unknown', winner: raw.winner_id ?? null,
    special: !!(raw.forfeit || raw.draw || raw.game_advantage),
    tournament_id: raw.tournament?.id ?? null,
    tournament: [raw.league?.name, raw.tournament?.name].filter(Boolean).join(' / ').slice(0, 160) };
}
const teamKey = (m, id) => `${m.sport}:${m.bo}:${id}`;
export function probability(model, m) {
  const a = model.teams[teamKey(m, m.a_id)]?.rating ?? 1500;
  const b = model.teams[teamKey(m, m.b_id)]?.rating ?? 1500;
  return Math.min(.95, Math.max(.05, 1 / (1 + 10 ** ((b - a) / 400))));
}
export function train(history, now) {
  const model = { version: MODEL, updated: now, n: 0, teams: {} };
  const rows = history.filter(m => finished(m, now)).sort((a, b) => a.end - b.end || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  for (const m of rows) {
    const change = 24 * (Number(m.winner === m.a_id) - probability(model, m));
    for (const [id, delta] of [[m.a_id, change], [m.b_id, -change]]) {
      const key = teamKey(m, id), old = model.teams[key] || { rating: 1500, n: 0 };
      model.teams[key] = { rating: old.rating + delta, n: old.n + 1, last: m.end };
    }
    model.n++;
  }
  return model;
}
export function issues(model, m, now) {
  if (!eligible(m)) return ['Формат/ничья/технический результат не поддерживается.'];
  const result = [];
  for (const [id, label] of [[m.a_id, m.a], [m.b_id, m.b]]) {
    const t = model?.teams[teamKey(m, id)];
    if (!t || t.n < 10) result.push(`${label}: меньше 10 серий этого BO в загруженной истории.`);
    if (!t || now - t.last > 45 * 86400000) result.push(`${label}: нет недавней истории за 45 дней.`);
  }
  return result;
}
export function market(oa, ob, p) {
  const a = oa / 1000, b = ob / 1000, sum = 1 / a + 1 / b;
  return { margin: sum - 1, a: (1 / a) / sum, b: (1 / b) / sum,
    breakA: 1 / a, breakB: 1 / b, evA: p * a - 1, evB: (1 - p) * b - 1 };
}
export function related(a, b) {
  return a.id === b.id || a.sport === b.sport &&
    ([a.a_id, a.b_id].some(id => [b.a_id, b.b_id].includes(id)) ||
      a.tournament_id != null && a.tournament_id === b.tournament_id);
}
export function moscow(ms) {
  // Moscow uses UTC+3 without seasonal changes for the supported current schedule.
  // Avoid loading ICU formatters on a 10 ms Workers Free cold invocation.
  const value = new Date(ms + 3 * 3600000).toISOString();
  return { date: value.slice(0, 10), hhmm: value.slice(11, 16) };
}
export function heading(m) {
  const when = m.start ? moscow(m.start) : null;
  return `${GAMES[m.sport]} · BO${m.bo} · ${when ? when.date + ' ' + when.hhmm + ' МСК' : 'время неизвестно'}\n${m.id}\nA: ${m.a} / B: ${m.b}`;
}
export function outcome(ticket, m, now) {
  if (!m) return null;
  const original = JSON.parse(ticket.snapshot).match;
  const changed = ![m.a_id, m.b_id].includes(original.a_id) || ![m.a_id, m.b_id].includes(original.b_id);
  if (m.status === 'canceled' || m.status === 'finished' && (!eligible(m) || changed)) {
    return { id: ticket.id, status: 'void', returned: ticket.stake };
  }
  if (!finished(m, now)) return null;
  const won = m.winner === ticket.team_id;
  return { id: ticket.id, status: won ? 'won' : 'lost', returned: won ? winReturn(ticket.stake, ticket.odds_milli) : 0 };
}
