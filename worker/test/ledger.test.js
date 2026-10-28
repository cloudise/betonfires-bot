import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture, match, NOW } from './fixtures.js';
import { bank, place, settleRows } from '../db.js';
import { command, context, analyze } from '../service.js';

test('paper placement and duplicate replay preserve exact cash and reference balance', async t => {
  const db = await fixture(); t.after(() => db.close());
  await command(db, '/balance 900000', 1, NOW, {});
  const text = await command(db, '/paper csgo:90001 A', 2, NOW, {});
  assert.ok(text.includes('только виртуальная'));
  assert.equal((await bank(db)).cash, 997500);
  await assert.rejects(command(db, '/paper csgo:90001 A', 3, NOW, {}));
  const b = await bank(db); assert.equal(b.total, 1); assert.equal(b.reference_balance, 90000000);
});
test('atomic SQL caps concurrent exposure at four 0.25% tickets', async t => {
  const db = await fixture(); t.after(() => db.close());
  const row = analyze(match(), await context(db), NOW);
  const results = await Promise.all(Array.from({ length: 8 }, (_, i) => place(db, match('csgo', 100000 + i), 'A', row.p, row.quote, 100 + i, NOW, 'test')));
  assert.equal(results.filter(Boolean).length, 4);
  assert.equal((await bank(db)).open, 10000);
});
test('settlement is idempotent; void returns stake; original snapshots stay immutable', async t => {
  const db = await fixture(); t.after(() => db.close());
  await command(db, '/paper csgo:90001 A', 1, NOW, {});
  const ticket = await db.prepare('SELECT * FROM tickets').first();
  await settleRows(db, [{ id: ticket.id, status: 'won', returned: 4750 }], NOW + 7200000);
  await settleRows(db, [{ id: ticket.id, status: 'won', returned: 4750 }], NOW + 7200000);
  assert.equal((await bank(db)).cash, 1002250);
  assert.equal((await db.prepare('SELECT snapshot FROM tickets').first()).snapshot, ticket.snapshot);
});
test('stale quote/source, changed sides and started event prohibit paper execution', async t => {
  const db = await fixture(); t.after(() => db.close());
  const ctx = await context(db), m = match();
  ctx.quotes[m.id].observed = NOW - 601000;
  assert.ok(analyze(m, ctx, NOW).blockers.length);
  await db.prepare('UPDATE quotes SET observed=?').bind(NOW - 601000).run();
  await assert.rejects(command(db, '/paper csgo:90001 A', 1, NOW, {}));
  ctx.quotes[m.id].observed = NOW;
  assert.ok(analyze({ ...m, a_id: 2, b_id: 1 }, ctx, NOW).blockers.some(s => s.includes('стороны')));
  ctx.states.csgo.fixture_updated = NOW - 1801000;
  assert.ok(analyze(m, ctx, NOW).blockers.some(s => s.includes('30 минут')));
});
test('cannot reset initialized bank or use a real balance as virtual funds', async t => {
  const db = await fixture(); t.after(() => db.close());
  await assert.rejects(command(db, '/paperbank 999999', 2, NOW, {}));
  assert.equal((await bank(db)).initial, 1000000);
  assert.ok((await command(db, '/analyze csgo:90001', 3, NOW, {})).includes('Реальная ставка: 0%'));
});
