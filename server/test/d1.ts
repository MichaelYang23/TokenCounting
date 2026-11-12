// A minimal stand-in for Cloudflare D1 on top of node:sqlite, so the API's real
// SQL (and the real migration) run in plain `node --test`.
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import type { Database, Statement } from "../src/app.ts";

const MIGRATIONS = new URL("../migrations/", import.meta.url);

type Value = null | number | bigint | string | Uint8Array;

class SqliteStatement implements Statement {
  private readonly db: DatabaseSync;
  private readonly sql: string;
  private readonly args: Value[];

  constructor(db: DatabaseSync, sql: string, args: Value[] = []) {
    this.db = db;
    this.sql = sql;
    this.args = args;
  }

  bind(...values: unknown[]): Statement {
    for (const v of values) {
      if (v === undefined || typeof v === "boolean" || typeof v === "object" && v !== null) {
        throw new TypeError(`D1 cannot bind ${typeof v}: ${String(v)}`);
      }
    }
    return new SqliteStatement(this.db, this.sql, values as Value[]);
  }

  async first<T>(): Promise<T | null> {
    return (this.db.prepare(this.sql).get(...this.args) as T | undefined) ?? null;
  }

  async all<T>(): Promise<{ results: T[] }> {
    return { results: this.db.prepare(this.sql).all(...this.args) as T[] };
  }

  async run(): Promise<{ meta: { changes?: number } }> {
    const res = this.db.prepare(this.sql).run(...this.args);
    return { meta: { changes: Number(res.changes) } };
  }
}

export function memoryD1(): Database & { sqlite: DatabaseSync } {
  const sqlite = new DatabaseSync(":memory:");
  for (const name of readdirSync(MIGRATIONS).filter((f) => f.endsWith(".sql")).sort()) {
    sqlite.exec(readFileSync(new URL(name, MIGRATIONS), "utf8"));
  }
  return {
    sqlite,
    prepare: (sql: string) => new SqliteStatement(sqlite, sql),
    async batch(statements: Statement[]) {
      sqlite.exec("BEGIN");
      try {
        const out = [];
        for (const s of statements) out.push(await s.run());
        sqlite.exec("COMMIT");
        return out;
      } catch (err) {
        sqlite.exec("ROLLBACK");
        throw err;
      }
    },
  };
}
