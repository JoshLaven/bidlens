from __future__ import annotations

import argparse

from bidlens.services.job_runs import TRIGGER_TYPE_SCHEDULED
from bidlens.services.operational_jobs import run_sam_ingest_job


def run() -> int:
    parser = argparse.ArgumentParser(description="Run scheduled SAM.gov ingestion for eligible workspaces.")
    parser.add_argument("--trigger-type", default=TRIGGER_TYPE_SCHEDULED, choices=("scheduled", "manual", "retry", "system"))
    args = parser.parse_args()
    return run_sam_ingest_job(trigger_type=args.trigger_type)


if __name__ == "__main__":
    raise SystemExit(run())
