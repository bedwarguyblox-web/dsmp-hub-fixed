import path from "path";
import { fileURLToPath } from "url";
import fs from "fs";
import { Router, type IRouter } from "express";
import Database from "better-sqlite3";
import {
  GetStatsResponse,
  GetVouchLeaderboardResponse,
  GetScamVouchLeaderboardResponse,
  GetRecentStrikesResponse,
  GetBuilderCasesResponse,
  GetBuilderPaymentsResponse,
  GetRecentActivityResponse,
} from "@workspace/api-zod";

const router: IRouter = Router();

// Use __dirname equivalent for ESM to get reliable path resolution
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Resolve DB path relative to this file's location, not process.cwd()
// This file is at artifacts/api-server/src/routes/bot-stats.ts
// DB is at data/database.db (3 levels up from here)
const DB_PATH = process.env.DATABASE_PATH || path.resolve(__dirname, "../../../../data/database.db");

// Singleton DB connection
let _db: Database.Database | null = null;

function getDb(): Database.Database | null {
  if (_db) return _db;
  if (!fs.existsSync(DB_PATH)) return null;
  _db = new Database(DB_PATH, { readonly: true });
  return _db;
}

router.get("/stats", (req, res) => {
  try {
    const db = getDb();
    if (!db) {
      res.json({ totalVouches: 0, totalScamVouches: 0, totalStrikes: 0, totalBuilderCases: 0, totalPayments: 0, activeTimers: 0, recentActions: 0 });
      return;
    }
    const totalVouches = (db.prepare("SELECT COUNT(*) as c FROM vouches").get() as { c: number }).c;
    const totalScamVouches = (db.prepare("SELECT COUNT(*) as c FROM scam_vouches").get() as { c: number }).c;
    const totalStrikes = (db.prepare("SELECT COUNT(*) as c FROM strike_history WHERE action='add'").get() as { c: number }).c;
    const totalBuilderCases = (db.prepare("SELECT COUNT(*) as c FROM builder_cases").get() as { c: number }).c;
    const totalPayments = (db.prepare("SELECT COUNT(*) as c FROM builder_payments").get() as { c: number }).c;
    const activeTimers = (db.prepare("SELECT COUNT(*) as c FROM builder_cases WHERE status='active'").get() as { c: number }).c;
    const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString().replace("T", " ").slice(0, 19);
    const recentActions = (db.prepare("SELECT COUNT(*) as c FROM staff_actions WHERE timestamp >= ?").get(since) as { c: number }).c;
    res.json(GetStatsResponse.parse({ totalVouches, totalScamVouches, totalStrikes, totalBuilderCases, totalPayments, activeTimers, recentActions }));
  } catch (err) {
    req.log.warn({ err }, "stats query failed");
    res.json({ totalVouches: 0, totalScamVouches: 0, totalStrikes: 0, totalBuilderCases: 0, totalPayments: 0, activeTimers: 0, recentActions: 0 });
  }
});

router.get("/vouches/leaderboard", (req, res) => {
  const limit = Math.min(Math.max(1, Number(req.query["limit"]) || 10), 100);
  try {
    const db = getDb();
    if (!db) { res.json([]); return; }
    const rows = db.prepare(
      `SELECT target_id as userId, COUNT(*) as total FROM vouches GROUP BY target_id ORDER BY total DESC LIMIT ?`
    ).all(limit) as { userId: string; total: number }[];
    res.json(GetVouchLeaderboardResponse.parse(rows.map((r, i) => ({ ...r, userId: String(r.userId), rank: i + 1 }))));
  } catch (err) {
    req.log.warn({ err }, "vouch leaderboard query failed");
    res.json([]);
  }
});

