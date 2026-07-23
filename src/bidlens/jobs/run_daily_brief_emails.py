from __future__ import annotations

import argparse
import datetime as dt

from bidlens.services.job_runs import TRIGGER_TYPE_SCHEDULED


def _run_operational_job(*, trigger_type: str, snapshot_date: dt.date | None) -> int:
    from bidlens.services.operational_jobs import run_daily_brief_emails_job

    return run_daily_brief_emails_job(trigger_type=trigger_type, snapshot_date=snapshot_date)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send Daily Brief emails for eligible workspace users.")
    parser.add_argument(
        "--trigger-type",
        default=TRIGGER_TYPE_SCHEDULED,
        choices=("scheduled", "manual", "retry", "system"),
        help="JobRun trigger type to record. Defaults to scheduled.",
    )
    parser.add_argument("--snapshot-date", default=None, help="Snapshot date in YYYY-MM-DD format. Defaults to today.")
    args = parser.parse_args(argv)
    snapshot_date = dt.date.fromisoformat(args.snapshot_date) if args.snapshot_date else None

    print("BidLens Daily Brief Email job started", flush=True)
    try:
        operational_exit_code = _run_operational_job(
            trigger_type=args.trigger_type,
            snapshot_date=snapshot_date,
        )
    except Exception as exc:
        print(f"BidLens Daily Brief Email job failed before completion: {type(exc).__name__}", flush=True)
        return 1

    if operational_exit_code:
        print(
            "BidLens Daily Brief Email job completed with isolated delivery failures; "
            "see JobRun and delivery history for details.",
            flush=True,
        )
    else:
        print("BidLens Daily Brief Email job completed successfully", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
