import { GAMES, MODEL, UserError, amount, odds, money, pct, eligible, probability, issues,
  market, heading, moscow, related } from './core.js';
import * as store from './db.js';

export const NOTICE = 'CS2 / Dota 2 · исследовательский режим\nРеальные ставки: 0%. Точность и прибыльность модели не подтверждены.';
export const HELP = `${NOTICE}

/matches — ближайшие матчи на 48 часов
/analyze csgo:123 — разбор серии, A/B и ограничения
/odds csgo:123 1.90 2.05 — вручную задать коэффициенты BetBoom
/balance 10000 — реальный баланс только для справки
/paperbank 10000 — однократно создать виртуальный банк
/paper csgo:123 A — виртуальная сделка
/bank — виртуальные итоги и открытый риск
/journal — последние 8 записей
/today — сводка и виртуальные кандидаты
/express csgo:123:A dota2:456:B — условный расчёт пары
/model — состояние экспериментальной модели
/status — подключения и ограничения бесплатного режима
/subscribe 10:00 — сводка в 10:00 Москвы
/unsubscribe — отключить сводку

Коэффициенты: только победитель всей серии с 2 исходами, в порядке A/B из /analyze. Срок — 10 минут.
Виртуальная сделка 0.25% банка, максимум 1% открытых и 1% за 24 часа. Это параметры теста, а не безопасные реальные проценты.`;

export async function context(db) {
  const [states, quotes] = await db.batch([
    db.prepare('SELECT * FROM sync_state'), db.prepare('SELECT * FROM quotes ORDER BY observed DESC LIMIT 200')
  ]);
  return { states: Object.fromEntries(states.results.map(s => [s.sport, { ...s, parsed: s.model ? JSON.parse(s.model) : null }])),
    quotes: Object.fromEntries(quotes.results.map(q => [q.match_key, q])) };
}
export function analyze(m, ctx, now) {
  const state = ctx.states[m.sport], model = state?.parsed;
  const blockers = issues(model, m, now);
  if (!state || now - state.fixture_updated > 1800000 || now - state.results_updated > 1800000 ||
      now < state.fixture_updated || now < state.results_updated) blockers.push('Нет свежего расписания/результатов за 30 минут.');
  if (!model || now - model.updated > 1800000 || now < model.updated) blockers.push('Кэш модели ещё не готов или устарел.');
  if (state?.error) blockers.push('Последнее обновление источника завершилось ошибкой.');
  if (m.status !== 'not_started' || !Number.isFinite(m.start) || m.start <= now) blockers.push('Матч уже начался, отменён, перенесён или время неизвестно.');
  const p = model && !issues(model, m, now).length ? probability(model, m) : null;
  const q = ctx.quotes[m.id];
  let marketStats = null;
  if (!q) blockers.push('Нет свежих коэффициентов: /odds ID КФ_A КФ_B.');
  else {
    const old = JSON.parse(q.match_snapshot);
    if (q.observed > now || now - q.observed > 600000) blockers.push('Коэффициенты старше 10 минут или время неверно.');
    if (old.a_id !== m.a_id || old.b_id !== m.b_id || old.start !== m.start || old.bo !== m.bo) blockers.push('После ввода линии изменились стороны, формат или время.');
    if (p !== null) {
      marketStats = market(q.a, q.b, p);
      if (marketStats.margin < 0 || marketStats.margin > .25) blockers.push('Проверь коэффициенты: необычная маржа линии.');
    }
  }
  let side = null;
  if (!blockers.length && marketStats) {
    if (Math.max(marketStats.evA, marketStats.evB) > 0) side = marketStats.evA >= marketStats.evB ? 'A' : 'B';
    else blockers.push('Нет положительного условного EV даже по базовой модели.');
  }
  return { match: m, p, quote: q, stats: marketStats, side, blockers };
}
export function explain(row) {
  const lines = [heading(row.match)];
  if (row.p === null) lines.push('Оценка вероятности не опубликована: недостаточно подходящей истории.');
  else lines.push(`Сырой выход Elo: A ${pct(row.p)}, B ${pct(1 - row.p)}. Не откалибровано.`);
  if (row.quote) lines.push(`BetBoom: A ${(row.quote.a / 1000).toFixed(3)}, B ${(row.quote.b / 1000).toFixed(3)} · ручной ввод.`);
  if (row.stats) {
    const s = row.stats;
    lines.push(`Маржа: ${pct(s.margin)}; нормированные веса рынка: A ${pct(s.a)}, B ${pct(s.b)}.`,
      `Порог безубыточности: A ${pct(s.breakA)}, B ${pct(s.breakB)}.`,
      `Условный EV модели: A ${pct(s.evA)}, B ${pct(s.evB)}. Это не доказательство преимущества.`);
  }
  if (row.blockers.length) lines.push('ПРОПУСК:\n' + row.blockers.map(s => '• ' + s).join('\n'));
  else lines.push(`Кандидат только для виртуального наблюдения: ${row.side}.`);
  lines.push('Не учтены актуальные составы, veto/карты CS2, патч и драфт Dota 2.', 'Реальная ставка: 0%. Возможна потеря всей суммы ставки.');
  return lines.join('\n');
}
export async function bankText(db) {
  const b = await store.bank(db);
  const rows = await db.prepare("SELECT stake,returned FROM tickets WHERE status<>'open' ORDER BY settled,id").all();
  let value = b.initial, peak = b.initial, drawdown = 0;
  for (const t of rows.results) { value += t.returned - t.stake; peak = Math.max(peak, value); drawdown = Math.max(drawdown, peak ? (peak - value) / peak : 0); }
  return `Реальный баланс (ручная справка): ${b.reference_balance == null ? 'не задан' : money(b.reference_balance)}\n` +
    `Виртуально: доступно ${money(b.cash)}, открытый риск ${money(b.open)} ед.\n` +
    `P&L: ${money(b.pnl)}; ROI закрытых ставок: ${b.roi == null ? 'нет данных' : pct(b.roi)}.\n` +
    `Просадка по закрытым результатам: ${pct(drawdown)}. Записей ${b.total}, закрыто ${b.settled}.\nРеальные деньги не используются.`;
}
export async function daily(db, now) {
  const [matches, ctx] = await Promise.all([store.slate(db, now), context(db)]);
  const candidates = matches.map(m => analyze(m, ctx, now)).filter(r => r.side);
  candidates.sort((a, b) => Math.max(b.stats.evA, b.stats.evB) - Math.max(a.stats.evA, a.stats.evB));
  const lines = [NOTICE, 'Ближайшие 48 часов'];
  if (!candidates.length) lines.push('Подходящих виртуальных кандидатов сейчас нет. /matches — расписание.');
  for (const r of candidates.slice(0, 5)) lines.push(heading(r.match) + `\nВиртуальный кандидат ${r.side}; /analyze ${r.match.id}`);
  let pair = null;
  for (const a of candidates) { const b = candidates.find(b => !related(a.match, b.match)); if (b) { pair = [[a, a.side], [b, b.side]]; break; } }
  lines.push(pair ? express(pair) : 'Экспресс дня не сформирован: нет двух подходящих событий для расчёта.');
  lines.push('Автоматический платный импорт линии выключен. Коэффициенты вводятся вручную через /odds.');
  return lines.join('\n\n');
}
function express(pair) {
  const [[a, sa], [b, sb]] = pair;
  if (related(a.match, b.match)) throw new UserError('Совпали матч, команда или турнир — такая пара не рассчитывается.');
  const p = (sa === 'A' ? a.p : 1 - a.p) * (sb === 'A' ? b.p : 1 - b.p);
  const coefficient = a.quote[sa === 'A' ? 'a' : 'b'] * b.quote[sb === 'A' ? 'a' : 'b'] / 1e6;
  return `Условный экспресс: ${a.match.id}:${sa} + ${b.match.id}:${sb}\nПроизведение КФ: ${coefficient.toFixed(3)}.\n` +
    `ТОЛЬКО при независимости и верности Elo: оба исхода ${pct(p)}, проигрыш всей ставки ${pct(1 - p)}.\n` +
    'Независимость не доказана. Это калькулятор, не подтверждённая точность. Реальная ставка: 0%.';
}

