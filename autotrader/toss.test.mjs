import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { TossReadOnly, normalizeFrame, sessionAt, runTossFeed } from './toss.mjs';

const calendar = { today: { date: '2026-10-29', integrated: {
  regularMarket: { startTime: '2026-10-29T09:00:00+09:00', singlePriceAuctionStartTime: '2026-10-29T15:20:00+09:00', endTime: '2026-10-29T15:30:00+09:00' },
  afterMarket: { startTime: '2026-10-29T15:30:00+09:00', singlePriceAuctionEndTime: '2026-10-29T15:40:00+09:00', endTime: '2026-10-29T20:00:00+09:00' }
} } };
const at = '2026-10-29T10:00:00+09:00';
const book = { type: 'message', topic: 'orderbook:kr:005930', data: { timestamp: at, currency: 'KRW', bids: [{ price: '70000', volume: '100' }], asks: [{ price: '70100', volume: '200' }] } };

test('calendar excludes auction windows, holidays and closed sessions', () => {
  assert.equal(sessionAt(calendar, at), 'regular');
  assert.equal(sessionAt(calendar, '2026-10-29T15:25:00+09:00'), 'closed');
  assert.equal(sessionAt(calendar, '2026-10-29T15:35:00+09:00'), 'closed');
  assert.equal(sessionAt(calendar, '2026-10-29T15:40:00+09:00'), 'after');
  assert.equal(sessionAt(calendar, '2026-10-29T20:00:00+09:00'), 'closed');
  assert.equal(sessionAt({ today: { date: '2026-10-29', integrated: null } }, at), 'closed');
  assert.equal(sessionAt(calendar, '2026-10-30T10:00:00+09:00'), 'closed');
});

test('Toss official book frame becomes a timestamped bid/ask paper event', () => {
  const e = normalizeFrame(book, calendar, at);
  assert.equal(e.symbol, '005930'); assert.equal(e.type, 'quote'); assert.equal(e.bid, 70000); assert.equal(e.askSize, 200);
  assert.equal(e.session, 'regular'); assert.equal(normalizeFrame(book, calendar, at).id, e.id);
  const altered = structuredClone(book); altered.data.bids[0].volume = '101';
  assert.notEqual(normalizeFrame(altered, calendar, at).id, e.id);
});

test('trade normalization ignores account channels and rejects unknown currency', () => {
  assert.equal(normalizeFrame({ type: 'pong' }, calendar), null);
  assert.equal(normalizeFrame({ type: 'message', topic: 'personal:order:3' }, calendar), null);
  const e = normalizeFrame({ type: 'message', topic: 'trade:kr:005930', data: { timestamp: at, currency: 'KRW', price: '70000', volume: '1' } }, calendar, at);
  assert.equal(e.type, 'trade'); assert.equal(e.price, 70000);
  assert.throws(() => normalizeFrame({ ...book, data: { ...book.data, currency: 'USD' } }, calendar));
});

test('read-only client cannot reach order endpoints and keeps credentials off the query string', async () => {
  const calls = [], client = new TossReadOnly({ env: { TOSS_CLIENT_ID: 'fake-client', TOSS_CLIENT_SECRET: 'fake-secret' }, fetchImpl: async (url, options) => {
    calls.push({ url: String(url), options });
    return { ok: true, json: async () => String(url).endsWith('/oauth2/token') ? { access_token: 'fake-token', expires_in: 3600 } : { result: calendar } };
  } });
  assert.deepEqual(await client.calendar(), calendar); await client.calendar();
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 1);
  assert(calls.every(c => !c.url.includes('fake-secret')));
  assert.equal(calls[1].options.headers.Authorization, 'Bearer fake-token');
  assert.equal(calls[1].options.redirect, 'error');
  for (const path of ['/api/v1/orders', '/api/v1/conditional-orders', 'https://example.com']) await assert.rejects(client.get(path), /allowlist/);
});

test('existing token is reused without invalidating other clients', async () => {
  const client = new TossReadOnly({ env: { TOSS_ACCESS_TOKEN: 'existing' }, fetchImpl: async () => { throw new Error('must not request a token'); } });
  assert.equal(await client.accessToken(), 'existing');
});

test('non-OK responses do not expose a response body or token in errors', async () => {
  const client = new TossReadOnly({ env: { TOSS_ACCESS_TOKEN: 'secret' }, fetchImpl: async () => ({ ok: false, status: 401, json: async () => ({ secret: 'secret' }) }) });
  await assert.rejects(client.calendar(), error => !error.message.includes('secret') && error.message.includes('401'));
});

test('WebSocket handshake, full-replace subscriptions, frame delivery and abort work together', async () => {
  const controller = new AbortController(), statuses = [], events = []; let terminated = false;
  class FakeSocket extends EventEmitter {
    constructor(url, options) {
      super(); assert.equal(url, 'wss://openapi-ws.tossinvest.com/ws/v1');
      assert.equal(options.headers.Authorization, 'Bearer test');
      queueMicrotask(() => this.emit('open'));
    }
    send(payload) {
      const declaration = JSON.parse(payload);
      assert.deepEqual(declaration, [{ type: 'trade:kr', codes: ['005930'] }, { type: 'orderbook:kr', codes: ['005930'] }]);
      this.emit('message', Buffer.from(JSON.stringify({ type: 'subscriptions', subscribed: ['trade:kr:005930', 'orderbook:kr:005930'], rejected: [] })));
      queueMicrotask(() => this.emit('message', Buffer.from(JSON.stringify(book))));
    }
    terminate() { terminated = true; }
  }
  await runTossFeed({ symbols: ['005930'], client: { calendar: async () => calendar, accessToken: async () => 'test' }, WebSocketImpl: FakeSocket,
    signal: controller.signal, onStatus: ready => statuses.push(ready), onEvent: event => { events.push(event); controller.abort(); } });
  assert.deepEqual(statuses, [true]); assert.equal(events.length, 1); assert.equal(terminated, true);
});
