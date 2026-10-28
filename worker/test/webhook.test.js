import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { hash, claim, webhookSecret } from '../index.js';
import { D1 } from './d1.js';
import { initialize, bank } from '../db.js';
import { fixture, message, NOW, TOKEN } from './fixtures.js';

const envFor = db => ({ DB: db, TELEGRAM_BOT_TOKEN: TOKEN, EXPECTED_BOT_USERNAME: 'BETONFIRES_BOT' });
async function post(db, update, secret = null) {
  const request = new Request('https://my-worker.example/telegram', { method: 'POST', headers: {
    'Content-Type': 'application/json', 'X-Telegram-Bot-Api-Secret-Token': secret ?? await webhookSecret(TOKEN)
  }, body: JSON.stringify(update) });
  return worker.fetch(request, envFor(db));
}
function setupRequest(token = TOKEN, origin = 'https://my-worker.example', body = {}) {
  return new Request('https://my-worker.example/setup/activate', { method: 'POST', headers: {
    Origin: origin, Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'
  }, body: JSON.stringify(body) });
}
function mockTelegram(t, webhook = '') {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const method = url.split('/').at(-1), body = JSON.parse(init.body);
    calls.push({ method, body });
    const result = method === 'getMe' ? { username: 'BETONFIRES_BOT' } : method === 'getWebhookInfo' ? { url: webhook } : true;
    return Response.json({ ok: true, result });
  });
  return calls;
}

test('forged webhook is rejected before any D1 query', async t => {
  const db = new D1(); t.after(() => db.close());
  const r = await post(db, { update_id: 1, message: message('/bank') }, 'forged');
  assert.equal(r.status, 403); assert.equal(db.queries, 0);
});
test('owner ACL rejects groups, bots and other people without sending bank data', async t => {
  const db = await fixture(); t.after(() => db.close());
  for (const m of [message('/bank', 456), { ...message('/bank'), chat: { id: 123, type: 'group' } },
    { ...message('/bank'), from: { id: 123, is_bot: true } }]) {
    const r = await post(db, { update_id: 1, message: m });
    assert.deepEqual(await r.json(), { ok: true });
  }
});
test('duplicate Telegram update does not run a financial command twice', async t => {
  t.mock.method(Date, 'now', () => NOW);
  const db = await fixture(); t.after(() => db.close());
  db.queries = 0;
  const update = { update_id: 555, message: message('/paper csgo:90001 A') };
  const first = await post(db, update); assert.equal((await first.json()).method, 'sendMessage');
  assert.ok(db.queries < 50, `D1 query budget exceeded: ${db.queries}`);
  const second = await post(db, update); assert.deepEqual(await second.json(), { ok: true });
  assert.equal((await bank(db)).total, 1); assert.equal((await bank(db)).cash, 997500);
});
test('a crash after ledger commit cannot replay the stake on webhook retry', async t => {
  t.mock.method(Date, 'now', () => NOW);
  const db = await fixture(); t.after(() => db.close());
  db.failOnce = sql => sql.startsWith("UPDATE updates SET state='done'");
  const update = { update_id: 556, message: message('/paper csgo:90001 A') };
  const first = await post(db, update); assert.equal(first.status, 503);
  const second = await post(db, update); assert.equal(second.status, 200);
  assert.equal((await bank(db)).total, 1); assert.equal((await bank(db)).cash, 997500);
});
test('setup requires matching origin and token; public page exposes no credentials', async t => {
  const db = new D1(); t.after(() => db.close());
  assert.equal((await worker.fetch(setupRequest(TOKEN, 'https://other.example'), envFor(db))).status, 403);
  assert.equal((await worker.fetch(setupRequest('wrong'), envFor(db))).status, 401);
  assert.equal(db.queries, 0);
  const page = await worker.fetch(new Request('https://my-worker.example/'), envFor(db));
  const body = await page.text(); assert.equal(body.includes(TOKEN), false);
  assert.equal(page.headers.get('Cache-Control'), 'no-store');
});
test('activation signs webhook and creates single-use owner deep link without returning the bot token', async t => {
  t.mock.method(Date, 'now', () => NOW);
  const db = new D1(); t.after(() => db.close());
  const calls = mockTelegram(t);
  const response = await worker.fetch(setupRequest(), envFor(db));
  assert.equal(response.status, 200);
  const body = await response.json(); assert.ok(body.link.startsWith('https://t.me/BETONFIRES_BOT?start=pair_'));
  assert.equal(JSON.stringify(body).includes(TOKEN), false);
  const set = calls.find(c => c.method === 'setWebhook').body;
  assert.equal(set.url, 'https://my-worker.example/telegram'); assert.equal(set.drop_pending_updates, false);
  assert.equal(set.secret_token, await webhookSecret(TOKEN)); assert.equal(set.max_connections, 1);
  const code = body.link.split('pair_')[1];
  const first = await post(db, { update_id: 1, message: message('/start pair_' + code) });
  assert.ok((await first.json()).text.includes('привязан'));
  assert.equal((await db.prepare('SELECT owner FROM settings').first()).owner, 123);
  assert.equal(await claim(db, 456, '/start pair_' + code, NOW), false);
});
test('foreign webhook is not silently overwritten', async t => {
  const db = new D1(); t.after(() => db.close());
  const calls = mockTelegram(t, 'https://other.example/telegram');
  const response = await worker.fetch(setupRequest(), envFor(db));
  assert.equal(response.status, 409); assert.equal(calls.some(c => c.method === 'setWebhook'), false);
  const confirmed = await worker.fetch(setupRequest(TOKEN, 'https://my-worker.example', { replace: true }), envFor(db));
  assert.equal(confirmed.status, 200); assert.equal(calls.some(c => c.method === 'setWebhook'), true);
});
test('expired pairing and five failed codes do not claim an owner', async t => {
  const db = new D1(); t.after(() => db.close()); await initialize(db);
  const code = 'a'.repeat(48), wrong = 'b'.repeat(48);
  await db.prepare('UPDATE settings SET pairing_hash=?,pairing_expires=?').bind(await hash(code), NOW - 1).run();
  assert.equal(await claim(db, 123, '/pair ' + code, NOW), false);
  await db.prepare('UPDATE settings SET pairing_expires=?').bind(NOW + 100000).run();
  for (let i = 0; i < 5; i++) await claim(db, 456, '/pair ' + wrong, NOW);
  assert.equal(await claim(db, 456, '/pair ' + code, NOW), false);
  assert.equal((await db.prepare('SELECT owner FROM settings').first()).owner, null);
});
