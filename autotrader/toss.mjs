import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import WebSocket from 'ws';
import { time, kstDate } from './engine.mjs';

const BASE = 'https://openapi.tossinvest.com';
const WS = 'wss://openapi-ws.tossinvest.com/ws/v1';
const READ_PATHS = new Set(['/api/v1/orderbook', '/api/v1/prices', '/api/v1/market-calendar/KR']);

// Deliberately no account headers, order endpoint, or broker order method in the paper feed adapter.
export class TossReadOnly {
  constructor({ env = process.env, fetchImpl = fetch } = {}) {
    this.env = env; this.fetch = fetchImpl; this.token = env.TOSS_ACCESS_TOKEN; this.expiresAt = this.token ? Infinity : 0;
  }
  async accessToken() {
    if (this.token && Date.now() < this.expiresAt - 30000) return this.token;
    assert(this.env.TOSS_CLIENT_ID && this.env.TOSS_CLIENT_SECRET, 'Set TOSS_ACCESS_TOKEN or TOSS_CLIENT_ID and TOSS_CLIENT_SECRET');
    const response = await this.fetch(`${BASE}/oauth2/token`, { method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15000),
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'client_credentials', client_id: this.env.TOSS_CLIENT_ID, client_secret: this.env.TOSS_CLIENT_SECRET }) });
    assert(response.ok, `Toss token request failed (${response.status})`);
    const body = await response.json();
    assert(typeof body.access_token === 'string' && body.access_token && Number.isFinite(body.expires_in) && body.expires_in > 0, 'Invalid token response');
    this.token = body.access_token; this.expiresAt = Date.now() + body.expires_in * 1000;
    return this.token;
  }
  async get(endpoint, query = {}) {
    assert(READ_PATHS.has(endpoint), 'Read-only endpoint allowlist rejected this path');
    const url = new URL(endpoint, BASE); url.search = new URLSearchParams(query).toString();
    const response = await this.fetch(url, { method: 'GET', redirect: 'error', signal: AbortSignal.timeout(15000),
      headers: { Authorization: `Bearer ${await this.accessToken()}` } });
    assert(response.ok, `Toss read request failed (${response.status})`);
    const body = await response.json(); assert(body.result != null, 'Missing Toss result'); return body.result;
  }
  calendar() { return this.get('/api/v1/market-calendar/KR'); }
}

export function sessionAt(calendar, at) {
  const day = [calendar.today, calendar.previousBusinessDay, calendar.nextBusinessDay].find(d => d?.date === kstDate(at));
  if (!day?.integrated) return 'closed';
  const now = time(at);
  for (const [key, name] of [['regularMarket', 'regular'], ['afterMarket', 'after']]) {
    const interval = day.integrated[key];
    if (!interval || !(now >= time(interval.startTime) && now < time(interval.endTime))) continue;
    if (interval.singlePriceAuctionStartTime && now >= time(interval.singlePriceAuctionStartTime)) return 'closed';
    if (interval.singlePriceAuctionEndTime && now < time(interval.singlePriceAuctionEndTime)) return 'closed';
    return name;
  }
  return 'closed';
}

export function normalizeFrame(frame, calendar, receivedAt = new Date().toISOString()) {
  if (frame.type !== 'message') return null;
  const match = /^(trade|orderbook):kr:([0-9A-Z]{6})$/.exec(frame.topic ?? '');
  if (!match) return null;
  const [, channel, symbol] = match, data = frame.data;
  assert(data?.currency === 'KRW', 'Expected KRW market data');
  time(data.timestamp);
  const id = createHash('sha256').update(JSON.stringify({ topic: frame.topic, data })).digest('hex');
  if (channel === 'trade') return { id, type: 'trade', symbol, at: data.timestamp, receivedAt, price: Number(data.price) };
  assert(data.bids?.length && data.asks?.length, 'Empty orderbook');
  return { id, type: 'quote', symbol, at: data.timestamp, receivedAt, session: sessionAt(calendar, data.timestamp),
    bid: Number(data.bids[0].price), ask: Number(data.asks[0].price), bidSize: Number(data.bids[0].volume), askSize: Number(data.asks[0].volume) };
}

export async function runTossFeed({ symbols, onEvent, onStatus = () => {}, signal, client = new TossReadOnly(), WebSocketImpl = WebSocket }) {
  assert(symbols.length > 0 && symbols.length <= 40 && new Set(symbols).size === symbols.length && symbols.every(s => /^[0-9A-Z]{6}$/.test(s)), 'Invalid feed symbols');
  let backoff = 1000, calendar, calendarDay, lastPong = 0;
  while (!signal?.aborted) {
    try {
      if (!calendar || calendarDay !== kstDate(new Date().toISOString())) {
        calendar = await client.calendar(); calendarDay = kstDate(new Date().toISOString());
      }
      const token = await client.accessToken();
      await new Promise((resolve, reject) => {
        const socket = new WebSocketImpl(WS, { headers: { Authorization: `Bearer ${token}` }, handshakeTimeout: 15000 });
        let heartbeat, refresh, opened = false, settled = false;
        const cleanup = () => { clearInterval(heartbeat); clearInterval(refresh); signal?.removeEventListener('abort', abort); };
        const done = error => { if (settled) return; settled = true; cleanup(); socket.terminate(); error ? reject(error) : resolve(); };
        const abort = () => done();
        signal?.addEventListener('abort', abort, { once: true });
        if (signal?.aborted) return done();
        socket.on('open', () => {
          opened = true; lastPong = Date.now();
          socket.send(JSON.stringify([{ type: 'trade:kr', codes: symbols }, { type: 'orderbook:kr', codes: symbols }]));
          heartbeat = setInterval(() => {
            if (Date.now() - lastPong > 120000) return done(new Error('Feed heartbeat timed out'));
            socket.send('PING');
          }, 45000);
          refresh = setInterval(() => { if (calendarDay !== kstDate(new Date().toISOString())) done(new Error('Refresh daily market calendar')); }, 30000);
        });
        socket.on('message', raw => {
          try {
            const frame = JSON.parse(raw.toString());
            if (frame.type === 'pong') { lastPong = Date.now(); return; }
            if (frame.type === 'subscriptions') {
              assert(Array.isArray(frame.rejected) && !frame.rejected.length, 'Feed subscription rejected');
              const expected = symbols.flatMap(s => [`trade:kr:${s}`, `orderbook:kr:${s}`]);
              assert(expected.every(s => frame.subscribed?.includes(s)), 'Incomplete feed subscriptions');
              backoff = 1000; onStatus(true); return;
            }
            if (frame.type === 'error') throw new Error('Toss feed error; reconnecting');
            const event = normalizeFrame(frame, calendar);
            if (event) onEvent(event);
          } catch (error) { done(error); }
        });
        socket.on('error', () => done(new Error('Toss WebSocket connection failed')));
        socket.on('close', () => done(signal?.aborted ? null : new Error(opened ? 'Toss feed disconnected' : 'Toss handshake failed')));
      });
    } catch (error) {
      onStatus(false, error.message);
      if (signal?.aborted) break;
      await delay(backoff + Math.floor(Math.random() * 250), undefined, { signal }).catch(() => {});
      backoff = Math.min(backoff * 2, 30000);
    }
  }
}
