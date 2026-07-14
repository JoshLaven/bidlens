#!/usr/bin/env bash

# Source this file from the repository root:
#   source scripts/use-local.sh

export AUTO_CREATE_SCHEMA=true
export ENABLE_INTERNAL_SCHEDULER=true
unset DATABASE_URL

echo "BidLens developer environment"
echo "Database: Local SQLite"
echo "Scheduler: Enabled"
