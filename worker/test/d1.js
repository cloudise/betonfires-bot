// Test adapter only. Production uses a real D1 binding, never node:sqlite.
import { DatabaseSync } from 'node:sqlite';
export class D1 {
  constructor() { this.raw = new DatabaseSync(':memory:'); this.queries = 0; this.failOnce = null; }
  close() { this.raw.close(); }
  prepare(sql) {
    const owner = this;
    return new class Statement {
      constructor() { this.args = []; }
      bind(...args) { this.args = args; return this; }
      execute(mode) {
        owner.queries++;
        if (owner.failOnce && owner.failOnce(sql)) { owner.failOnce = null; throw new Error('Synthetic database failure'); }
        const stmt = owner.raw.prepare(sql);
        if (mode === 'first') { const row = stmt.get(...this.args); return row ? { ...row } : null; }
        if (mode === 'all' || /^\s*SELECT\b/i.test(sql)) {
          return { results: stmt.all(...this.args).map(r => ({ ...r })), success: true, meta: { changes: 0 } };
        }
        const r = stmt.run(...this.args);
        return { results: [], success: true, meta: { changes: Number(r.changes), last_row_id: Number(r.lastInsertRowid) } };
      }
      async run() { return this.execute('run'); }
      async all() { return this.execute('all'); }
      async first(column) { const row = this.execute('first'); return column && row ? row[column] : row; }
    };
  }
  async batch(statements) {
    this.raw.exec('BEGIN');
    try { const rows = statements.map(s => s.execute('run')); this.raw.exec('COMMIT'); return rows; }
    catch (e) { this.raw.exec('ROLLBACK'); throw e; }
  }
}
