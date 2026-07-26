from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import datetime as dt
from .services.operational_jobs import run_grants_ingest_job, run_sam_ingest_job
from .services.outlook_sync_jobs import run_outlook_conversation_sync_job

print("[SCHEDULER] scheduler.py imported")

def run_sam_ingest():
    print("[SCHEDULER] run_sam_ingest fired at", dt.datetime.utcnow().isoformat(), "UTC")
    run_sam_ingest_job()


def run_grants_ingest():
    print("[SCHEDULER] run_grants_ingest fired at", dt.datetime.utcnow().isoformat(), "UTC")
    run_grants_ingest_job()


def run_outlook_conversation_sync():
    print("[SCHEDULER] run_outlook_conversation_sync fired at", dt.datetime.now(dt.timezone.utc).isoformat())
    return run_outlook_conversation_sync_job()


def start_scheduler():
    print("[SCHEDULER] start_scheduler() called")
    sched = BackgroundScheduler(timezone="UTC")

    # V1 source refresh schedule: run SAM.gov once daily, then Grants.gov.
    sched.add_job(run_sam_ingest, CronTrigger(hour=1, minute=0))
    sched.add_job(run_grants_ingest, CronTrigger(hour=1, minute=30))
    sched.add_job(
        run_outlook_conversation_sync,
        IntervalTrigger(minutes=15),
        id="outlook-conversation-sync",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        replace_existing=True,
    )

    sched.start()
    return sched
