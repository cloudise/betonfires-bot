import test from 'node:test';
import assert from 'node:assert/strict';
import { amount, odds, winReturn, market, train, probability, issues, related, outcome, normalize, moscow } from '../core.js';
import { history, match, NOW } from './fixtures.js';

test('money rejects negative/nonfinite/excess precision; payouts use integer arithmetic', () => {
  for (const s of ['NaN', 'Infinity', '-1', '100.001', '1e6', '100000001']) assert.throws(() => amount(s));
  assert.equal(amount('100,50'), 10050); assert.equal(winReturn(150, odds('2.02')), 303);
  assert.throws(() => odds('1')); assert.throws(() => odds('1001')); assert.throws(() => odds('1.2345'));
});
test('margin, market weight and break-even are distinct', () => {
  const s = market(1900, 1900, .6);
  assert.equal(s.a, .5); assert.ok(Math.abs(s.breakA - 1 / 1.9) < 1e-12); assert.ok(Math.abs(s.evA - .14) < 1e-12);
});
test('ratings are separate by sport/BO and future results cannot train', () => {
  const m = match(), model = train(history(), NOW);
  assert.ok(probability(model, m) > .7);
  assert.equal(probability(model, { ...m, sport: 'dota2' }), .5);
  assert.equal(probability(model, { ...m, bo: 1 }), .5);
  const future = { ...m, status: 'finished', winner: 2, start: NOW - 100, end: NOW + 100 };
  assert.deepEqual(train([...history(), future], NOW), model);
  assert.ok(issues(model, { ...m, a_id: 999 }, NOW).length);
  assert.ok(issues(model, { ...m, bo: 2 }, NOW).length);
});
test('settlement follows team identity, refunds unsupported/canceled and waits for actual result', () => {
  const m = match(), ticket = { id: 1, side: 'A', team_id: 1, stake: 2500, odds_milli: 1900, snapshot: JSON.stringify({ match: m }) };
  assert.equal(outcome(ticket, m, NOW), null);
  const ended = { ...m, a_id: 2, b_id: 1, start: NOW - 3600000, end: NOW - 1000, status: 'finished', winner: 1 };
  assert.deepEqual(outcome(ticket, ended, NOW), { id: 1, status: 'won', returned: 4750 });
  assert.equal(outcome(ticket, { ...ended, winner: 2 }, NOW).status, 'lost');
  assert.equal(outcome(ticket, { ...ended, special: true }, NOW).returned, 2500);
  assert.equal(outcome(ticket, { ...m, status: 'canceled' }, NOW).status, 'void');
});
test('express rejects linked events and adapter fails closed for unknown schema', () => {
  const m = match(); assert.ok(related(m, { ...m, id: 'csgo:90002' }));
  assert.equal(related(m, { ...m, sport: 'dota2', id: 'dota2:90001' }), false);
  assert.equal(normalize({}, 'csgo'), null);
});
test('Moscow schedule rolls into the next calendar day at UTC+3', () => {
  assert.deepEqual(moscow(Date.parse('2026-10-29T22:59:00Z')), { date: '2026-10-30', hhmm: '01:59' });
  assert.deepEqual(moscow(Date.parse('2026-06-01T00:00:00Z')), { date: '2026-06-01', hhmm: '03:00' });
});
