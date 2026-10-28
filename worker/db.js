// D1 owns persistence. Ledger effects use atomic SQL, not in-memory balances.
export const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), owner INTEGER,
    pairing_hash TEXT, pairing_expires INTEGER, paper_initial INTEGER NOT NULL DEFAULT 0,
    reference_balance INTEGER, digest_time TEXT)`,
  `INSERT OR IGNORE INTO settings(id) VALUES(1)`,
  `CREATE TABLE IF NOT EXISTS matches (id TEXT PRIMARY KEY, sport TEXT NOT NULL,
    starts INTEGER, ends INTEGER, status TEXT NOT NULL, data TEXT NOT NULL)`,
  `CREATE INDEX IF NOT EXISTS matches_slate ON matches(status, starts)`,
  `CREATE INDEX IF NOT EXISTS matches_history ON matches(sport, status, ends)`,
  `CREATE TABLE IF NOT EXISTS sync_state (sport TEXT PRIMARY KEY, fixture_updated INTEGER NOT NULL DEFAULT 0,
    results_updated INTEGER NOT NULL DEFAULT 0,
    error TEXT, history_page INTEGER NOT NULL DEFAULT 1, history_done INTEGER NOT NULL DEFAULT 0,
    model TEXT)`,
  `CREATE TABLE IF NOT EXISTS quotes (match_key TEXT PRIMARY KEY, a INTEGER NOT NULL, b INTEGER NOT NULL,
    observed INTEGER NOT NULL, match_snapshot TEXT NOT NULL, source TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, match_key TEXT NOT NULL UNIQUE,
    side TEXT NOT NULL CHECK(side IN ('A','B')), team_id INTEGER NOT NULL, stake INTEGER NOT NULL CHECK(stake>0),
    odds_milli INTEGER NOT NULL, probability REAL NOT NULL, model TEXT NOT NULL, created INTEGER NOT NULL,
    snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', returned INTEGER NOT NULL DEFAULT 0,
    settled INTEGER, event_id INTEGER NOT NULL UNIQUE)`,
  `CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status)`,
  `CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, created INTEGER NOT NULL, state TEXT NOT NULL, response TEXT)`,
  `CREATE TABLE IF NOT EXISTS pairing_attempts (sender INTEGER PRIMARY KEY, count INTEGER NOT NULL, started INTEGER NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS digests (day TEXT PRIMARY KEY, status TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS maintenance (id INTEGER PRIMARY KEY CHECK(id=1), last_run INTEGER NOT NULL DEFAULT 0,
    cursor INTEGER NOT NULL DEFAULT 0, error TEXT)`,
  `INSERT OR IGNORE INTO maintenance(id) VALUES(1)`
];
export async function initialize(db) { await db.batch(SCHEMA.map(sql => db.prepare(sql))); }
export const settings = (db) => db.prepare('SELECT * FROM settings WHERE id=1').first();
export async function match(db, key) {
  const row = await db.prepare('SELECT data FROM matches WHERE id=?').bind(key).first();
  return row ? JSON.parse(row.data) : null;
}
export async function slate(db, now) {
  const { results } = await db.prepare("SELECT data FROM matches WHERE status='not_started' AND starts>? AND starts<=? ORDER BY starts LIMIT 40").bind(now, now + 172800000).all();
  return results.map(r => JSON.parse(r.data));
}
export async function upsertMatches(db, rows) {
  if (!rows.length) return;
  await db.prepare(`INSERT INTO matches(id,sport,starts,ends,status,data)
    SELECT json_extract(value,'$.id'),json_extract(value,'$.sport'),json_extract(value,'$.start'),
      json_extract(value,'$.end'),json_extract(value,'$.status'),value FROM json_each(?) WHERE 1
    ON CONFLICT(id) DO UPDATE SET starts=excluded.starts,ends=excluded.ends,status=excluded.status,data=excluded.data
    WHERE matches.data<>excluded.data`).bind(JSON.stringify(rows)).run();
}
export async function bank(db) {
  const row = await db.prepare(`SELECT s.paper_initial initial, s.reference_balance reference_balance,
    s.paper_initial+COALESCE(SUM(CASE WHEN t.status<>'open' THEN t.returned-t.stake ELSE 0 END),0) equity,
    COALESCE(SUM(CASE WHEN t.status='open' THEN t.stake ELSE 0 END),0) open,
    COALESCE(SUM(CASE WHEN t.status IN ('won','lost') THEN t.stake ELSE 0 END),0) wagered,
    COUNT(t.id) total, COALESCE(SUM(CASE WHEN t.id IS NOT NULL AND t.status<>'open' THEN 1 ELSE 0 END),0) settled
    FROM settings s LEFT JOIN tickets t ON 1=1 WHERE s.id=1 GROUP BY s.id`).first();
  return { ...row, cash: row.equity - row.open, pnl: row.equity - row.initial,
    roi: row.wagered ? (row.equity - row.initial) / row.wagered : null };
}
export async function place(db, m, side, probability, quote, event, now, version) {
  // The bankroll and rolling exposure are re-read INSIDE this single write.
  // Thus concurrent requests cannot spend based on the same stale JS balance.
  const result = await db.prepare(`WITH b AS (
      SELECT s.paper_initial initial,
        s.paper_initial+COALESCE(SUM(CASE WHEN t.status<>'open' THEN t.returned-t.stake ELSE 0 END),0) equity,
        COALESCE(SUM(CASE WHEN t.status='open' THEN t.stake ELSE 0 END),0) opened,
        COALESCE(SUM(CASE WHEN t.created>? THEN t.stake ELSE 0 END),0) recent
      FROM settings s LEFT JOIN tickets t ON 1=1 WHERE s.id=1 GROUP BY s.id)
    INSERT INTO tickets(match_key,side,team_id,stake,odds_milli,probability,model,created,snapshot,event_id)
    SELECT ?,?,?,CAST(equity/400 AS INTEGER),?,?,?,?,?,? FROM b
    WHERE initial>0 AND CAST(equity/400 AS INTEGER)>0
      AND equity-opened>=CAST(equity/400 AS INTEGER)
      AND opened+CAST(equity/400 AS INTEGER)<=CAST(equity/100 AS INTEGER)
      AND recent+CAST(equity/400 AS INTEGER)<=CAST(equity/100 AS INTEGER)
      AND NOT EXISTS(SELECT 1 FROM tickets WHERE match_key=?)
      AND NOT EXISTS(SELECT 1 FROM tickets WHERE event_id=?)`)
    .bind(now - 86400000, m.id, side, side === 'A' ? m.a_id : m.b_id,
      side === 'A' ? quote.a : quote.b, side === 'A' ? probability : 1 - probability,
      version, now, JSON.stringify({ match: m, quote }), event, m.id, event).run();
  return result.meta.changes > 0;
}
export async function openTickets(db) {
  const { results } = await db.prepare("SELECT t.*,m.data match_data FROM tickets t LEFT JOIN matches m ON m.id=t.match_key WHERE t.status='open' LIMIT 200").all();
  return results;
}
export async function settleRows(db, changes, now) {
  if (!changes.length) return;
  await db.prepare(`UPDATE tickets SET
    status=(SELECT json_extract(value,'$.status') FROM json_each(?1) WHERE json_extract(value,'$.id')=tickets.id),
    returned=(SELECT json_extract(value,'$.returned') FROM json_each(?1) WHERE json_extract(value,'$.id')=tickets.id),
    settled=?2 WHERE status='open' AND id IN(SELECT json_extract(value,'$.id') FROM json_each(?1))`)
    .bind(JSON.stringify(changes), now).run();
}
