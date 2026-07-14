from __future__ import annotations

import argparse
import datetime as dt

from bidlens.services.job_runs import TRIGGER_TYPE_SCHEDULED
from bidlens.services.operational_jobs import run_daily_snapshots_job


def run() -> int:
    parser = argparse.ArgumentParser(description="Generate Daily Snapshots for eligible workspace users.")
    parser.add_argument("--trigger-type", default=TRIGGER_TYPE_SCHEDULED, choices=("scheduled", "manual", "retry", "system"))
    parser.add_argument("--snapshot-date", default=None, help="Snapshot date in YYYY-MM-DD format. Defaults to today.")
    args = parser.parse_args()
    snapshot_date = dt.date.fromisoformat(args.snapshot_date) if args.snapshot_date else None
    return run_daily_snapshots_job(trigger_type=args.trigger_type, snapshot_date=snapshot_date)


if __name__ == "__main__":
    raise SystemExit(run())
