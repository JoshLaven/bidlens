from __future__ import annotations

import argparse

TRIGGER_TYPE_SCHEDULED = "scheduled"


def _run_operational_job(*, trigger_type: str) -> int:
    from bidlens.services.operational_jobs import run_sam_ingest_job

    return run_sam_ingest_job(trigger_type=trigger_type)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the one-shot scheduled SAM.gov refresh for Railway Cron."
    )
    parser.add_argument(
        "--trigger-type",
        default=TRIGGER_TYPE_SCHEDULED,
        choices=("scheduled", "manual", "retry", "system"),
        help="JobRun trigger type to record. Defaults to scheduled.",
    )
    args = parser.parse_args(argv)

    print("BidLens SAM.gov refresh started", flush=True)
    try:
        operational_exit_code = _run_operational_job(trigger_type=args.trigger_type)
    except Exception as exc:
        print(f"BidLens SAM.gov refresh failed before completion: {type(exc).__name__}", flush=True)
        return 1

    if operational_exit_code:
        print(
            "BidLens SAM.gov refresh completed with organization-level failures; "
            "see JobRun and pull history for details.",
            flush=True,
        )
    else:
        print("BidLens SAM.gov refresh completed successfully", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
