#!/usr/bin/env python3
"""Report source-material metadata/object drift without deleting objects."""

import argparse
import json

from bidlens.database import SessionLocal
from bidlens.services.opportunity_intake import configured_source_material_storage, reconcile_source_materials


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=int)
    parser.add_argument("--workspace-id", type=int)
    parser.add_argument("--detail-limit", type=int, default=100)
    args = parser.parse_args()
    with SessionLocal() as db:
        report = reconcile_source_materials(
            db,
            configured_source_material_storage(),
            organization_id=args.organization_id,
            workspace_id=args.workspace_id,
            detail_limit=args.detail_limit,
        )
    print(json.dumps({
        "metadata_objects": report.metadata_objects,
        "storage_objects": report.storage_objects,
        "missing_object_material_ids": report.missing_object_material_ids,
        "unreferenced_storage_keys": report.unreferenced_storage_keys,
        "expired_unpublished_material_ids": report.expired_unpublished_material_ids,
        "errors": report.errors,
    }, indent=2))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