export async function command(db, text, event, now, env) {
  const words = text.trim().split(/\s+/), cmd = words.shift().split('@')[0].toLowerCase(), args = words;
  if (['/start', '/help'].includes(cmd)) return HELP;
  if (cmd === '/status') {
    const last = await db.prepare('SELECT error FROM maintenance WHERE id=1').first();
    return `${NOTICE}\nРазмещение: Cloudflare Workers Free + D1 в пределах квот.\n` +
      `Расписание: ${env.PANDASCORE_TOKEN ? 'ключ задан; свежесть — /matches' : 'нужен бесплатный ключ PANDASCORE_TOKEN в настройках Cloudflare'}.\n` +
      'BetBoom: ручной ввод коэффициентов, автоматический импорт не подключён.\n' +
      (last?.error ? 'Последняя фоновая задача: ошибка. /matches покажет свежесть данных.' : 'Фоновые задачи выполняются небольшими порциями раз в минуту.') +
      '\nПревышение бесплатных лимитов может остановить обработку. Платные сервисы код не включает.';
  }
  if (cmd === '/matches') {
    const [matches, ctx] = await Promise.all([store.slate(db, now), context(db)]);
    const lines = [NOTICE];
    for (const [sport, label] of Object.entries(GAMES)) {
      const s = ctx.states[sport];
      lines.push(`${label}: расписание ${s?.fixture_updated ? Math.floor((now - s.fixture_updated) / 60000) + ' мин назад' : 'ещё не загружено'}, ` +
        `результаты ${s?.results_updated ? Math.floor((now - s.results_updated) / 60000) + ' мин назад' : 'ещё не загружены'}.`);
    }
    lines.push(...matches.slice(0, 10).map(heading));
    if (!matches.length) lines.push('В кэше нет матчей на 48 часов. Это не доказывает, что матчей вообще нет.');
    return lines.join('\n\n');
  }
  if (cmd === '/bank' || cmd === '/stats') return bankText(db);
  if (cmd === '/balance' && args.length === 1) {
    await db.prepare('UPDATE settings SET reference_balance=? WHERE id=1').bind(amount(args[0])).run();
    return bankText(db);
  }
  if (cmd === '/paperbank' && args.length === 1) {
    const value = amount(args[0]);
    if (!value) throw new UserError('Виртуальный банк должен быть больше нуля.');
    const result = await db.prepare('UPDATE settings SET paper_initial=? WHERE id=1 AND paper_initial=0').bind(value).run();
    if (!result.meta.changes) throw new UserError('Виртуальный банк уже создан. История сохраняется без сброса.');
    return bankText(db);
  }
  if (cmd === '/journal') {
    const rows = await db.prepare('SELECT * FROM tickets ORDER BY id DESC LIMIT 8').all();
    return 'Виртуальный журнал\n' + (rows.results.map(t => `#${t.id} ${t.match_key} ${t.side} · ${money(t.stake)} @ ${(t.odds_milli / 1000).toFixed(3)} · ${t.status} · возврат ${money(t.returned)}`).join('\n') || 'Пока пусто.');
  }
  if (cmd === '/today') return daily(db, now);
  if (cmd === '/model') {
    const ctx = await context(db);
    return [NOTICE, `Модель ${MODEL}`, ...Object.entries(GAMES).map(([key, label]) => `${label}: в кэше модели ${ctx.states[key]?.parsed?.n ?? 0} подходящих результатов.`),
      'Elo отдельно для игры и BO1/BO3/BO5. Минимум 10 серий на команду в этом BO и недавняя история за 45 дней.',
      'История подгружается постепенно. Нет калибровки и доказанного преимущества над линией.',
      'Полная проверка на истории доступна в исходной Python-версии репозитория. В бесплатном Worker тяжёлый backtest не запускается.'].join('\n');
  }
  if (cmd === '/subscribe' && args.length === 1) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(args[0])) throw new UserError('Время Москвы, например /subscribe 10:00.');
    await db.prepare('UPDATE settings SET digest_time=? WHERE id=1').bind(args[0]).run();
    const local = moscow(now);
    if (local.hhmm >= args[0]) await db.prepare("INSERT OR IGNORE INTO digests(day,status) VALUES(?,'skip')").bind(local.date).run();
    return `Сводка включена на ${args[0]} МСК. Возможна задержка фонового запуска.`;
  }
  if (cmd === '/unsubscribe') { await db.prepare('UPDATE settings SET digest_time=NULL WHERE id=1').run(); return 'Сводка отключена.'; }
  if (cmd === '/express' && args.length === 2) {
    const ctx = await context(db), pair = [];
    for (const value of args) {
      const cut = value.lastIndexOf(':'), key = value.slice(0, cut), side = value.slice(cut + 1).toUpperCase();
      const m = await store.match(db, key);
      if (!m || !['A', 'B'].includes(side)) throw new UserError('Формат: /express csgo:123:A dota2:456:B.');
      const row = analyze(m, ctx, now);
      if (row.blockers.length) throw new UserError('Сначала исправь ограничения в /analyze для обоих матчей.');
      pair.push([row, side]);
    }
    return express(pair);
  }
  if (['/analyze', '/odds', '/paper'].includes(cmd)) {
    const m = await store.match(db, args[0] || '');
    if (!m) throw new UserError('Матч не найден. Возьми ID из /matches, например csgo:123.');
    if (cmd === '/odds') {
      if (args.length !== 3) throw new UserError('Формат: /odds ID КФ_A КФ_B. Только победитель всей серии в BetBoom.');
      if (!eligible(m) || m.status !== 'not_started' || !Number.isFinite(m.start) || m.start <= now) throw new UserError('Подходит только будущий BO1/BO3/BO5 без технического результата.');
      await db.prepare('INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?)')
        .bind(m.id, odds(args[1]), odds(args[2]), now, JSON.stringify(m), 'BetBoom manual').run();
    }
    const row = analyze(m, await context(db), now);
    if (cmd === '/paper') {
      const side = (args[1] || '').toUpperCase();
      if (args.length !== 2 || row.blockers.length || side !== row.side) throw new UserError(explain(row));
      if (!await store.place(db, m, side, row.p, row.quote, event, now, MODEL)) throw new UserError('Матч уже записан, нет банка или исчерпан виртуальный лимит.');
      return 'Сохранена только виртуальная сделка.\n' + await bankText(db);
    }
    return explain(row);
  }
  if (cmd === '/betboom') return 'Бесплатный режим: коэффициенты BetBoom вводятся вручную через /odds. Автоматический импорт платного источника отключён.';
  throw new UserError('Команда не распознана. Примеры: /help.');
}
