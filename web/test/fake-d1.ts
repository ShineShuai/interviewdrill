// Minimal D1 stand-in on node:sqlite, so tests run the real SQL from src/ and schema.sql.
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

type Params = (string | number | null)[];

class Stmt {
  db: DatabaseSync;
  sql: string;
  args: Params;
  constructor(db: DatabaseSync, sql: string, args: Params = []) {
    this.db = db;
    this.sql = sql;
    this.args = args;
  }
  bind(...args: Params): Stmt {
    return new Stmt(this.db, this.sql, args);
  }
  async first(): Promise<unknown> {
    return this.db.prepare(this.sql).get(...this.args) ?? null;
  }
  async all(): Promise<{ results: unknown[] }> {
    return { results: this.db.prepare(this.sql).all(...this.args) };
  }
  runSync(): { changes: number } {
    const r = this.db.prepare(this.sql).run(...this.args);
    return { changes: Number(r.changes) };
  }
  async run(): Promise<{ meta: { changes: number } }> {
    return { meta: this.runSync() };
  }
}

export function fakeD1() {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  return {
    raw: db,
    prepare: (sql: string) => new Stmt(db, sql),
    batch: async (stmts: Stmt[]) => {
      db.exec("BEGIN");
      try {
        const results = stmts.map((s) => ({ meta: s.runSync() }));
        db.exec("COMMIT");
        return results;
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
    },
  };
}
