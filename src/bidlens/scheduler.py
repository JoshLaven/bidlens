import os
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .database import SessionLocal
from .ingest_sam import ingest_sam

def run_sam_ingest():
    naics_env = os.getenv("SAM_NAICS", "541611,541690")
    naics_list = [x.strip() for x in naics_env.split(",") if x.strip()]

    db = SessionLocal()
    try:
        results = ingest_sam(db, naics_list=naics_list, days_back=7)
        print("[SAM INGEST] done:", results)
    except Exception as e:
        print("[SAM INGEST] error:", repr(e))
    finally:
        db.close()

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")

    # 2x per day example: 13:00 and 01:00 UTC
    sched.add_job(run_sam_ingest, CronTrigger(hour="1,13", minute=0))

    sched.start()
    return sched
