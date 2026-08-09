import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Fastify from "fastify";
import fastifyStatic from "@fastify/static";
import pg from "pg";
import { TimedCache } from "./cache.js";
import { normalizeUserMetric, summarize, type UserMetric, type UserMetricRow } from "./model.js";

const { Pool } = pg;
const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, "..");
const repositoryRoot = path.resolve(appRoot, "../..");
const passwordFile = process.env.OPS_DB_PASSWORD_FILE
  ? path.resolve(process.env.OPS_DB_PASSWORD_FILE)
  : path.join(repositoryRoot, "data", "ops-reporting.password");
const password = (await fs.readFile(passwordFile, "utf8")).trim();
if (!password) throw new Error("只读数据库密码文件为空");

const pool = new Pool({
  host: process.env.OPS_DB_HOST ?? "127.0.0.1",
  port: Number(process.env.OPS_DB_PORT ?? "15432"),
  database: process.env.OPS_DB_NAME ?? "slow",
  user: process.env.OPS_DB_USER ?? "slow_ops_ro",
  password,
  max: 2,
  idleTimeoutMillis: 90_000,
  connectionTimeoutMillis: 2_000,
  application_name: "slow_ops_local",
  options: "-c default_transaction_read_only=on -c statement_timeout=2000",
});

const cache = new TimedCache<UserMetric[]>(60_000);
const app = Fastify({ logger: false, bodyLimit: 1_024 });

app.addHook("onSend", async (_request, reply, payload) => {
  reply.header("Cache-Control", "no-store");
  reply.header("X-Content-Type-Options", "nosniff");
  reply.header("X-Frame-Options", "DENY");
  reply.header("Referrer-Policy", "no-referrer");
  reply.header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");
  return payload;
});

async function loadUsers(force = false): Promise<UserMetric[]> {
  if (!force) {
    const cached = cache.get();
    if (cached) return cached;
  }
  const result = await pool.query<UserMetricRow>(
    "SELECT * FROM ops_reporting.user_metrics_v1 ORDER BY created_at, account_ref",
  );
  return cache.set(result.rows.map(normalizeUserMetric));
}

app.get("/api/health", async (_request, reply) => {
  try {
    await pool.query("SELECT 1 FROM ops_reporting.user_metrics_v1 LIMIT 1");
    return { ok: true, tunnel: "connected", mode: "read_only" };
  } catch {
    return reply.code(503).send({ ok: false, tunnel: "unavailable", mode: "read_only" });
  }
});

app.get("/api/dashboard", async (request, reply) => {
  try {
    const query = request.query as { refresh?: string };
    const users = await loadUsers(query.refresh === "1");
    return {
      generatedAt: new Date().toISOString(),
      cacheSeconds: 60,
      summary: summarize(users),
      users,
    };
  } catch {
    return reply.code(503).send({
      error: "运营数据暂时不可用",
      detail: "请确认 SSH 隧道正在运行，且只读账号仍有权限。",
    });
  }
});

await app.register(fastifyStatic, {
  root: path.join(appRoot, "dist"),
  wildcard: false,
});
app.setNotFoundHandler((request, reply) => {
  if (request.url.startsWith("/api/")) return reply.code(404).send({ error: "Not found" });
  return reply.sendFile("index.html");
});

const host = "127.0.0.1";
const port = Number(process.env.OPS_PORT ?? "4174");
await app.listen({ host, port });
console.log(`Slow 运营瞭望台仅监听 http://${host}:${port}`);

const close = async () => {
  await app.close();
  await pool.end();
  process.exit(0);
};
process.on("SIGINT", close);
process.on("SIGTERM", close);
