from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import datetime as dt
from .services.operational_jobs import run_grants_ingest_job, run_sam_ingest_job

print("[SCHEDULER] scheduler.py imported")

def run_sam_ingest():
    print("[SCHEDULER] run_sam_ingest fired at", dt.datetime.utcnow().isoformat(), "UTC")
    run_sam_ingest_job()


def run_grants_ingest():
    print("[SCHEDULER] run_grants_ingest fired at", dt.datetime.utcnow().isoformat(), "UTC")
    run_grants_ingest_job()


def start_scheduler():
    print("[SCHEDULER] start_scheduler() called")
    sched = BackgroundScheduler(timezone="UTC")

    # Temporarily run once daily while SAM quota usage is being characterized.
    sched.add_job(run_sam_ingest, CronTrigger(hour=1, minute=0))
    sched.add_job(run_grants_ingest, CronTrigger(hour=1, minute=30))

    sched.start()
    return sched
