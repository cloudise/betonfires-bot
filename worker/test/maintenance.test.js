import test from 'node:test';
import assert from 'node:assert/strict';
import { job, maintenance, remoteJSON } from '../maintenance.js';
import { fixture, NOW, TOKEN } from './fixtures.js';
import { bank } from '../db.js';
import { command } from '../service.js';
import { upsertMatches } from '../db.js';
import { match } from './fixtures.js';

test('source auth stays in headers; unsupported games never enter requested routes', async t => {
  const db = await fixture(); t.after(() => db.close());
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => { calls.push({ url, init }); return Response.json([]); });
  await job(db, { PANDASCORE_TOKEN: 'synthetic-source-key' }, 0, NOW);
  assert.ok(calls[0].url.startsWith('https://api.pandascore.co/csgo/matches/upcoming?'));
  assert.equal(calls[0].url.includes('synthetic-source-key'), false);
  assert.equal(calls[0].init.headers.Authorization, 'Bearer synthetic-source-key');
});
test('failed source refresh never marks a fixture cache fresh or exposes secret-bearing URL', async t => {
  const db = await fixture(); t.after(() => db.close());
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('secret-token-bearing-upstream-url'); });
  await assert.rejects(job(db, { PANDASCORE_TOKEN: 'hidden' }, 0, NOW + 100000));
  const row = await db.prepare("SELECT * FROM sync_state WHERE sport='csgo'").first();
  assert.equal(row.fixture_updated, NOW); assert.ok(row.error); assert.equal(row.error.includes('secret-token'), false);
});
test('daily digest is claimed once per Moscow date, even on repeated cron calls', async t => {
  const db = await fixture(); t.after(() => db.close());
  await db.prepare("UPDATE settings SET digest_time='13:00'").run();
  const sends = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => { sends.push(JSON.parse(init.body)); return Response.json({ ok: true, result: {} }); });
  const env = { TELEGRAM_BOT_TOKEN: TOKEN };
  await maintenance(db, env, NOW); await maintenance(db, env, NOW + 60000);
  assert.equal(sends.length, 1); assert.equal(sends[0].chat_id, 123);
  await maintenance(db, env, NOW + 86400000); assert.equal(sends.length, 2);
});
test('cached result settlement survives restarts without a mutable cash counter', async t => {
  const db = await fixture(); t.after(() => db.close());
  await command(db, '/paper csgo:90001 A', 1, NOW, {});
  const row = await db.prepare("SELECT data FROM matches WHERE id='csgo:90001'").first();
  const m = { ...JSON.parse(row.data), start: NOW - 7200000, end: NOW - 3600000, status: 'finished', winner: 1 };
  await db.prepare("UPDATE matches SET data=? WHERE id='csgo:90001'").bind(JSON.stringify(m)).run();
  await maintenance(db, {}, NOW); await maintenance(db, {}, NOW + 60000);
  assert.equal((await bank(db)).cash, 1002250);
});
test('HTTP errors are fixed messages rather than remote descriptions with credentials', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('private remote detail', { status: 401 }));
  await assert.rejects(remoteJSON('https://example.invalid/secret'), { message: 'Сервис вернул HTTP 401.' });
});
test('unchanged fixtures do not amplify database writes and model query budget stays bounded', async t => {
  const db = await fixture(); t.after(() => db.close());
  const m = match();
  await upsertMatches(db, [m]);
  assert.equal(db.raw.prepare('SELECT changes() changes').get().changes, 0);
  db.queries = 0;
  await job(db, {}, 4, NOW);
  assert.ok(db.queries < 10);
  const model = JSON.parse((await db.prepare("SELECT model FROM sync_state WHERE sport='csgo'").first()).model);
  assert.equal(model.n, 80);
});
test('history backfill stops at five pages instead of spending quota indefinitely', async t => {
  const db = await fixture(); t.after(() => db.close());
  await db.prepare("UPDATE sync_state SET history_page=5 WHERE sport='csgo'").run();
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => { calls++; return Response.json(Array.from({ length: 100 }, (_, i) => ({ id: i }))); });
  await job(db, { PANDASCORE_TOKEN: 'test' }, 6, NOW);
  await job(db, { PANDASCORE_TOKEN: 'test' }, 6, NOW + 60000);
  assert.equal(calls, 1);
  assert.equal((await db.prepare("SELECT history_done FROM sync_state WHERE sport='csgo'").first()).history_done, 1);
});