router.get("/scamvouches/leaderboard", (req, res) => {
  const limit = Math.min(Math.max(1, Number(req.query["limit"]) || 10), 100);
  try {
    const db = getDb();
    if (!db) { res.json([]); return; }
    const rows = db.prepare(
      `SELECT target_id as userId, COUNT(*) as total FROM scam_vouches GROUP BY target_id ORDER BY total DESC LIMIT ?`
    ).all(limit) as { userId: string; total: number }[];
    res.json(GetScamVouchLeaderboardResponse.parse(rows.map((r, i) => ({ ...r, userId: String(r.userId), rank: i + 1 }))));
  } catch (err) {
    req.log.warn({ err }, "scam vouch leaderboard query failed");
    res.json([]);
  }
});

router.get("/strikes/recent", (req, res) => {
  const limit = Math.min(Math.max(1, Number(req.query["limit"]) || 20), 100);
  try {
    const db = getDb();
    if (!db) { res.json([]); return; }
    const rows = db.prepare(
      `SELECT id, user_id as userId, moderator_id as moderatorId, reason, action, timestamp
       FROM strike_history ORDER BY timestamp DESC LIMIT ?`
    ).all(limit) as { id: number; userId: string; moderatorId: string; reason: string; action: string; timestamp: string }[];
    res.json(GetRecentStrikesResponse.parse(rows.map((r) => ({ ...r, userId: String(r.userId), moderatorId: String(r.moderatorId) }))));
  } catch (err) {
    req.log.warn({ err }, "strikes query failed");
    res.json([]);
  }
});

router.get("/builder/cases", (req, res) => {
  const limit = Math.min(Math.max(1, Number(req.query["limit"]) || 200), 500);
  try {
    const db = getDb();
    if (!db) { res.json([]); return; }
    const rows = db.prepare(
      `SELECT case_id as caseId, builder_id as builderId, customer_id as customerId,
              ign, amount, status, start_time as startTime, end_time as endTime, created_at as createdAt
       FROM builder_cases ORDER BY created_at DESC LIMIT ?`
    ).all(limit) as { caseId: string; builderId: string; customerId: string; ign: string; amount: string; status: string; startTime: string | null; endTime: string | null; createdAt: string }[];
    res.json(GetBuilderCasesResponse.parse(rows.map((r) => ({ ...r, builderId: String(r.builderId), customerId: String(r.customerId) }))));
  } catch (err) {
    req.log.warn({ err }, "builder cases query failed");
    res.json([]);
  }
});

router.get("/builder/payments", (req, res) => {
  const limit = Math.min(Math.max(1, Number(req.query["limit"]) || 20), 100);
  try {
    const db = getDb();
    if (!db) { res.json([]); return; }
    const rows = db.prepare(
      `SELECT id, payment_id as paymentId, staff_id as staffId, ign, amount, timestamp
       FROM builder_payments ORDER BY timestamp DESC LIMIT ?`
    ).all(limit) as { id: number; paymentId: string; staffId: string; ign: string; amount: string; timestamp: string }[];
    res.json(GetBuilderPaymentsResponse.parse(rows.map((r) => ({ ...r, staffId: String(r.staffId) }))));
  } catch (err) {
    req.log.warn({ err }, "builder payments query failed");
    res.json([]);
  }
});

router.get("/activity", (req, res) => {
  const limit = Math.min(Math.max(1, Number(req.query["limit"]) || 30), 200);
  try {
    const db = getDb();
    if (!db) { res.json([]); return; }
    const rows = db.prepare(
      `SELECT id, action_type as actionType, actor_id as actorId, target_id as targetId, details, timestamp
       FROM staff_actions ORDER BY timestamp DESC LIMIT ?`
    ).all(limit) as { id: number; actionType: string; actorId: string; targetId: string | null; details: string | null; timestamp: string }[];
    res.json(GetRecentActivityResponse.parse(rows.map((r) => ({ ...r, actorId: String(r.actorId), targetId: r.targetId != null ? String(r.targetId) : null }))));
  } catch (err) {
    req.log.warn({ err }, "activity query failed");
    res.json([]);
  }
});

export default router;
