import { normalize, train, outcome, moscow } from './core.js';
import * as store from './db.js';
import { daily } from './service.js';

export async function remoteJSON(url, init = {}) {
  let response;
  try { response = await fetch(url, { ...init, redirect: 'error', signal: AbortSignal.timeout(15000) }); }
  catch { throw new Error('Сервис временно недоступен.'); }
  if (!response.ok) throw new Error(`Сервис вернул HTTP ${response.status}.`);
  try { return await response.json(); } catch { throw new Error('Сервис вернул некорректный JSON.'); }
}
export async function telegram(env, method, payload = {}) {
  const result = await remoteJSON(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  });
  if (!result?.ok) throw new Error('Telegram не принял запрос.');
  return result.result;
}
async function source(env, sport, route, params = {}) {
  const query = new URLSearchParams({ per_page: '100', ...params });
  const suffix = route ? '/' + route : '';
  const raw = await remoteJSON(`https://api.pandascore.co/${sport}/matches${suffix}?${query}`, {
    headers: { Authorization: `Bearer ${env.PANDASCORE_TOKEN}`, Accept: 'application/json' }
  });
  if (!Array.isArray(raw) || raw.some(r => typeof r !== 'object' || r === null)) throw new Error('PandaScore: ожидался список матчей.');
  return { rows: raw.map(r => normalize(r, sport)).filter(Boolean), count: raw.length };
}
async function updateState(db, sport, field, now) {
  if (!['fixture_updated', 'results_updated'].includes(field)) throw new Error('Invalid state field');
  await db.prepare(`INSERT INTO sync_state(sport,${field},error) VALUES(?,?,NULL)
    ON CONFLICT(sport) DO UPDATE SET ${field}=excluded.${field},error=NULL`).bind(sport, now).run();
}
export async function job(db, env, number, now) {
  const sport = [0, 1, 4, 6, 8].includes(number) ? 'csgo' : 'dota2';
  try {
    if ([0, 2].includes(number)) {
      const data = await source(env, sport, 'upcoming', { sort: 'scheduled_at' });
      await store.upsertMatches(db, data.rows);
      await updateState(db, sport, 'fixture_updated', now);
    } else if ([1, 3].includes(number)) {
      const data = await source(env, sport, 'past', { sort: '-begin_at' });
      await store.upsertMatches(db, data.rows);
      await updateState(db, sport, 'results_updated', now);
    } else if ([4, 5].includes(number)) {
      const rows = await db.prepare("SELECT data FROM matches WHERE sport=? AND status='finished' AND ends<? ORDER BY ends DESC LIMIT 500").bind(sport, now).all();
      const model = train(rows.results.map(r => JSON.parse(r.data)), now);
      await db.prepare('INSERT INTO sync_state(sport,model) VALUES(?,?) ON CONFLICT(sport) DO UPDATE SET model=excluded.model')
        .bind(sport, JSON.stringify(model)).run();
    } else if ([6, 7].includes(number)) {
      const state = await db.prepare('SELECT history_page,history_done FROM sync_state WHERE sport=?').bind(sport).first();
      if (state?.history_done) return;
      const page = Math.max(2, state?.history_page || 2);
      const data = await source(env, sport, 'past', { sort: '-begin_at', page: String(page) });
      await store.upsertMatches(db, data.rows);
      await db.prepare(`INSERT INTO sync_state(sport,history_page,history_done) VALUES(?,?,?)
        ON CONFLICT(sport) DO UPDATE SET history_page=excluded.history_page,history_done=excluded.history_done`)
        .bind(sport, page + 1, Number(data.count < 100 || page >= 5)).run();
    } else if ([8, 9].includes(number)) {
      const data = await source(env, sport, '', { sort: '-modified_at' });
      await store.upsertMatches(db, data.rows);
    } else if (number === 10) {
      const open = await store.openTickets(db);
      for (const game of ['csgo', 'dota2']) {
        const ids = open.filter(t => t.match_key.startsWith(game + ':')).map(t => t.match_key.split(':')[1]);
        if (ids.length) {
          const data = await source(env, game, '', { 'filter[id]': ids.slice(0, 50).join(',') });
          await store.upsertMatches(db, data.rows);
        }
      }
    } else if (number === 11) {
      await db.batch([
        db.prepare('DELETE FROM quotes WHERE observed<?').bind(now - 86400000),
        db.prepare('DELETE FROM updates WHERE created<?').bind(now - 30 * 86400000),
        db.prepare('DELETE FROM pairing_attempts WHERE started<?').bind(now - 86400000)
      ]);
    }
  } catch (error) {
    if (number < 10) await db.prepare('INSERT INTO sync_state(sport,error) VALUES(?,?) ON CONFLICT(sport) DO UPDATE SET error=excluded.error')
      .bind(sport, String(error.message).slice(0, 160)).run();
    throw error;
  }
}
export async function maintenance(db, env, now) {
  const slot = await db.prepare(`UPDATE maintenance SET last_run=?,cursor=(cursor+1)%12
    WHERE id=1 AND last_run<=? RETURNING (cursor+11)%12 AS job`).bind(now, now - 55000).first();
  if (!slot) return;
  const config = await store.settings(db);
  if (!config.owner) return;
  try {
    if (env.PANDASCORE_TOKEN || slot.job === 11) await job(db, env, slot.job, now);
    const open = await store.openTickets(db);
    const changes = open.map(t => outcome(t, t.match_data ? JSON.parse(t.match_data) : null, now)).filter(Boolean);
    await store.settleRows(db, changes, now);
    await db.prepare('UPDATE maintenance SET error=NULL WHERE id=1').run();
  } catch (error) {
    // Only sanitized fixed HTTP errors reach persistence; URLs/keys never do.
    await db.prepare('UPDATE maintenance SET error=? WHERE id=1').bind(String(error.message).slice(0, 160)).run();
  }
  const local = moscow(now);
  if (config.digest_time && local.hhmm >= config.digest_time) {
    const claimed = await db.prepare("INSERT OR IGNORE INTO digests(day,status) VALUES(?,'sending')").bind(local.date).run();
    if (claimed.meta.changes) {
      try {
        const text = (await daily(db, now)).slice(0, 3500);
        await telegram(env, 'sendMessage', { chat_id: config.owner, text });
        await db.prepare("UPDATE digests SET status='sent' WHERE day=?").bind(local.date).run();
      } catch {
        await db.prepare("UPDATE digests SET status='failed' WHERE day=?").bind(local.date).run();
      }
    }
  }
}
