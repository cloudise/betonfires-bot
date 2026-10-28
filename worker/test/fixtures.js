import { D1 } from './d1.js';
import { initialize, upsertMatches } from '../db.js';
import { train } from '../core.js';
export const NOW = Date.parse('2026-10-29T11:00:00Z');
export const TOKEN = '123456:synthetic-test-token-not-a-real-secret';
export function match(sport = 'csgo', id = 90001) {
  return { id: `${sport}:${id}`, match_id: id, sport, a_id: 1, a: 'DEMO One', b_id: 2, b: 'DEMO Two',
    start: NOW + 3600000, end: null, status: 'not_started', bo: 3, match_type: 'best_of',
    winner: null, special: false, tournament_id: 10, tournament: 'Fictional fixture' };
}
export function history(sport = 'csgo') {
  return Array.from({ length: 80 }, (_, i) => {
    const m = match(sport, i + 1), start = NOW - (81 - i) * 6 * 3600000;
    return { ...m, start, end: start + 3600000, status: 'finished', winner: i % 4 ? 1 : 2 };
  });
}
export async function fixture() {
  const db = new D1(); await initialize(db);
  await db.prepare('UPDATE settings SET owner=123,paper_initial=1000000 WHERE id=1').run();
  for (const sport of ['csgo', 'dota2']) {
    const m = match(sport), past = history(sport);
    await upsertMatches(db, [...past, m]);
    await db.prepare('INSERT INTO sync_state(sport,fixture_updated,results_updated,model) VALUES(?,?,?,?)')
      .bind(sport, NOW, NOW, JSON.stringify(train(past, NOW))).run();
    await db.prepare('INSERT INTO quotes VALUES(?,?,?,?,?,?)').bind(m.id, 1900, 1900, NOW, JSON.stringify(m), 'BetBoom manual').run();
  }
  return db;
}
export function message(text, id = 123) {
  return { text, from: { id, is_bot: false }, chat: { id, type: 'private' } };
}
