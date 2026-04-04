"""
Garmin Connect sync — pulls the last N days of health data into local SQLite.

Auth flow:
  1. Try to load a saved garth session from /data/garth_session (fastest, no network auth).
  2. If the session is missing or expired, fall back to email/password login.
  3. Save the new session token so subsequent runs skip re-auth.

MFA: If Garmin requires MFA and you're running interactively, run:
    docker compose exec health_service python setup_auth.py
That will complete the MFA challenge and save the session token.
"""

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from db import get_conn

logger = logging.getLogger(__name__)

TOKEN_FILE = Path("/data/garmin_tokens.json")


def _load_credentials() -> tuple[str, str]:
    from infisical import get_secrets
    secrets = get_secrets()
    email    = secrets.get("GARMIN_EMAIL")
    password = secrets.get("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "GARMIN_EMAIL or GARMIN_PASSWORD not found in Infisical. "
            "Add them at the configured secret path in your Kronk project."
        )
    return email, password


def _get_client():
    from garminconnect import Garmin

    email, password = _load_credentials()
    client = Garmin(email, password)
    # login() will load existing tokens from TOKEN_FILE if present,
    # do a full auth flow if not, and auto-save tokens back to the file.
    client.login(tokenstore=str(TOKEN_FILE))
    logger.info("Garmin: authenticated (tokens at %s)", TOKEN_FILE)
    return client


def _safe_get(d: dict, *keys, default=None):
    """Safe nested dict access."""
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)
        if d is None:
            return default
    return d


def _sync_daily_summary(client, date_str: str):
    try:
        stats = client.get_stats(date_str) or {}
        rhr_data = client.get_rhr_day(date_str) or {}
        rhr = _safe_get(rhr_data, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE")
        resting_hr = rhr[0].get("value") if rhr else None

        with get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_summary
                (date, steps, calories_total, calories_active, distance_meters,
                 resting_hr, avg_stress, max_stress, body_battery_high, body_battery_low, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                stats.get("totalSteps"),
                stats.get("totalKilocalories"),
                stats.get("activeKilocalories"),
                stats.get("totalDistanceMeters"),
                resting_hr,
                stats.get("averageStressLevel"),
                stats.get("maxStressLevel"),
                stats.get("bodyBatteryHighestValue"),
                stats.get("bodyBatteryLowestValue"),
                datetime.utcnow().isoformat(),
            ))
    except Exception as e:
        logger.warning(f"daily_summary sync failed for {date_str}: {e}")


def _sync_sleep(client, date_str: str):
    try:
        data = client.get_sleep_data(date_str) or {}
        dto = _safe_get(data, "dailySleepDTO") or {}
        if not dto:
            return

        score = _safe_get(data, "sleepScores", "overall", "value")
        start_ts = dto.get("sleepStartTimestampGMT")
        end_ts = dto.get("sleepEndTimestampGMT")
        start_str = datetime.utcfromtimestamp(start_ts / 1000).isoformat() if start_ts else None
        end_str = datetime.utcfromtimestamp(end_ts / 1000).isoformat() if end_ts else None

        with get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO sleep
                (date, start_time, end_time, duration_seconds, deep_seconds,
                 light_seconds, rem_seconds, awake_seconds, score, avg_hrv, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                start_str,
                end_str,
                dto.get("sleepTimeSeconds"),
                dto.get("deepSleepSeconds"),
                dto.get("lightSleepSeconds"),
                dto.get("remSleepSeconds"),
                dto.get("awakeSleepSeconds"),
                score,
                dto.get("averageSpO2Value"),
                datetime.utcnow().isoformat(),
            ))
    except Exception as e:
        logger.warning(f"sleep sync failed for {date_str}: {e}")


def _sync_hrv(client, date_str: str):
    try:
        data = client.get_hrv_data(date_str) or {}
        summary = _safe_get(data, "hrvSummary") or {}
        if not summary:
            return

        baseline = summary.get("baseline") or {}
        with get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO hrv
                (date, weekly_avg, last_night, last_night_5min_high,
                 baseline_low, baseline_high, status, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                summary.get("weeklyAvg"),
                summary.get("lastNight"),
                summary.get("lastNight5MinHigh"),
                baseline.get("balancedLow"),
                baseline.get("balancedHigh"),
                summary.get("status"),
                datetime.utcnow().isoformat(),
            ))
    except Exception as e:
        logger.warning(f"HRV sync failed for {date_str}: {e}")


def _sync_body_battery(client, start_str: str, end_str: str):
    try:
        data = client.get_body_battery(start_str, end_str) or []
        synced_at = datetime.utcnow().isoformat()
        with get_conn() as conn:
            for day in data:
                day_date = day.get("date", "")
                for entry in day.get("bodyBatteryValuesArray", []):
                    # entry is [timestamp_ms, value, ...]
                    if not entry or len(entry) < 2:
                        continue
                    ts_ms = entry[0]
                    value = entry[1]
                    if ts_ms is None or value is None:
                        continue
                    ts_str = datetime.utcfromtimestamp(ts_ms / 1000).isoformat()
                    conn.execute("""
                        INSERT OR REPLACE INTO body_battery (timestamp, date, value, synced_at)
                        VALUES (?, ?, ?, ?)
                    """, (ts_str, day_date, int(value), synced_at))
    except Exception as e:
        logger.warning(f"body battery sync failed: {e}")


def _sync_activities(client, start_str: str, end_str: str):
    try:
        activities = client.get_activities_by_date(start_str, end_str) or []
        synced_at = datetime.utcnow().isoformat()
        with get_conn() as conn:
            for a in activities:
                act_type = _safe_get(a, "activityType", "typeKey", default="unknown")
                start_local = a.get("startTimeLocal", "")
                act_date = start_local[:10] if start_local else ""
                conn.execute("""
                    INSERT OR REPLACE INTO activities
                    (activity_id, date, name, type, duration_seconds,
                     distance_meters, avg_hr, max_hr, calories, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    a.get("activityId"),
                    act_date,
                    a.get("activityName"),
                    act_type,
                    int(a.get("duration", 0)),
                    a.get("distance"),
                    a.get("averageHR"),
                    a.get("maxHR"),
                    a.get("calories"),
                    synced_at,
                ))
    except Exception as e:
        logger.warning(f"activities sync failed: {e}")


def sync_garmin(days_back: int = 7):
    started_at = datetime.utcnow().isoformat()
    logger.info(f"Garmin sync started (last {days_back} days)")
    try:
        client = _get_client()
        today = date.today()
        start = today - timedelta(days=days_back)

        for i in range(days_back):
            d = (today - timedelta(days=i)).isoformat()
            _sync_daily_summary(client, d)
            time.sleep(1)
            _sync_sleep(client, d)
            time.sleep(1)
            _sync_hrv(client, d)
            time.sleep(1)

        _sync_body_battery(client, start.isoformat(), today.isoformat())
        time.sleep(1)
        _sync_activities(client, start.isoformat(), today.isoformat())

        completed_at = datetime.utcnow().isoformat()
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO sync_log (started_at, completed_at, status, message) VALUES (?, ?, ?, ?)",
                (started_at, completed_at, "ok", f"Synced {days_back} days"),
            )
        logger.info("Garmin sync completed successfully")
        return {"status": "ok", "completed_at": completed_at}

    except Exception as e:
        logger.error(f"Garmin sync failed: {e}")
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO sync_log (started_at, completed_at, status, message) VALUES (?, ?, ?, ?)",
                (started_at, datetime.utcnow().isoformat(), "error", str(e)),
            )
        raise
