"""SQLite 持久化：池子清单、事件、LP 持仓缓存、快照历史、告警状态。"""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS pools (pool TEXT PRIMARY KEY, token0 TEXT, token1 TEXT, fee INT, tick_spacing INT, block INT);
CREATE TABLE IF NOT EXISTS events (
  pool TEXT, block INT, log_index INT, kind TEXT, amount0 REAL, amount1 REAL, usd REAL, actor TEXT,
  PRIMARY KEY (pool, block, log_index));
CREATE INDEX IF NOT EXISTS events_block ON events(block);
CREATE TABLE IF NOT EXISTS positions (id INT PRIMARY KEY, pool TEXT, owner TEXT, tick_lower INT, tick_upper INT, liquidity TEXT);
CREATE TABLE IF NOT EXISTS snapshots (ts INT, block INT, pool TEXT, data TEXT, PRIMARY KEY (ts, pool));
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, severity TEXT, message TEXT, first_ts INT, last_ts INT, active INT);
"""


HISTORY_FIELDS = ["tvl_usd", "depth2_up", "depth2_down", "vol_24h_usd", "price", "price_dev_pct", "top1_lp_pct", "impact10k_buy_pct"]


def compact(r):
    d2 = r.get("depth", {}).get("2") or {}
    return {"pool": r["pool"], "pair": r["pair"], "fee_pct": r["fee_pct"], "tvl_usd": r.get("tvl_usd"),
            "depth": {"2": d2} if d2 else {}, "depth2_up": d2.get("up"), "depth2_down": d2.get("down"),
            "vol_24h_usd": (r.get("events_24h") or {}).get("swap_usd", 0), "price": r.get("price"),
            "price_dev_pct": r.get("price_dev_pct"), "top1_lp_pct": (r.get("lp") or {}).get("top1_pct"),
            "impact10k_buy_pct": (r.get("impact") or {}).get("10000", {}).get("buy")}


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def get(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        self.db.commit()

    def add_pools(self, pools):
        self.db.executemany("INSERT OR IGNORE INTO pools VALUES (?,?,?,?,?,?)",
                            [(p["pool"], p["token0"], p["token1"], p["fee"], p["tickSpacing"], p["block"]) for p in pools])
        self.db.commit()

    def pools(self):
        cols = ("pool", "token0", "token1", "fee", "tickSpacing", "block")
        return [dict(zip(cols, r)) for r in self.db.execute("SELECT * FROM pools")]

    def add_events(self, rows):
        self.db.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def event_stats(self, pool, since_block):
        return self.db.execute(
            "SELECT kind, COUNT(*), COALESCE(SUM(usd),0), COALESCE(MAX(usd),0) FROM events WHERE pool=? AND block>=? GROUP BY kind",
            (pool, since_block)).fetchall()

    def upsert_positions(self, rows):
        self.db.executemany("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?)", rows)
        self.db.commit()

    def positions(self, pool=None):
        q, a = "SELECT id,pool,owner,tick_lower,tick_upper,liquidity FROM positions", ()
        if pool:
            q, a = q + " WHERE pool=?", (pool,)
        return [{"id": r[0], "pool": r[1], "owner": r[2], "tickLower": r[3], "tickUpper": r[4], "L": int(r[5])}
                for r in self.db.execute(q, a)]

    def save_snapshot(self, ts, block, rows, keep_days=30):
        """只存紧凑指标（告警回看 + 看板时间序列），全量明细只写 latest.json。"""
        self.db.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
                            [(ts, block, r["pool"], json.dumps(compact(r))) for r in rows])
        self.db.execute("DELETE FROM snapshots WHERE ts<?", (ts - keep_days * 86400,))
        self.db.commit()

    def export_history(self):
        out = {}
        for ts, pool, data in self.db.execute("SELECT ts, pool, data FROM snapshots ORDER BY ts"):
            d = json.loads(data)
            out.setdefault(pool, {"pair": d.get("pair"), "fee_pct": d.get("fee_pct"), "rows": []})["rows"].append(
                [ts] + [d.get(k) for k in HISTORY_FIELDS])
        return {"fields": ["ts"] + HISTORY_FIELDS, "pools": out}

    def import_history(self, hist):
        """把线上看板的 history.json 合并回本地（CI 环境缓存丢失时用来续接历史）。"""
        fields = hist.get("fields", [])[1:]
        rows = []
        for pool, v in hist.get("pools", {}).items():
            for row in v["rows"]:
                d = dict(zip(fields, row[1:]))
                d.update(pool=pool, pair=v.get("pair"), fee_pct=v.get("fee_pct"),
                         depth={"2": {"up": d.get("depth2_up"), "down": d.get("depth2_down")}})
                rows.append((row[0], 0, pool, json.dumps(d)))
        self.db.executemany("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)", rows)
        self.db.commit()
        return len(rows)

    def previous_snapshot(self, pool, before_ts, min_age_s=0):
        row = self.db.execute("SELECT data FROM snapshots WHERE pool=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                              (pool, before_ts - min_age_s)).fetchone()
        return json.loads(row[0]) if row else None

    def active_alerts(self):
        return {r[0]: r for r in self.db.execute("SELECT * FROM alerts WHERE active=1")}

    def set_alert(self, key, severity, message, ts):
        row = self.db.execute("SELECT active FROM alerts WHERE key=?", (key,)).fetchone()
        if row and row[0]:
            self.db.execute("UPDATE alerts SET last_ts=?, message=?, severity=? WHERE key=?", (ts, message, severity, key))
        else:
            self.db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,1)", (key, severity, message, ts, ts))
        self.db.commit()

    def resolve_alert(self, key, ts):
        self.db.execute("UPDATE alerts SET active=0, last_ts=? WHERE key=?", (ts, key))
        self.db.commit()
