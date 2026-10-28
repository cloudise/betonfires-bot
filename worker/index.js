import { UserError } from './core.js';
import { initialize, settings } from './db.js';
import { command } from './service.js';
import { maintenance, telegram } from './maintenance.js';
import { PAGE } from './ui.js';

const ready = new WeakMap();
async function ensure(db) {
  if (!db) throw new Error('D1 binding DB missing');
  if (!ready.has(db)) ready.set(db, initialize(db).catch(e => { ready.delete(db); throw e; }));
  await ready.get(db);
}
const headers = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY' };
const json = (data, status = 200) => Response.json(data, { status, headers });
export async function hash(value) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function equal(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const ah = await hash(a), bh = await hash(b); let difference = 0;
  for (let i = 0; i < ah.length; i++) difference |= ah.charCodeAt(i) ^ bh.charCodeAt(i);
  return difference === 0;
}
export const webhookSecret = (token) => hash('betonfires-webhook:' + token);
function randomCode() { return [...crypto.getRandomValues(new Uint8Array(24))].map(n => n.toString(16).padStart(2, '0')).join(''); }
function privateSender(message) {
  const id = message?.from?.id;
  return Number.isSafeInteger(id) && id > 0 && message.chat?.type === 'private' && message.chat.id === id && !message.from.is_bot ? id : null;
}
async function smallJSON(request, max) {
  const reader = request.body?.getReader();
  if (!reader) return {};
  const chunks = []; let total = 0;
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    total += value.byteLength;
    if (total > max) { await reader.cancel(); throw new UserError('Слишком большой запрос.'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total); let offset = 0;
  for (const part of chunks) { bytes.set(part, offset); offset += part.length; }
  try { return JSON.parse(new TextDecoder().decode(bytes) || '{}'); }
  catch { throw new UserError('Некорректный JSON.'); }
}
async function activate(request, env, now) {
  const origin = new URL(request.url).origin;
  if (request.headers.get('Origin') !== origin) return json({ message: 'Настройка доступна только с этой страницы.' }, 403);
  const bearer = request.headers.get('Authorization');
  if (!env.TELEGRAM_BOT_TOKEN || !await equal(bearer, 'Bearer ' + env.TELEGRAM_BOT_TOKEN)) return json({ message: 'Проверь токен и секрет TELEGRAM_BOT_TOKEN в Cloudflare.' }, 401);
  const body = await smallJSON(request, 2048);
  await ensure(env.DB);
  const me = await telegram(env, 'getMe');
  if (me.username?.toLowerCase() !== String(env.EXPECTED_BOT_USERNAME || 'BETONFIRES_BOT').toLowerCase()) return json({ message: 'Этот токен принадлежит другому боту. Проверь настройки.' }, 400);
  const target = new URL('/telegram', request.url).href;
  const old = await telegram(env, 'getWebhookInfo');
  if (old.url && old.url !== target && body.replace !== true) return json({ message: 'У бота уже подключён другой webhook. Замена отключит прежний способ получения сообщений.' }, 409);
  await telegram(env, 'setWebhook', { url: target, secret_token: await webhookSecret(env.TELEGRAM_BOT_TOKEN),
    max_connections: 1, allowed_updates: ['message'], drop_pending_updates: false });
  const config = await settings(env.DB);
  if (config.owner) return json({ message: 'Telegram подключён. Владелец уже привязан; отправь боту /status.', link: `https://t.me/${me.username}` });
  const code = randomCode();
  await env.DB.prepare('UPDATE settings SET pairing_hash=?,pairing_expires=? WHERE id=1 AND owner IS NULL').bind(await hash(code), now + 3600000).run();
  return json({ message: 'Telegram подключён. Открой бота по кнопке и нажми Start в течение часа. Ссылка одноразовая.', link: `https://t.me/${me.username}?start=pair_${code}` });
}
export async function claim(db, sender, text, now) {
  const config = await settings(db);
  if (config.owner) return false;
  const code = /^\/start(?:@\w+)? pair_([a-f0-9]{48})$/.exec(text)?.[1] || /^\/pair(?:@\w+)? ([a-f0-9]{48})$/.exec(text)?.[1];
  if (!code) return false;
  const previous = await db.prepare('SELECT * FROM pairing_attempts WHERE sender=?').bind(sender).first();
  if (previous && previous.started > now - 86400000 && previous.count >= 5) return false;
  const digest = await hash(code);
  const result = await db.prepare('UPDATE settings SET owner=?,pairing_hash=NULL,pairing_expires=NULL WHERE id=1 AND owner IS NULL AND pairing_hash=? AND pairing_expires>=?')
    .bind(sender, digest, now).run();
  if (result.meta.changes) return true;
  await db.prepare(`INSERT INTO pairing_attempts(sender,count,started) VALUES(?,1,?)
    ON CONFLICT(sender) DO UPDATE SET count=CASE WHEN started<=? THEN 1 ELSE count+1 END,
      started=CASE WHEN started<=? THEN excluded.started ELSE started END`)
    .bind(sender, now, now - 86400000, now - 86400000).run();
  return false;
}
async function webhook(request, env, now) {
  if (!env.TELEGRAM_BOT_TOKEN || !await equal(request.headers.get('X-Telegram-Bot-Api-Secret-Token'), await webhookSecret(env.TELEGRAM_BOT_TOKEN))) return json({ ok: false }, 403);
  const update = await smallJSON(request, 32768);
  const sender = privateSender(update.message);
  if (!sender || typeof update.message.text !== 'string' || !Number.isSafeInteger(update.update_id)) return json({ ok: true });
  await ensure(env.DB);
  const config = await settings(env.DB);
  if (config.owner && config.owner !== sender) return json({ ok: true });
  const reserved = await env.DB.prepare("INSERT OR IGNORE INTO updates(id,created,state) VALUES(?,?,'processing')").bind(update.update_id, now).run();
  if (!reserved.meta.changes) return json({ ok: true });
  let response;
  if (!config.owner) {
    const success = await claim(env.DB, sender, update.message.text.trim(), now);
    response = success ? 'Бот привязан к твоему аккаунту. /status — подключения, /help — команды.' : 'Сначала открой одноразовую ссылку со страницы настройки своего Worker и нажми Start.';
  } else {
    try { response = await command(env.DB, update.message.text, update.update_id, now, env); }
    catch (error) { if (error instanceof UserError) response = error.message; else throw error; }
  }
  response = String(response).slice(0, 3500);
  await env.DB.prepare("UPDATE updates SET state='done',response=? WHERE id=?").bind(response, update.update_id).run();
  // Telegram executes this Bot API reply. At-most-once effects: a retry is acknowledged
  // without re-running the command. A lost response may require resending the command.
  return json({ method: 'sendMessage', chat_id: sender, text: response });
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url), now = Date.now();
    try {
      if (request.method === 'GET' && ['/', '/setup'].includes(url.pathname)) {
        return new Response(PAGE, { headers: { ...headers, 'Content-Type': 'text/html;charset=utf-8',
          'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'" } });
      }
      if (request.method === 'GET' && url.pathname === '/health') return json({ service: 'BETONFIRES', mode: 'free-paper-only', database_bound: !!env.DB });
      if (request.method === 'POST' && url.pathname === '/setup/activate') return await activate(request, env, now);
      if (request.method === 'POST' && url.pathname === '/telegram') return await webhook(request, env, now);
      return json({ message: 'Not found' }, 404);
    } catch (error) {
      if (error instanceof UserError) return json({ message: error.message }, 400);
      // No request body, token-bearing URL or remote exception is logged/returned.
      return json({ message: 'Не удалось завершить операцию. Проверь секреты, привязку DB и состояние сервисов.' }, 503);
    }
  },
  async scheduled(event, env, ctx) {
    if (!env.TELEGRAM_BOT_TOKEN || !env.DB) return;
    ctx.waitUntil((async () => { await ensure(env.DB); await maintenance(env.DB, env, event.scheduledTime || Date.now()); })());
  }
};
