import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path("/data/health.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                steps INTEGER,
                calories_total INTEGER,
                calories_active INTEGER,
                distance_meters REAL,
                resting_hr INTEGER,
                avg_stress INTEGER,
                max_stress INTEGER,
                body_battery_high INTEGER,
                body_battery_low INTEGER,
                synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sleep (
                date TEXT PRIMARY KEY,
                start_time TEXT,
                end_time TEXT,
                duration_seconds INTEGER,
                deep_seconds INTEGER,
                light_seconds INTEGER,
                rem_seconds INTEGER,
                awake_seconds INTEGER,
                score INTEGER,
                avg_hrv REAL,
                synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS hrv (
                date TEXT PRIMARY KEY,
                weekly_avg REAL,
                last_night REAL,
                last_night_5min_high REAL,
                baseline_low REAL,
                baseline_high REAL,
                status TEXT,
                synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS body_battery (
                timestamp TEXT PRIMARY KEY,
                date TEXT,
                value INTEGER,
                synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS activities (
                activity_id INTEGER PRIMARY KEY,
                date TEXT,
                name TEXT,
                type TEXT,
                duration_seconds INTEGER,
                distance_meters REAL,
                avg_hr INTEGER,
                max_hr INTEGER,
                calories INTEGER,
                synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                completed_at TEXT,
                status TEXT,
                message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_body_battery_date ON body_battery(date);
            CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
        """)


# ── Queries ───────────────────────────────────────────────────────────────────

def get_summary(days: int = 7) -> dict:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_summary WHERE date >= ? ORDER BY date DESC",
            (cutoff,)
        ).fetchall()
        today_row = rows[0] if rows else None

        sleep_rows = conn.execute(
            "SELECT duration_seconds, score FROM sleep WHERE date >= ? ORDER BY date DESC",
            (cutoff,)
        ).fetchall()

    week = [dict(r) for r in rows]
    avg_steps = int(sum(r["steps"] or 0 for r in rows) / len(rows)) if rows else None
    avg_sleep_h = round(
        sum((r["duration_seconds"] or 0) for r in sleep_rows) / len(sleep_rows) / 3600, 1
    ) if sleep_rows else None
    avg_hr = int(sum(r["resting_hr"] or 0 for r in rows if r["resting_hr"]) /
                 len([r for r in rows if r["resting_hr"]])) if any(r["resting_hr"] for r in rows) else None

    return {
        "today": dict(today_row) if today_row else None,
        "week": week,
        "averages": {"steps": avg_steps, "sleep_hours": avg_sleep_h, "resting_hr": avg_hr},
    }


def get_sleep(days: int = 14) -> list:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sleep WHERE date >= ? ORDER BY date DESC",
            (cutoff,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        total = d["duration_seconds"] or 1
        d["duration_hours"] = round(total / 3600, 2)
        d["deep_pct"] = round((d["deep_seconds"] or 0) / total * 100)
        d["light_pct"] = round((d["light_seconds"] or 0) / total * 100)
        d["rem_pct"] = round((d["rem_seconds"] or 0) / total * 100)
        d["awake_pct"] = round((d["awake_seconds"] or 0) / total * 100)
        result.append(d)
    return result


def get_hrv(days: int = 30) -> list:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM hrv WHERE date >= ? ORDER BY date ASC",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_body_battery(for_date: str | None = None) -> dict:
    target = for_date or date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT timestamp, value FROM body_battery WHERE date = ? ORDER BY timestamp ASC",
            (target,)
        ).fetchall()
        summary = conn.execute(
            "SELECT body_battery_high, body_battery_low FROM daily_summary WHERE date = ?",
            (target,)
        ).fetchone()

    curve = [{"time": r["timestamp"][11:16], "value": r["value"]} for r in rows]
    current = rows[-1]["value"] if rows else None
    return {
        "date": target,
        "current": current,
        "high": summary["body_battery_high"] if summary else None,
        "low": summary["body_battery_low"] if summary else None,
        "curve": curve,
    }


def get_activities(limit: int = 8) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activities ORDER BY date DESC, activity_id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["duration_minutes"] = round((d["duration_seconds"] or 0) / 60)
        d["distance_km"] = round((d["distance_meters"] or 0) / 1000, 2) if d["distance_meters"] else None
        result.append(d)
    return result


def get_last_sync() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
