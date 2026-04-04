import logging
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from db import get_activities, get_body_battery, get_hrv, get_last_sync, get_sleep, get_summary, init_db
from sync import sync_garmin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Kronk Health Service")


@app.on_event("startup")
def startup():
    init_db()
    scheduler = BackgroundScheduler(timezone="America/New_York")
    # Daily sync at 6:30am — after devices have had time to upload overnight data
    scheduler.add_job(sync_garmin, "cron", hour=6, minute=30, id="daily_sync")
    scheduler.start()
    logger.info("Health service started — daily sync scheduled at 06:30")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("/app/static/index.html") as f:
        return f.read()


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/summary")
def api_summary(days: int = Query(default=7, ge=1, le=90)):
    return get_summary(days)


@app.get("/api/sleep")
def api_sleep(days: int = Query(default=14, ge=1, le=90)):
    return get_sleep(days)


@app.get("/api/hrv")
def api_hrv(days: int = Query(default=30, ge=1, le=365)):
    return get_hrv(days)


@app.get("/api/body-battery")
def api_body_battery(date: str = Query(default=None)):
    return get_body_battery(date)


@app.get("/api/activities")
def api_activities(limit: int = Query(default=8, ge=1, le=50)):
    return get_activities(limit)


@app.get("/api/sync-status")
def api_sync_status():
    return get_last_sync() or {"status": "never synced"}


@app.post("/api/sync")
def api_sync(background_tasks: BackgroundTasks):
    background_tasks.add_task(sync_garmin)
    return {"status": "sync started"}


@app.get("/health")
def health_check():
    last = get_last_sync()
    return {"status": "ok", "last_sync": last}
