import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, chmodSync } from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { initialState, applyEvent, fingerprint, summary } from './engine.mjs';

export class PaperStore {
  constructor(filename, config) {
    const start = initialState(config);
    this.config = config;
    if (filename !== ':memory:') mkdirSync(path.dirname(path.resolve(filename)), { recursive: true });
    this.db = new DatabaseSync(filename);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS actions (seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);`);
    if (filename !== ':memory:') chmodSync(filename, 0o600);
    this.db.prepare('INSERT OR IGNORE INTO state VALUES (1, ?)').run(JSON.stringify(start));
    if (this.state().configHash !== fingerprint(config)) {
      this.db.close();
      throw new Error('Existing state belongs to a different configuration; use another database');
    }
  }
  state() { return JSON.parse(this.db.prepare('SELECT value FROM state WHERE id=1').get().value); }
  process(event) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const prior = this.db.prepare('SELECT payload FROM events WHERE id=?').get(event.id);
      if (prior) {
        assert(prior.payload === JSON.stringify(event), 'Event ID reused with different content');
        this.db.exec('COMMIT'); return [];
      }
      const result = applyEvent(this.state(), this.config, event);
      this.db.prepare('INSERT INTO events VALUES (?, ?)').run(event.id, JSON.stringify(event));
      this.db.prepare('UPDATE state SET value=? WHERE id=1').run(JSON.stringify(result.state));
      const insert = this.db.prepare('INSERT INTO actions(payload) VALUES (?)');
      for (const action of result.actions) insert.run(JSON.stringify(action));
      this.db.exec('COMMIT'); return result.actions;
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  actions() { return this.db.prepare('SELECT payload FROM actions ORDER BY seq').all().map(r => JSON.parse(r.payload)); }
  events() { return this.db.prepare('SELECT payload FROM events ORDER BY rowid').all().map(r => JSON.parse(r.payload)); }
  *eventRows() { for (const row of this.db.prepare('SELECT payload FROM events ORDER BY rowid').iterate()) yield row.payload; }
  summary() { return summary(this.state(), this.config); }
  close() { this.db.close(); }
}
