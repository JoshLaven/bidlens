from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import datetime as dt
from .database import SessionLocal
from .ingest_sam import ingest_sam
from .models import SamSourceConfig
from .services.sam_source_config import ingest_kwargs

print("[SCHEDULER] scheduler.py imported")

def run_sam_ingest():
    print("[SCHEDULER] run_sam_ingest fired at", dt.datetime.utcnow().isoformat(), "UTC")

    db = SessionLocal()
    try:
        configs = db.query(SamSourceConfig).order_by(SamSourceConfig.organization_id.asc()).all()
        if not configs:
            print("[SAM INGEST] skipped: no workspace has a saved SAM.gov source configuration")
            return
        for config in configs:
            try:
                results = ingest_sam(
                    db,
                    organization_id=config.organization_id,
                    saved_search_name=config.name,
                    run_type="Scheduled",
                    **ingest_kwargs(config),
                )
                print(f"[SAM INGEST] org={config.organization_id} done:", results)
            except Exception as exc:
                db.rollback()
                print(f"[SAM INGEST] org={config.organization_id} error:", repr(exc))
    except Exception as e:
        print("[SAM INGEST] error:", repr(e))
    finally:
        db.close()

def start_scheduler():
    print("[SCHEDULER] start_scheduler() called")
    sched = BackgroundScheduler(timezone="UTC")

    # 2x per day example: 13:00 and 01:00 UTC
    sched.add_job(run_sam_ingest, CronTrigger(hour="1,13", minute=0))

    sched.start()
    return sched
