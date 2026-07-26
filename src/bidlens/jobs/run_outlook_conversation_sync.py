from __future__ import annotations

from bidlens.services.outlook_sync_jobs import run_outlook_conversation_sync_job


def run() -> int:
    result = run_outlook_conversation_sync_job()
    return 1 if result["workspaces_failed"] or result["conversations_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(run())
